"""Authenticated loopback receiver for responses posted by the iPhone Shortcut."""

from __future__ import annotations

import http.server
import json
import re
import time

from .config import receiver_port, receiver_token


CLIPBOARDS: dict[str, str] = {}
ALARMS: dict[str, list[dict[str, object]]] = {}


def id_from_header(value: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        return value
    digits = re.search(r"[0-9]{4,10}", value)
    return digits.group(0) if digits else None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "CodexIOSAssistantReceiver/1"

    @property
    def token(self) -> str:
        return self.server.receiver_token  # type: ignore[attr-defined]

    def _reply(self, code: int, text: str, *, content_type: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if self.headers.get("X-Auth") == self.token:
            return True
        self._reply(403, "bad token\n")
        return False

    def do_GET(self) -> None:
        alarm = re.fullmatch(r"/get-alarm/([A-Za-z0-9_-]{1,32})", self.path)
        if alarm:
            if not self._authorized():
                return
            records = ALARMS.get(alarm.group(1))
            if records is None:
                return self._reply(404, "no alarms for that id\n")
            return self._reply(
                200,
                json.dumps({"alarms": records}, ensure_ascii=False),
                content_type="application/json; charset=utf-8",
            )
        clipboard = re.fullmatch(r"/clipboard/([A-Za-z0-9_-]{1,32})", self.path)
        if clipboard:
            if not self._authorized():
                return
            text = CLIPBOARDS.get(clipboard.group(1))
            if text is None:
                return self._reply(404, "no clipboard for that id\n")
            return self._reply(200, text)
        if self.path in {"/", "/health"}:
            return self._reply(200, "codex-ios-assistant receiver up\n")
        self._reply(404, "unknown endpoint\n")

    def do_DELETE(self) -> None:
        alarm = re.fullmatch(r"/get-alarm/([A-Za-z0-9_-]{1,32})", self.path)
        if alarm:
            if not self._authorized():
                return
            ALARMS.pop(alarm.group(1), None)
            return self._reply(204, "")
        clipboard = re.fullmatch(r"/clipboard/([A-Za-z0-9_-]{1,32})", self.path)
        if clipboard:
            if not self._authorized():
                return
            CLIPBOARDS.pop(clipboard.group(1), None)
            return self._reply(204, "")
        self._reply(404, "unknown endpoint\n")

    def do_POST(self) -> None:
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._reply(400, "bad length\n")
        if not 0 <= length <= 50_000_000 or (length == 0 and self.path != "/clipboard"):
            return self._reply(400, "bad length\n")
        data = self.rfile.read(length)
        if self.path == "/clipboard":
            return self.handle_clipboard(data)
        if self.path == "/get-alarm":
            return self.handle_alarms(data)
        self._reply(404, "unknown endpoint\n")


    def handle_clipboard(self, data: bytes) -> None:
        try:
            payload = json.loads(data) if data else ""
        except (ValueError, UnicodeDecodeError):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                return self._reply(400, "clipboard body is not UTF-8 text\n")
        else:
            if isinstance(payload, str):
                text = payload
            elif isinstance(payload, dict) and isinstance(payload.get("clipboard"), str):
                text = payload["clipboard"]
            else:
                return self._reply(400, "clipboard JSON must be a string or clipboard field\n")
        request_id = id_from_header(self.headers.get("X-Screenshot-Id", ""))
        if not request_id:
            return self._reply(400, "missing or unusable X-Screenshot-Id\n")
        CLIPBOARDS[request_id] = text
        while len(CLIPBOARDS) > 200:
            CLIPBOARDS.pop(next(iter(CLIPBOARDS)))
        print(f"stored clipboard for {request_id} ({len(text)} chars; value redacted)", flush=True)
        self._reply(200, f"stored clipboard for {request_id}\n")

    def handle_alarms(self, data: bytes) -> None:
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return self._reply(400, "body is not valid JSON\n")
        value = payload.get("alarms") if isinstance(payload, dict) else None
        if isinstance(value, str):
            records: list[dict[str, object]] = []
            for line in value.splitlines():
                if not line.strip():
                    continue
                parts = line.split("\t", 3)
                parts += [""] * (4 - len(parts))
                snooze = parts[3].strip().lower()
                allows_snooze = True if snooze in {"true", "yes", "1", "on"} else False if snooze in {"false", "no", "0", "off"} else None
                records.append(
                    {
                        "time": parts[0].strip(),
                        "label": parts[1].strip(),
                        "repeat_days": parts[2].strip(),
                        "allows_snooze": allows_snooze,
                        "enabled": True,
                    }
                )
        elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
            records = value
        else:
            return self._reply(400, "alarms must be a text value or an array of objects\n")
        request_id = id_from_header(self.headers.get("X-Screenshot-Id", ""))
        if not request_id:
            return self._reply(400, "missing or unusable X-Screenshot-Id\n")
        ALARMS[request_id] = records
        while len(ALARMS) > 200:
            ALARMS.pop(next(iter(ALARMS)))
        print(f"stored {len(records)} active alarms for {request_id} (details redacted)", flush=True)
        self._reply(200, f"stored alarms for {request_id}\n")

    def log_message(self, format_string: str, *arguments: object) -> None:
        print(f"{self.address_string()} {format_string % arguments}", flush=True)


def main() -> int:
    token = receiver_token()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", receiver_port()), Handler)
    server.receiver_token = token  # type: ignore[attr-defined]
    print(f"receiver listening on 127.0.0.1:{receiver_port()}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
