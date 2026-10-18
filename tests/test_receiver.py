import http.server
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from iphone_cli.receiver import ALARMS, CLIPBOARDS, Handler


class ReceiverTests(unittest.TestCase):
    token = "test-token-that-is-longer-than-thirty-two-characters"

    def test_removed_screen_uploads_are_not_supported(self):
        for path in ("/text", "/screentext", "/photo", "/screenshot", "/shot"):
            with self.subTest(path=path):
                status, _ = self.request("POST", path, body=b"removed")
                self.assertEqual(status, 404)

    def setUp(self):
        CLIPBOARDS.clear()
        ALARMS.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.receiver_token = self.token
        self.server.inbox = Path(self.temporary.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.origin = f"http://{host}:{port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        authenticated: bool = True,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["X-Auth"] = self.token
        request = Request(
            self.origin + path,
            method=method,
            data=body,
            headers=request_headers,
        )
        try:
            with urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def test_health_is_public_but_data_paths_require_authentication(self):
        status, body = self.request("GET", "/health", authenticated=False)
        self.assertEqual(status, 200)
        self.assertIn(b"receiver up", body)

        status, _ = self.request("GET", "/clipboard/12345", authenticated=False)
        self.assertEqual(status, 403)


    def test_empty_clipboard_round_trip(self):
        status, _ = self.request(
            "POST",
            "/clipboard",
            body=b"",
            headers={"X-Screenshot-Id": "34567"},
        )
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/clipboard/34567")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")

    def test_alarm_text_becomes_structured_enabled_records(self):
        payload = json.dumps(
            {"alarms": "7:30 AM\tWake up\tWeekdays\ttrue\n8:15 AM\t\tNever\tfalse"}
        ).encode()
        status, _ = self.request(
            "POST",
            "/get-alarm",
            body=payload,
            headers={"Content-Type": "application/json", "X-Screenshot-Id": "45678"},
        )
        self.assertEqual(status, 200)
        status, body = self.request("GET", "/get-alarm/45678")
        self.assertEqual(status, 200)
        alarms = json.loads(body)["alarms"]
        self.assertEqual(len(alarms), 2)
        self.assertEqual(alarms[1]["label"], "")
        self.assertFalse(alarms[1]["allows_snooze"])
        self.assertTrue(all(alarm["enabled"] for alarm in alarms))


if __name__ == "__main__":
    unittest.main()
