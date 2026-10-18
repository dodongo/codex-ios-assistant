"""Finite UI sessions over an authenticated, userspace Tailscale developer tunnel."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import fcntl
import ipaddress
import os
from pathlib import Path
import plistlib
import re
import signal
import sys
import time
import uuid

from . import ui
from .errors import IPhoneError


def tailnet_address(value: str) -> str:
    """Validate an explicit Tailscale address without accepting LAN or public endpoints."""
    try:
        if not isinstance(value, str) or "%" in value:
            raise ValueError("expected an unscoped IP address")
        address = ipaddress.ip_address(value)
        network = ipaddress.ip_network("100.64.0.0/10" if address.version == 4 else "fd7a:115c:a1e0::/48")
        if address not in network:
            raise ValueError("not a Tailscale address")
        return str(address)
    except (ValueError, TypeError) as error:
        raise IPhoneError("Expected the iPhone's Tailscale IP address.") from error


async def _serve(nonce: str, state: dict) -> None:
    request = ui._read(ui.ROOT / (nonce + ".request.json"))
    duration, timeout = request.get("duration"), request.get("timeout")
    if not isinstance(duration, int) or not 10 <= duration <= 600:
        raise IPhoneError("Invalid supervisor lifetime.")
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise IPhoneError("Invalid supervisor startup timeout.")
    endpoint = tailnet_address(request.get("tailnet"))
    if os.environ.get("PYMOBILEDEVICE3_USERSPACE_TCP_RELAY"):
        raise IPhoneError("Tailscale UI control requires a private Unix relay, not a TCP relay.")
    config = ui._config()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", config["device"]):
        raise IPhoneError("Invalid paired device identifier.")
    record = Path.home() / ".pymobiledevice3" / ("remote_" + config["device"] + ".plist")
    if (not record.parent.is_dir() or record.parent.stat().st_mode & 0o077
            or record.is_symlink() or not record.is_file() or record.stat().st_uid != os.getuid()
            or record.stat().st_mode & 0o077):
        raise IPhoneError("A private RemotePairing record is required; pair the trusted iPhone over USB first.")
    runner_definition = plistlib.loads(Path(config["runner"]).read_bytes())["WebDriverAgentRunner"]
    runner_bundle = ui._bundle(runner_definition["TestHostBundleIdentifier"])
    ui._unlocked(config, 8)
    from pymobiledevice3.remote import tunnel_service
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.remote.tunnel_service import RemotePairingTunnelService
    from pymobiledevice3.remote.userspace_tunnel import UserspaceDialPlane
    from pymobiledevice3.services.dvt.testmanaged.xcuitest import TestConfig, XCUITestService
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    stop_file = ui.ROOT / (nonce + ".stop")
    socket_path = ui.ROOT / (nonce + ".sock")
    address = "unix:" + nonce
    state.update(address=address, tailnet=endpoint, deadline=time.time() + duration)
    ui._write(ui.ROOT / "state.json", state)
    ready_by = min(state["deadline"], time.time() + timeout)

    def remaining():
        if stop.is_set() or stop_file.exists():
            raise IPhoneError("UI startup cancelled.")
        value = ready_by - time.time()
        if value <= 0:
            raise IPhoneError("Tailscale UI startup timed out.")
        return value

    tunnel_service.USE_USERSPACE_TUNNEL = True
    async with AsyncExitStack() as resources:
        provider = RemotePairingTunnelService(config["device"], endpoint, 49152)
        resources.push_async_callback(provider.close)
        await asyncio.wait_for(provider.connect(autopair=False), remaining())
        tunnel = await asyncio.wait_for(resources.enter_async_context(provider.start_tcp_tunnel()), remaining())
        tunnel.client.tun.set_peer(tunnel.address)
        dialer = await resources.enter_async_context(UserspaceDialPlane(tunnel.client.tun, tunnel.address))
        if not getattr(dialer, "_socket_dir", None):
            raise IPhoneError("Private Unix tunnel relay could not start; refusing TCP fallback.")
        rsd = RemoteServiceDiscoveryService(
            (tunnel.address, tunnel.port), open_connection=dialer.dial,
            auxiliary_metadata=tunnel.auxiliary_metadata,
        )
        resources.push_async_callback(rsd.close)
        await asyncio.wait_for(rsd.connect(), remaining())
        if rsd.udid != config["device"]:
            raise IPhoneError("The Tailscale peer is not the configured iPhone.")
        task_set = set()

        async def relay(reader, writer):
            task_set.add(asyncio.current_task())
            remote_writer = None
            try:
                async with asyncio.timeout(max(5, min(300, state["deadline"] - time.time() + 5))):
                    remote_reader, remote_writer = await dialer.dial(tunnel.address, ui.PORT)

                    async def copy(source, destination):
                        count = 0
                        while data := await source.read(65536):
                            count += len(data)
                            if count > ui.MAX_RESPONSE + 8192:
                                raise IPhoneError("Controller relay exceeded the size limit.")
                            destination.write(data)
                            await destination.drain()

                    up = asyncio.create_task(copy(reader, remote_writer))
                    down = asyncio.create_task(copy(remote_reader, writer))
                    try:
                        done, _ = await asyncio.wait((up, down), return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            await task
                    finally:
                        up.cancel()
                        down.cancel()
                        await asyncio.gather(up, down, return_exceptions=True)
            except (OSError, TimeoutError, IPhoneError) as error:
                print(f"Private controller relay failed: {error}", file=sys.stderr, flush=True)
            finally:
                if remote_writer is not None:
                    remote_writer.close()
                writer.close()
                task_set.discard(asyncio.current_task())

        server = await asyncio.start_unix_server(relay, str(socket_path))
        socket_path.chmod(0o600)
        runner = None
        try:
            cfg = await asyncio.wait_for(TestConfig.create_for(rsd, runner_bundle), remaining())
            cfg.runner_app_env = {"USE_IP": tunnel.address, "USE_PORT": str(ui.PORT)}
            runner = asyncio.create_task(XCUITestService(rsd).run(cfg, timeout=duration))
            last_error = "Controller has not become ready."
            while time.time() < ready_by:
                if stop.is_set() or stop_file.exists():
                    raise IPhoneError("UI startup cancelled.")
                if runner.done():
                    await runner
                    raise IPhoneError("UI runner exited during startup.")
                try:
                    status = await asyncio.to_thread(ui._http, address, "/status", timeout=1)
                    if isinstance(status, dict) and status.get("ready") is True:
                        break
                except IPhoneError as error:
                    last_error = str(error)
                await asyncio.sleep(0.5)
            else:
                raise IPhoneError("Tailscale UI startup timed out: " + last_error)
            await asyncio.to_thread(ui._unlocked, config, min(5, remaining()), address)
            from .targeted import CAPABILITIES
            session = await asyncio.to_thread(ui._http, address, "/session", {
                "capabilities": {"alwaysMatch": {**CAPABILITIES, "forceAppLaunch": False, "shouldTerminateApp": False}},
            }, timeout=min(10, remaining()))
            if not isinstance(session, dict) or not isinstance(session.get("sessionId"), str):
                raise IPhoneError("WebDriverAgent did not create a session.")
            state.update(status="running", session=session["sessionId"])
            ui._write(ui.ROOT / "state.json", state)
            while time.time() < state["deadline"] and not stop.is_set() and not stop_file.exists():
                if runner.done():
                    await runner
                    raise IPhoneError("UI runner ended unexpectedly.")
                with (ui.ROOT / "commands.lock").open("a") as command_lock:
                    try:
                        fcntl.flock(command_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        await asyncio.to_thread(ui._unlocked, config, 30, address)
                try:
                    await asyncio.wait_for(stop.wait(), 2)
                except TimeoutError:
                    pass
        finally:
            try:
                if runner is not None:
                    try:
                        await asyncio.to_thread(ui._http, address, "/wda/shutdown", timeout=2)
                    finally:
                        runner.cancel()
                        await asyncio.gather(runner, return_exceptions=True)
            finally:
                server.close()
                for task in list(task_set):
                    task.cancel()
                await asyncio.gather(*task_set, return_exceptions=True)
                await server.wait_closed()
                socket_path.unlink(missing_ok=True)


def supervise(nonce: str) -> None:
    """Own one finite Tailscale tunnel and UI runner behind a private Unix socket."""
    state = {"nonce": nonce, "status": "starting"}
    with (ui.ROOT / "session.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise IPhoneError("Another UI session is already running.") from error
        ui._write(ui.ROOT / "state.json", state)
        try:
            asyncio.run(_serve(nonce, state))
        except Exception as error:
            state.update(status="failed", error=f"{type(error).__name__}: {error}")
        else:
            state["status"] = "stopped"
        finally:
            ui._write(ui.ROOT / "state.json", state)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "supervise":
        raise SystemExit("Internal supervisor entry point; use iphone ui.")
    supervise(str(uuid.UUID(sys.argv[2])))
