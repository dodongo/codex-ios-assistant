"""Local bridge commands used by the public ``iphone`` CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import DATA_DIR, message_target, receiver_token, receiver_url, sender_socket
from .errors import IPhoneError
from .ocr import recognize_text


MAX_COMMAND_BYTES = 4096
MAX_REPLY_BYTES = 16_384


def _read_line(connection: socket.socket, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        chunk = connection.recv(min(4096, limit + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if b"\n" in chunk:
            break
    data = b"".join(chunks)
    if len(data) > limit:
        raise IPhoneError("Bridge message exceeded its size limit.")
    return data.split(b"\n", 1)[0]


def send_command(command: str) -> None:
    """Ask the per-user sender agent to deliver one private command."""
    if not command.startswith("hola ") or "\n" in command or "\r" in command:
        raise IPhoneError("The sender accepts one single-line 'hola' command.")
    payload = json.dumps({"command": command}, separators=(",", ":")).encode() + b"\n"
    path = sender_socket()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(8)
            connection.connect(str(path))
            connection.sendall(payload)
            reply_bytes = _read_line(connection, MAX_REPLY_BYTES)
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise IPhoneError(
            "The Messages sender service is not running. Run scripts/install-services."
        ) from error
    except OSError as error:
        raise IPhoneError(f"Could not reach the Messages sender service: {error}") from error
    try:
        reply = json.loads(reply_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise IPhoneError("The Messages sender service returned an invalid response.") from error
    if not isinstance(reply, dict) or not reply.get("ok"):
        detail = reply.get("error") if isinstance(reply, dict) else None
        raise IPhoneError(str(detail or "The Messages sender service rejected the command."))


def _send_imessage(command: str) -> None:
    target = message_target()
    script = [
        "/usr/bin/osascript",
        "-e",
        "on run argv",
        "-e",
        'tell application id "com.apple.MobileSMS"',
        "-e",
        "set acct to 1st account whose service type is iMessage and enabled is true",
        "-e",
        "send (item 1 of argv) to participant (item 2 of argv) of acct",
        "-e",
        "end tell",
        "-e",
        "end run",
        command,
        target,
    ]
    completed = subprocess.run(script, capture_output=True, text=True, timeout=15, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise IPhoneError(f"Messages automation failed: {detail}")


def _peer_is_current_user(connection: socket.socket) -> bool:
    getpeereid = getattr(connection, "getpeereid", None)
    if getpeereid is None:
        return True
    peer_uid, _ = getpeereid()
    return peer_uid == os.getuid()


def _handle_sender_connection(connection: socket.socket) -> None:
    try:
        if not _peer_is_current_user(connection):
            raise IPhoneError("Sender connection came from another user.")
        request = json.loads(_read_line(connection, MAX_COMMAND_BYTES))
        command = request.get("command") if isinstance(request, dict) else None
        if (
            not isinstance(command, str)
            or not command.startswith("hola ")
            or "\n" in command
            or "\r" in command
        ):
            raise IPhoneError("The sender accepts one single-line 'hola' command.")
        _send_imessage(command)
        response = {"ok": True}
    except (IPhoneError, json.JSONDecodeError, UnicodeDecodeError) as error:
        response = {"ok": False, "error": str(error)}
    connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")


def run_sender() -> None:
    """Run the unsandboxed per-user Messages automation service."""
    path = sender_socket()
    if path.exists():
        mode = path.stat().st_mode
        if not stat.S_ISSOCK(mode):
            raise IPhoneError(f"Refusing to replace non-socket path: {path}")
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
        print(f"sender listening on {path}", flush=True)
        while True:
            connection, _ = server.accept()
            with connection:
                _handle_sender_connection(connection)
    finally:
        server.close()
        try:
            if path.exists() and stat.S_ISSOCK(path.stat().st_mode):
                path.unlink()
        except OSError:
            pass


def _correlation_id() -> str:
    return f"{secrets.randbelow(90_000) + 10_000}"


def _receiver_request(method: str, path: str) -> bytes | None:
    request = Request(
        receiver_url() + path,
        method=method,
        headers={"X-Auth": receiver_token()},
    )
    try:
        with urlopen(request, timeout=2) as response:
            return response.read()
    except HTTPError as error:
        if error.code == 404:
            return None
        raise IPhoneError(f"Receiver returned HTTP {error.code} for {path}.") from error
    except URLError as error:
        raise IPhoneError(f"Could not reach the local receiver: {error.reason}") from error


def _poll(path: str, timeout: int) -> bytes:
    deadline = time.monotonic() + timeout
    last_error: IPhoneError | None = None
    while time.monotonic() < deadline:
        try:
            value = _receiver_request("GET", path)
        except IPhoneError as error:
            last_error = error
        else:
            if value is not None:
                return value
        time.sleep(0.5)
    if last_error:
        raise last_error
    raise IPhoneError(f"Timed out after {timeout}s waiting for the iPhone response.")


def _timeout(environment_name: str, default: int) -> int:
    raw = os.environ.get(environment_name, str(default))
    try:
        value = int(raw)
    except ValueError as error:
        raise IPhoneError(f"{environment_name} must be an integer.") from error
    return max(1, value)


def read_screen() -> None:
    timeout = _timeout("READ_SCREEN_TIMEOUT", 30)
    started = time.monotonic()
    image = request_screen_capture(timeout=timeout)
    remaining = max(1, int(timeout - (time.monotonic() - started)))
    try:
        text = recognize_text(image, timeout=remaining)
    finally:
        try:
            image.unlink()
        except OSError:
            pass
    sys.stdout.write(f"<screen>\n{text}\n</screen>\n")


def read_clipboard() -> None:
    request_id = _correlation_id()
    _receiver_request("DELETE", f"/clipboard/{request_id}")
    send_command(f"hola getclipboard {request_id}")
    value = _poll(f"/clipboard/{request_id}", _timeout("CLIPBOARD_TIMEOUT", 30))
    sys.stdout.buffer.write(value)


def read_alarms() -> None:
    request_id = _correlation_id()
    _receiver_request("DELETE", f"/get-alarm/{request_id}")
    send_command(f"hola alarm get {request_id}")
    value = _poll(f"/get-alarm/{request_id}", _timeout("ALARM_TIMEOUT", 30))
    sys.stdout.buffer.write(value)


def request_screen_capture(*, timeout: int | None = None) -> Path:
    request_id = _correlation_id()
    inbox = DATA_DIR / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    os.chmod(DATA_DIR, 0o700)
    os.chmod(inbox, 0o700)
    candidates = [inbox / f"shot-{request_id}.{extension}" for extension in ("png", "jpg", "bin")]
    for candidate in candidates:
        if candidate.is_file():
            candidate.unlink()
    send_command(f"hola screenshot {request_id}")
    deadline = time.monotonic() + (timeout or _timeout("SCREENSHOT_TIMEOUT", 45))
    while time.monotonic() < deadline:
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        time.sleep(0.5)
    raise IPhoneError("Timed out waiting for the iPhone screenshot.")


def capture_screen() -> None:
    print(request_screen_capture())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iphone-assistant-bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send", help="send one command through the sender agent")
    send.add_argument("words", nargs="+")
    commands.add_parser("sender", help="run the per-user sender agent")
    commands.add_parser("read-screen", help="request and return on-screen text")
    commands.add_parser("screenshot", help="request a screenshot and return its path")
    commands.add_parser("clipboard", help="request and return the iPhone clipboard")
    commands.add_parser("alarms", help="request and return enabled alarms")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "send":
            send_command(" ".join(args.words))
        elif args.command == "sender":
            run_sender()
        elif args.command == "read-screen":
            read_screen()
        elif args.command == "screenshot":
            capture_screen()
        elif args.command == "clipboard":
            read_clipboard()
        elif args.command == "alarms":
            read_alarms()
        return 0
    except IPhoneError as error:
        print(f"iphone-assistant-bridge: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
