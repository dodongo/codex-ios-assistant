import asyncio
import base64
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from iphone_cli import tailnet, ui
from iphone_cli.errors import IPhoneError
from iphone_cli.cli import build_parser


class TailnetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="it-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patch = patch.object(ui, "ROOT", self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.nonce = str(uuid.uuid4())

    def test_tailnet_address_accepts_only_explicit_tailnet_ips(self):
        for value in ("100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::123"):
            self.assertEqual(tailnet.tailnet_address(value), value)
        for value in (None, 123, "localhost", "127.0.0.1", "192.168.1.1", "8.8.8.8",
                      "100.128.0.1", "::1", "fd12::1", "fd7a:115c:a1e0::1%en0"):
            with self.subTest(value=value), self.assertRaises(IPhoneError):
                tailnet.tailnet_address(value)

    def test_start_parser_accepts_an_explicit_tailnet_address(self):
        args = build_parser().parse_args(["ui", "start", "--tailnet-address", "100.64.0.1"])
        self.assertEqual(args.tailnet_address, "100.64.0.1")
        self.assertEqual(args.duration, 120)

    def test_private_http_uses_only_the_session_unix_socket(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.root / (self.nonce + ".sock")))
        listener.listen(1)
        listener.settimeout(3)
        self.addCleanup(listener.close)
        requests = []

        def respond():
            connection, _ = listener.accept()
            with connection:
                requests.append(connection.recv(8192))
                payload = b'{"value":{"ready":true}}'
                connection.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode()
                    + b"\r\nConnection: close\r\n\r\n" + payload
                )

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            self.assertEqual(ui._http("unix:" + self.nonce, "/status", timeout=2), {"ready": True})
        finally:
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertIn(b"GET /status HTTP/1.1", requests[0])

    def test_private_http_reports_connection_failure(self):
        with self.assertRaisesRegex(IPhoneError, "Private tunnel request failed"):
            ui._http("unix:" + self.nonce, "/status", timeout=1)

    def test_private_http_forwards_every_wda_method_and_json_body(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.root / (self.nonce + ".sock")))
        listener.listen(4)
        listener.settimeout(3)
        self.addCleanup(listener.close)
        received = []
        methods = ("GET", "POST", "PUT", "DELETE")

        def respond():
            for _ in methods:
                connection, _ = listener.accept()
                with connection:
                    stream = connection.makefile("rb")
                    first = stream.readline().decode().strip()
                    length = 0
                    while (line := stream.readline()) != b"\r\n":
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":", 1)[1])
                    received.append((first, stream.read(length)))
                    payload = b'{"value":"ok"}'
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(payload)).encode()
                                       + b"\r\nConnection: close\r\n\r\n" + payload)
                    stream.close()

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            for method in methods:
                self.assertEqual(ui._http("unix:" + self.nonce, "/endpoint", {"value": ["text"]},
                                          timeout=2, method=method), "ok")
        finally:
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual([first.split()[0] for first, _ in received], list(methods))
        for _, data in received:
            self.assertEqual(json.loads(data), {"value": ["text"]})

    def test_private_endpoint_rejects_path_traversal(self):
        for address in ("unix:../../config", "unix:/tmp/test.sock", "unix:"):
            with self.subTest(address=address), self.assertRaises(IPhoneError):
                ui._http(address, "/status")

    def test_active_session_rejects_different_private_endpoint(self):
        ui._write(self.root / "state.json", {
            "nonce": self.nonce, "address": "unix:" + str(uuid.uuid4()),
            "status": "running", "deadline": time.time() + 60, "session": "SESSION",
        })
        with self.assertRaisesRegex(IPhoneError, "identity changed"):
            ui._active()

    def test_tunnel_screenshot_avoids_native_device_commands(self):
        address = "unix:" + self.nonce
        state = {"nonce": self.nonce, "address": address, "session": "SESSION"}
        image = b"\x89PNG\r\n\x1a\noriginal screenshot"

        def http(endpoint, path, *args, **kwargs):
            self.assertEqual(endpoint, address)
            return {
                "/wda/locked": False,
                "/session/SESSION/source": '<App name="Settings" visible="true"/>',
                "/screenshot": base64.b64encode(image).decode(),
            }[path]

        import argparse
        with patch.object(ui, "_config", return_value={}), patch.object(ui, "_active", return_value=state), \
                patch.object(ui, "_http", side_effect=http), patch.object(ui, "_device") as native:
            result = ui.handle_ui(argparse.Namespace(action="request", method="GET", path="/source",
                                                    scope="session", data=None, observe=True, timeout=2))
        native.assert_not_called()
        picture = Path(result.data["screenshot"])
        self.assertEqual(picture.read_bytes(), image)
        self.assertEqual(picture.stat().st_mode & 0o777, 0o600)

    def test_supervisor_reports_invalid_request_without_contacting_phone(self):
        ui._write(self.root / (self.nonce + ".request.json"), {"duration": 601, "timeout": 1})
        tailnet.supervise(self.nonce)
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertIn("Invalid supervisor lifetime", state["error"])

    def test_supervisor_rejects_invalid_startup_timeout(self):
        for timeout in (0, float("nan"), float("inf"), 301):
            ui._write(self.root / (self.nonce + ".request.json"), {"duration": 60, "timeout": timeout})
            with self.subTest(timeout=timeout), self.assertRaisesRegex(IPhoneError, "startup timeout"):
                asyncio.run(tailnet._serve(self.nonce, {}))

    def test_wrong_device_identity_closes_connections_before_runner_launch(self):
        runner = self.root / "runner.xctestrun"
        import plistlib
        runner.write_bytes(plistlib.dumps({"WebDriverAgentRunner": {
            "TestHostBundleIdentifier": "com.example.Runner.xctrunner",
        }}))
        records = self.root / ".pymobiledevice3"
        records.mkdir(mode=0o700)
        record = records / "remote_TEST.plist"
        record.touch(mode=0o600)
        ui._write(self.root / (self.nonce + ".request.json"), {
            "duration": 60, "timeout": 2, "tailnet": "100.64.0.1",
        })

        class Context:
            def __init__(self, value):
                self.value = value

            async def __aenter__(self):
                return self.value

            async def __aexit__(self, *args):
                pass

        provider = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
        tun = SimpleNamespace(set_peer=Mock())
        tunnel = SimpleNamespace(address="fd12::1", port=1234, client=SimpleNamespace(tun=tun), auxiliary_metadata={})
        provider.start_tcp_tunnel = lambda: Context(tunnel)
        rsd = SimpleNamespace(connect=AsyncMock(), close=AsyncMock(), udid="TEST")
        test_config = SimpleNamespace(create_for=AsyncMock())
        modules = {
            "pymobiledevice3": SimpleNamespace(),
            "pymobiledevice3.remote": SimpleNamespace(tunnel_service=SimpleNamespace()),
            "pymobiledevice3.remote.remote_service_discovery": SimpleNamespace(RemoteServiceDiscoveryService=lambda *a, **k: rsd),
            "pymobiledevice3.remote.tunnel_service": SimpleNamespace(RemotePairingTunnelService=lambda *a: provider),
            "pymobiledevice3.remote.userspace_tunnel": SimpleNamespace(UserspaceDialPlane=lambda *a: Context(SimpleNamespace(dial=AsyncMock(), _socket_dir="private"))),
            "pymobiledevice3.services.dvt.testmanaged.xcuitest": SimpleNamespace(TestConfig=test_config, XCUITestService=Mock()),
        }
        rsd.udid = "OTHER"
        with patch.dict(sys.modules, modules), patch.object(Path, "home", return_value=self.root), \
                patch.object(ui, "_config", return_value={"device": "TEST", "runner": str(runner)}), \
                patch.object(ui, "_unlocked"), \
                self.assertRaisesRegex(IPhoneError, "not the configured iPhone"):
            asyncio.run(tailnet._serve(self.nonce, {}))
        provider.close.assert_awaited_once()
        rsd.close.assert_awaited_once()
        test_config.create_for.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
