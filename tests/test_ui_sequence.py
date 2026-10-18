import json
import signal
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from iphone_cli import cli, ui
from iphone_cli.errors import IPhoneError
from iphone_cli.transport import Result


class SequenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.clock = 0
        self.calls = []
        self.page = "Root"
        self.foreground = "com.example.Chat"
        self.app_polls = 0
        self.value = "1"
        self.stopped = False
        self.fail_at = None
        self.return_failure = False
        self.native_handle = ui.handle_ui
        self.state = {"nonce": str(uuid.uuid4()), "address": "unix:", "session": "SESSION", "deadline": time.time() + 60}
        self.state["address"] += self.state["nonce"]
        config = {"device": "DEVICE", "return_app": "com.example.Chat"}
        patches = [patch.object(ui, "ROOT", Path(self.temporary.name)), patch.object(ui, "_config", return_value=config),
                   patch.object(cli, "_ui_config", return_value=config), patch.object(ui, "_active", return_value=self.state),
                   patch.object(ui, "_unlocked"), patch.object(ui, "_http", side_effect=self.http),
                   patch.object(cli, "handle_ui", side_effect=self.handle),
                   patch.object(time, "monotonic", side_effect=lambda: self.clock),
                   patch.object(time, "sleep", side_effect=lambda duration: setattr(self, "clock", self.clock + duration))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def handle(self, args):
        if args.action == "stop":
            self.stopped = True
            self.calls.append("STOP")
            return Result("ui", "stop", "completed", "Stopped", {})
        return self.native_handle(args)

    def http(self, address, path, body=None, timeout=30, **kwargs):
        self.clock += 0.01
        self.calls.append(path)
        if self.fail_at == path:
            raise IPhoneError("Device failure")
        if path.endswith("/url"):
            self.app_polls = 2
            return None
        if path == "/wda/activeAppInfo":
            if self.app_polls:
                self.app_polls -= 1
                if self.app_polls == 0:
                    self.foreground = "com.example.Settings"
            return {"bundleId": self.foreground}
        if path.endswith("/elements"):
            name = body["value"]
            if name != ("Navigation" if self.page == "Root" else "Destination"):
                return []
            return [{"element-6066-11e4-a52e-4f735466cecf": name, "enabled": True, "displayed": True,
                     "attribute/name": name, "attribute/value": self.value,
                     "rect": {"x": 0, "y": 0, "width": 100, "height": 40}}]
        if path.endswith("/element/Navigation/click"):
            self.page = "Detail"
            return None
        if path.endswith("/actions"):
            self.value = "0"
            return None
        if path.endswith("/wda/apps/activate"):
            if self.return_failure:
                raise IPhoneError("Return failed")
            self.foreground = body["bundleId"]
            return None
        if path.endswith("/wda/apps/state"):
            return 4 if self.foreground == body["bundleId"] else 1
        self.fail("Unexpected request, including any full-screen scan: " + path)

    def args(self, then=None, *extra):
        destination = {"using": "accessibility id", "value": "Destination"}
        plan = [
            {"method": "POST", "path": "/element/{id}/click",
             "target": {"bundleId": "com.example.Settings", "using": "accessibility id", "value": "Navigation"},
             "ensure": {"target": destination, "attribute": "name", "equals": "Destination"}},
            {"method": "POST", "path": "/actions",
             "target": {"bundleId": "com.example.Settings", **destination, "point": {"x": 0.9, "y": 0.5}},
             "ensure": {"attribute": "value", "equals": "0"}},
        ]
        return cli.build_parser().parse_args(["ui", "request", "POST", "/url", "--data",
                                             '{"url":"example:settings","bundleId":"com.example.Settings"}',
                                             "--then", json.dumps(plan) if then is None else then,
                                             "--finish", "--timeout", "2", *extra])

    def test_complete_navigation_change_return_and_stop_without_full_screen_scans(self):
        result = cli._handle_ui_request(self.args())
        self.assertEqual(result.status, "completed")
        self.assertTrue(result.data["results"][-1]["value"]["verified"])
        self.assertEqual(self.value, "0")
        self.assertEqual(self.foreground, "com.example.Chat")
        self.assertTrue(self.stopped)
        self.assertEqual(self.calls[-1], "STOP")
        self.assertEqual(len(result.data["timings"]["steps"]), 3)
        self.assertIn("return_seconds", result.data["timings"])
        self.assertIn("stop_seconds", result.data["timings"])
        self.assertEqual(sum(path.endswith("/click") for path in self.calls), 1)
        self.assertEqual(sum(path.endswith("/actions") for path in self.calls), 1)

    def test_navigation_failure_skips_change_and_still_returns_and_stops(self):
        self.fail_at = "/session/SESSION/element/Navigation/click"
        with self.assertRaisesRegex(IPhoneError, "step 2 failed"):
            cli._handle_ui_request(self.args())
        self.assertFalse(any(path.endswith("/actions") for path in self.calls))
        self.assertEqual(self.foreground, "com.example.Chat")
        self.assertTrue(self.stopped)

    def test_stop_still_runs_when_return_fails_and_both_errors_are_reported(self):
        self.fail_at = "/session/SESSION/element/Navigation/click"
        self.return_failure = True
        with self.assertRaisesRegex(IPhoneError, "step 2 failed.*Return to app failed"):
            cli._handle_ui_request(self.args())
        self.assertTrue(self.stopped)

    def test_keyboard_interrupt_still_returns_and_stops(self):
        original = self.native_handle

        def interrupted(args):
            if not getattr(args, "dry_run", False) and args.path == "/actions":
                raise KeyboardInterrupt()
            return original(args)

        self.native_handle = interrupted
        with self.assertRaises(KeyboardInterrupt):
            cli._handle_ui_request(self.args())
        self.assertEqual(self.foreground, "com.example.Chat")
        self.assertTrue(self.stopped)

    def test_invalid_and_dry_run_plans_never_contact_the_device(self):
        for plan in ("{}", "[]", '[{"method":[],"path":"/url"}]', '[{"method":"POST","path":"/../escape"}]'):
            with self.subTest(plan=plan), self.assertRaises(IPhoneError):
                cli._handle_ui_request(self.args(plan))
        result = cli._handle_ui_request(self.args(None, "--dry-run"))
        self.assertEqual(result.status, "dry-run")
        self.assertEqual(self.calls, [])

    def test_termination_signal_uses_cleanup_and_restores_the_handler(self):
        original = self.native_handle
        previous = signal.getsignal(signal.SIGTERM)

        def terminated(args):
            if not getattr(args, "dry_run", False) and args.path == "/actions":
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return original(args)

        self.native_handle = terminated
        with self.assertRaises(KeyboardInterrupt):
            cli._handle_ui_request(self.args())
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
        self.assertEqual(self.foreground, "com.example.Chat")
        self.assertTrue(self.stopped)

    def test_entire_sequence_shares_a_deadline(self):
        self.page = "Wrong page"
        original = self.http

        def delayed(address, path, *args, **kwargs):
            if path.endswith("/elements"):
                self.clock += 0.4
                return []
            return original(address, path, *args, **kwargs)

        with patch.object(ui, "_http", side_effect=delayed):
            with self.assertRaisesRegex(IPhoneError, "deadline"):
                cli._handle_ui_request(self.args())
        self.assertFalse(any(path.endswith("/actions") for path in self.calls))
        self.assertTrue(self.stopped)
