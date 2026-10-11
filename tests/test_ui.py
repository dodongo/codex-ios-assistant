import argparse
import fcntl
import hashlib
import io
import json
import os
import plistlib
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from iphone_cli import ui
from iphone_cli.cli import build_parser
from iphone_cli.errors import IPhoneError


SOURCE = '<App><XCUIElementTypeButton name="Select" label="Select" enabled="true" visible="true"/></App>'


class UITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "private"
        self.root.mkdir()
        self.root_patch = patch.object(ui, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.address = "fd12:3456::1"
        self.locked = False
        self.source = SOURCE
        self.http_calls = []
        self.native_calls = []
        self.response = None
        self.element_results = [{"element-6066-11e4-a52e-4f735466cecf": "ABC-123"}]
        self.run_patch = patch("subprocess.run", side_effect=self.native)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)
        self.http_patch = patch("urllib.request.build_opener", return_value=SimpleNamespace(open=self.open))
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        runner = self.root / "runner" / "runner.xctestrun"
        runner.parent.mkdir()
        runner.write_bytes(plistlib.dumps({"WebDriverAgentRunner": {"EnvironmentVariables": {}}}))
        self.write("config.json", {"version": 1, "device": "TEST-DEVICE", "runner": str(runner), "return_app": "com.openai.chat"})
        self.nonce = str(uuid.uuid4())
        self.write("state.json", {"nonce": self.nonce, "status": "running", "address": self.address,
                                 "session": "SESSION-ID", "deadline": time.time() + 120})

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def native(self, command, **kwargs):
        self.native_calls.append(command)
        if "lockState" in command:
            value = {"passcodeRequired": self.locked, "unlockedSinceBoot": True}
        elif "details" in command:
            value = {"connectionProperties": {"pairingState": "paired", "tunnelState": "connected", "tunnelIPAddress": self.address}}
        elif "screenshot" in command:
            Path(command[command.index("--destination") + 1]).write_bytes(b"PNG test payload")
            return SimpleNamespace(returncode=0, stdout="Screenshot saved", stderr="")
        else:
            return SimpleNamespace(returncode=0, stdout="App installed", stderr="")
        return SimpleNamespace(returncode=0, stdout=json.dumps({"result": value}), stderr="")

    def open(self, request, **kwargs):
        self.http_calls.append((request.full_url, request.data))
        if isinstance(self.response, Exception):
            raise self.response
        if self.response is not None:
            return io.BytesIO(self.response)
        if request.full_url.endswith("/source"):
            value = self.source
        elif request.full_url.endswith("/wda/locked"):
            value = self.locked
        elif request.full_url.endswith("/elements"):
            value = self.element_results
        elif request.full_url.endswith("/wda/apps/state"):
            value = 4
        elif request.full_url.endswith("/status"):
            value = {"ready": True}
        elif request.full_url.endswith("/session"):
            value = {"sessionId": "SESSION-ID"}
        elif request.full_url.endswith("/wda/shutdown"):
            return io.BytesIO(b"Shutting down")
        else:
            value = None
        return io.BytesIO(json.dumps({"value": value}).encode())

    def call(self, action, **values):
        return ui.handle_ui(argparse.Namespace(action=action, timeout=2, dry_run=False, **values))

    def test_handle_ui_dry_run_does_not_touch_device(self):
        result = ui.handle_ui(argparse.Namespace(action="start", dry_run=True, timeout=30))
        self.assertEqual(result.status, "dry-run")
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.http_calls, [])

    def test_handle_ui_rejects_invalid_timeout(self):
        for timeout in [float("nan"), float("inf"), 0, 301]:
            with self.subTest(timeout=timeout), self.assertRaises(IPhoneError):
                ui.handle_ui(argparse.Namespace(action="status", timeout=timeout))

    def test_handle_ui_observe_and_tap_use_fresh_private_snapshot(self):
        result = self.call("observe")
        snapshot = result.data["snapshot"]
        self.assertEqual(result.data["elements"][0]["name"], "Select")
        self.assertEqual(Path(result.data["screenshot"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        result = self.call("tap", name="Select", snapshot=snapshot)
        self.assertEqual(result.status, "requested")
        self.assertTrue(self.http_calls[-1][0].endswith("/element/ABC-123/click"))
        self.assertFalse((self.root / "snapshots" / (snapshot + ".json")).exists())

    def test_handle_ui_rejects_replayed_snapshot(self):
        snapshot = self.call("observe").data["snapshot"]
        self.call("tap", name="Select", snapshot=snapshot)
        with self.assertRaises(IPhoneError):
            self.call("tap", name="Select", snapshot=snapshot)

    def test_handle_ui_rejects_locked_device_before_action(self):
        self.locked = True
        with self.assertRaisesRegex(IPhoneError, "Unlock"):
            self.call("open", bundle="com.apple.shortcuts")
        self.assertEqual(len(self.http_calls), 1)
        self.assertTrue(self.http_calls[0][0].endswith("/wda/locked"))

    def test_handle_ui_rejects_unknown_controller_lock_state(self):
        self.response = json.dumps({"value": None}).encode()
        with self.assertRaisesRegex(IPhoneError, "Cannot verify"):
            self.call("observe")

    def test_handle_ui_rejects_changed_tunnel(self):
        self.address = "fd12:3456::2"
        with self.assertRaisesRegex(IPhoneError, "tunnel changed"):
            self.call("observe")

    def test_handle_ui_rejects_lan_or_public_endpoint(self):
        for address in ["192.168.1.1", "2001:4860::1", "::1", None]:
            self.address = address
            with self.subTest(address=address), self.assertRaises(IPhoneError):
                self.call("observe")

    def test_handle_ui_rejects_stale_changed_and_malformed_snapshots(self):
        for change in [{"created": time.time() - 31}, {"created": float("nan")},
                       {"nonce": "other"}, {"digest": "wrong"}, {"elements": None}]:
            result = self.call("observe")
            snapshot = result.data["snapshot"]
            path = self.root / "snapshots" / (snapshot + ".json")
            record = json.loads(path.read_text())
            record.update(change)
            path.write_text(json.dumps(record))
            with self.subTest(change=change), self.assertRaises(IPhoneError):
                self.call("tap", name="Select", snapshot=snapshot)

    def test_handle_ui_rejects_path_traversal_snapshot(self):
        with self.assertRaises(IPhoneError):
            self.call("tap", name="Select", snapshot="../../config")

    def test_handle_ui_rejects_ambiguous_or_unobserved_controls(self):
        snapshot = self.call("observe").data["snapshot"]
        with self.assertRaises(IPhoneError):
            self.call("tap", name="Send", snapshot=snapshot)
        self.element_results *= 2
        with self.assertRaisesRegex(IPhoneError, "ambiguous"):
            self.call("tap", name="Select", snapshot=snapshot)

    def test_handle_ui_rejects_invalid_element_id(self):
        snapshot = self.call("observe").data["snapshot"]
        self.element_results = [{"element-6066-11e4-a52e-4f735466cecf": "../../send"}]
        with self.assertRaisesRegex(IPhoneError, "element identifier"):
            self.call("tap", name="Select", snapshot=snapshot)

    def test_handle_ui_tap_disambiguates_repeated_names_by_observed_label(self):
        self.source = '<App><XCUIElementTypeButton name="enum" label="Value" visible="true" enabled="true"/>' \
                      '<XCUIElementTypeButton name="enum" label="Condition" visible="true" enabled="true"/></App>'
        snapshot = self.call("observe").data["snapshot"]
        with self.assertRaises(IPhoneError):
            self.call("tap", name="enum", snapshot=snapshot)
        self.call("tap", name="enum", label="Value", snapshot=snapshot)
        query = json.loads(self.http_calls[-2][1])
        self.assertEqual(query, {"using": "predicate string", "value": 'name == "enum" AND type == "XCUIElementTypeButton" AND visible == true AND enabled == true AND label == "Value"'})

    def test_handle_ui_invalidates_snapshot_when_tap_outcome_is_unknown(self):
        snapshot = self.call("observe").data["snapshot"]
        normal = self.open
        def failure(request, **kwargs):
            if request.full_url.endswith("/click"):
                raise URLError("connection lost")
            return normal(request, **kwargs)
        with patch("urllib.request.build_opener", return_value=SimpleNamespace(open=failure)):
            with self.assertRaisesRegex(IPhoneError, "outcome is unknown"):
                self.call("tap", name="Select", snapshot=snapshot)
        self.assertFalse((self.root / "snapshots" / (snapshot + ".json")).exists())

    def test_handle_ui_redacts_secure_values_and_limits_output(self):
        self.source = '<App><XCUIElementTypeSecureTextField name="Password" value="private" visible="true"/>' + ''.join(
            f'<Text name="item-{n}" visible="true"/>' for n in range(201)) + '</App>'
        result = self.call("observe")
        self.assertNotIn("value", result.data["elements"][0])
        self.assertEqual(len(result.data["elements"]), 200)
        self.assertTrue(result.data["truncated"])

    def test_handle_ui_rejects_xml_declarations_and_invalid_responses(self):
        for response in [b"not json", b"{}", json.dumps({"value": "<!DOCTYPE x><App/>"}).encode(),
                         json.dumps({"value": "<broken>"}).encode(), b"a" * (ui.MAX_RESPONSE + 1),
                         json.dumps({"value": {"error": "bad", "message": "failure"}}).encode()]:
            self.response = response
            with self.subTest(length=len(response)), self.assertRaises(IPhoneError):
                self.call("observe")

    def test_handle_ui_surfaces_http_and_connection_failures(self):
        for error in [URLError("unreachable"), HTTPError("url", 404, "missing", {}, io.BytesIO(b"no such element"))]:
            self.response = error
            with self.subTest(error=error), self.assertRaises(IPhoneError):
                self.call("observe")

    def test_handle_ui_open_and_return_verify_foreground(self):
        result = self.call("open", bundle="com.apple.shortcuts")
        self.assertEqual(result.status, "completed")
        self.assertEqual(json.loads(self.http_calls[-1][1])["bundleId"], "com.apple.shortcuts")
        self.assertTrue(self.http_calls[-2][0].endswith("/wda/apps/activate"))
        self.call("return")
        self.assertEqual(json.loads(self.http_calls[-1][1])["bundleId"], "com.openai.chat")
        normal = self.open
        def background(request, **kwargs):
            if request.full_url.endswith("/wda/apps/state"):
                return io.BytesIO(json.dumps({"value": 2}).encode())
            return normal(request, **kwargs)
        with patch("urllib.request.build_opener", return_value=SimpleNamespace(open=background)):
            with self.assertRaisesRegex(IPhoneError, "foreground"):
                self.call("open", bundle="com.apple.shortcuts")

    def test_handle_ui_status_and_expired_session(self):
        self.write("state.json", {"status": "running", "deadline": time.time() - 1})
        self.assertEqual(self.call("status").data["session"]["status"], "expired")
        with self.assertRaisesRegex(IPhoneError, "expired"):
            self.call("observe")

    def test_handle_ui_opens_app_url_without_shortcut_bridge(self):
        url = "shortcuts://open-shortcut?name=Health%20Export"
        self.call("open", bundle="com.apple.shortcuts", url=url)
        self.assertTrue(self.http_calls[-2][0].endswith("/url"))
        self.assertEqual(json.loads(self.http_calls[-2][1]), {"bundleId": "com.apple.shortcuts", "url": url})
        for url in ["file:///private", "javascript:alert(1)", "data:text/plain,test", "not a URL", "foo://[", "foo:test\n"]:
            before = len(self.http_calls)
            with self.subTest(url=url), self.assertRaises(IPhoneError):
                self.call("open", bundle="com.apple.shortcuts", url=url)
            self.assertEqual(len(self.http_calls), before + 1)

    def test_handle_ui_start_rejects_duplicate_and_invalid_duration(self):
        with self.assertRaisesRegex(IPhoneError, "already exists"):
            self.call("start", duration=120)
        for duration in [0, 601]:
            with self.subTest(duration=duration), self.assertRaises(IPhoneError):
                self.call("start", duration=duration)

    def test_handle_ui_rejects_malformed_session_state(self):
        for deadline in [None, "tomorrow", float("nan"), float("inf")]:
            self.write("state.json", {"status": "running", "deadline": deadline})
            with self.subTest(deadline=deadline), self.assertRaises(IPhoneError):
                self.call("status")
        self.write("state.json", {"status": "running", "deadline": time.time() + 10})
        with self.assertRaisesRegex(IPhoneError, "supervisor identity"):
            self.call("observe")

    def test_handle_ui_stop_requests_supervisor_cleanup(self):
        def stopped(_):
            self.assertTrue((self.root / (self.nonce + ".stop")).exists())
            self.write("state.json", {"status": "stopped"})
        with patch("time.sleep", side_effect=stopped):
            self.assertEqual(self.call("stop").status, "completed")
        self.assertEqual(self.call("stop").status, "completed")

    def test_handle_ui_configure_copies_runner_without_private_paths_in_source(self):
        self.write("state.json", {"status": "stopped"})
        products = Path(self.temporary.name) / "products"
        app = products / "Debug" / "Runner.app"
        (app / "PlugIns" / "Test.xctest").mkdir(parents=True)
        definition = {"WebDriverAgentRunner": {
            "EnvironmentVariables": {}, "TestHostPath": "__TESTROOT__/Debug/Runner.app",
            "DependentProductPaths": ["__TESTROOT__/Debug/Runner.app", "__TESTROOT__/Debug/Runner.app/PlugIns/Test.xctest"]}}
        runner = products / "source.xctestrun"
        runner.write_bytes(plistlib.dumps(definition))
        result = self.call("configure", device="TEST-DEVICE", runner=str(runner), return_app="com.openai.chat")
        self.assertEqual(result.status, "completed")
        config = json.loads((self.root / "config.json").read_text())
        self.assertTrue(Path(config["runner"]).is_file())
        self.assertTrue((self.root / "runner/Debug/Runner.app/PlugIns/Test.xctest").is_dir())
        definition["WebDriverAgentRunner"]["TestHostPath"] = "__TESTROOT__/../outside"
        runner.write_bytes(plistlib.dumps(definition))
        with self.assertRaises(IPhoneError):
            self.call("configure", device="TEST-DEVICE", runner=str(runner), return_app="com.openai.chat")

    def test_supervise_does_not_shutdown_an_existing_controller(self):
        self.write(self.nonce + ".request.json", {"duration": 10, "timeout": 1})
        with patch("signal.signal"):
            ui.supervise(self.nonce)
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["status"], "failed")
        self.assertIn("already occupies", state["error"])
        self.assertFalse(any(url.endswith("/wda/shutdown") for url, _ in self.http_calls))

    def test_supervise_owns_and_cleans_up_a_finite_session(self):
        self.write(self.nonce + ".request.json", {"duration": 10, "timeout": 1})
        normal = self.open
        status_count = 0
        def starting(request, **kwargs):
            nonlocal status_count
            if request.full_url.endswith("/status"):
                status_count += 1
                if status_count == 1:
                    raise URLError(ConnectionRefusedError("not running"))
            return normal(request, **kwargs)
        child = SimpleNamespace(poll=lambda: None, wait=lambda timeout=None: 0, pid=100)
        def stop_after_ready(_):
            (self.root / (self.nonce + ".stop")).touch()
        with patch("signal.signal"), patch("subprocess.Popen", return_value=child), \
             patch("time.sleep", side_effect=stop_after_ready), \
             patch("urllib.request.build_opener", return_value=SimpleNamespace(open=starting)):
            ui.supervise(self.nonce)
        state = json.loads((self.root / "state.json").read_text())
        self.assertEqual(state["status"], "stopped")
        definition = plistlib.loads((self.root / "runner/active.xctestrun").read_bytes())
        self.assertEqual(definition["WebDriverAgentRunner"]["EnvironmentVariables"]["USE_IP"], self.address)
        self.assertTrue(any(url.endswith("/wda/shutdown") for url, _ in self.http_calls))
        capabilities = next(json.loads(data)["capabilities"]["alwaysMatch"]
                            for url, data in self.http_calls if url.endswith("/session"))
        self.assertFalse(capabilities["forceAppLaunch"])
        self.assertFalse(capabilities["shouldTerminateApp"])
        self.assertNotIn("bundleId", capabilities)

    def test_handle_ui_native_timeouts_are_integer_seconds(self):
        ui.handle_ui(argparse.Namespace(action="observe", timeout=2.5, dry_run=False))
        for command in self.native_calls:
            self.assertEqual(command[command.index("--timeout") + 1], "3")

    def test_handle_ui_serializes_native_device_commands(self):
        with (self.root / "device.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(IPhoneError, "Device service is busy"):
                ui.handle_ui(argparse.Namespace(action="observe", timeout=0.01, dry_run=False))
        self.assertEqual(self.native_calls, [])

    def test_build_parser_exposes_explicit_ui_commands(self):
        for command in [["ui", "start"], ["ui", "open", "com.apple.shortcuts"],
                        ["ui", "tap", "Select", "--snapshot", str(uuid.uuid4())], ["ui", "return"]]:
            args = build_parser().parse_args(command)
            self.assertIs(args.handler, ui.handle_ui)
