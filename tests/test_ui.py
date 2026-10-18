import argparse
import base64
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
        self.http_methods = []
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
        self.http_methods.append(request.get_method())
        if isinstance(self.response, Exception):
            raise self.response
        if self.response is not None:
            return io.BytesIO(self.response)
        if request.full_url.endswith("/source"):
            value = self.source
        elif request.full_url.endswith("/screenshot"):
            value = base64.b64encode(b"\x89PNG\r\n\x1a\nPNG test payload").decode()
        elif request.full_url.endswith("/wda/locked"):
            value = self.locked
        elif request.full_url.endswith("/elements"):
            value = self.element_results
        elif request.full_url.endswith("/wda/apps/state"):
            value = 4
        elif request.full_url.endswith("/status"):
            value = {"ready": True}
        elif request.full_url.endswith("/session"):
            value = {"sessionId": "CREATED-SESSION"}
        elif request.full_url.endswith("/wda/shutdown"):
            return io.BytesIO(b"Shutting down")
        else:
            value = None
        return io.BytesIO(json.dumps({"value": value}).encode())

    def call(self, action, **values):
        return ui.handle_ui(argparse.Namespace(action=action, timeout=2, dry_run=False, **values))

    def request(self, method="GET", path="/orientation", **values):
        return self.call("request", method=method, path=path,
                         scope=values.pop("scope", "session"), data=values.pop("data", None), **values)

    def observe(self):
        return self.request("GET", "/source", observe=True)

    def test_request_prefers_direct_app_links_without_a_screen_target(self):
        self.request("POST", "/url", data=json.dumps({
            "url": "App-prefs:com.openai.chat", "bundleId": "com.apple.Preferences",
        }))
        self.assertTrue(self.http_calls[-1][0].endswith("/session/SESSION-ID/url"))
        self.assertEqual(len(self.http_calls), 2)

    def test_request_rejects_unsafe_app_links_before_contacting_device(self):
        for url in ["file:///private", "javascript:alert(1)", "data:text/plain,test", "not a URL", "foo://[", "foo:test\n"]:
            with self.subTest(url=url), self.assertRaises(IPhoneError):
                self.request("POST", "/url", data=json.dumps({"url": url}))
        self.assertEqual(self.http_calls, [])

    def test_mutation_can_return_its_post_action_observation(self):
        snapshot = self.observe().data["snapshot"]
        result = self.request("POST", "/wda/keys", data='{"value":["ChatGPT"]}', snapshot=snapshot, observe=True)
        self.assertEqual(result.status, "requested")
        self.assertNotEqual(result.data["snapshot"], snapshot)
        self.assertTrue(Path(result.data["screenshot"]).is_file())
        self.assertTrue(self.http_calls[-2][0].endswith("/screenshot"))
        self.assertTrue(self.http_calls[-1][0].endswith("/source"))

    def test_parser_exposes_one_device_command_interface(self):
        from contextlib import redirect_stderr
        args = build_parser().parse_args(["ui", "request", "POST", "/wda/keys", "--data", '{"value":["text"]}', "--observe", "--snapshot", self.nonce])
        self.assertEqual(args.action, "request")
        self.assertTrue(args.observe)
        for action in ("open", "tap", "observe", "return"):
            with self.subTest(action=action), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                build_parser().parse_args(["ui", action])

    def test_observation_reuses_then_rechecks_the_requested_xml_source(self):
        self.observe()
        self.assertEqual(sum(url.endswith("/source") for url, _ in self.http_calls), 2)
        self.assertTrue(self.http_calls[-2][0].endswith("/screenshot"))
        self.assertTrue(self.http_calls[-1][0].endswith("/source"))

    def test_observation_rejects_invalid_source_and_image_data(self):
        for source in ("<!DOCTYPE x><App/>", "<broken>"):
            self.source = source
            with self.subTest(source=source), self.assertRaisesRegex(IPhoneError, "observation failed"):
                self.observe()

    def test_observation_retries_capture_not_the_action_during_a_transition(self):
        original = ui._http
        screenshots = []
        settled = '<App><XCUIElementTypeSwitch name="Allow Notifications" value="0" visible="true"/></App>'

        def transitioning(address, path, *args, **kwargs):
            if path == "/screenshot":
                screenshots.append(path)
                self.source = settled
            return original(address, path, *args, **kwargs)

        with patch.object(ui, "_http", side_effect=transitioning):
            result = self.request("POST", "/url", data='{"url":"App-prefs:com.openai.chat","bundleId":"com.apple.Preferences"}', observe=True)
        self.assertEqual(len(screenshots), 2)
        self.assertEqual(sum(url.endswith("/url") for url, _ in self.http_calls), 1)
        self.assertEqual(result.data["elements"][0]["value"], "0")
        record = ui._read(self.root / "snapshots" / (result.data["snapshot"] + ".json"))
        self.assertEqual(record["digest"], hashlib.sha256(settled.encode()).hexdigest())

    def test_observation_stops_at_deadline_without_replaying_the_action(self):
        original = ui._http
        clock = [0]

        def changing(address, path, *args, **kwargs):
            if path == "/screenshot":
                clock[0] += 0.6
                self.source = f'<App name="{clock[0]}"/>'
            return original(address, path, *args, **kwargs)

        with patch.object(ui, "_http", side_effect=changing), patch.object(ui.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(IPhoneError, "observation failed.*deadline"):
                self.request("POST", "/url", data='{"url":"App-prefs:com.openai.chat"}', observe=True)
        self.assertEqual(sum(url.endswith("/url") for url, _ in self.http_calls), 1)
        self.assertFalse((self.root / "snapshots").exists())

    def test_observed_deep_links_await_native_idle_and_preserve_explicit_waits(self):
        for specified, expected in (({}, 2000), ({"idleTimeoutMs": 750}, 750)):
            body = {"url": "App-prefs:com.openai.chat", "bundleId": "com.apple.Preferences", **specified}
            self.request("POST", "/url", data=json.dumps(body), observe=True)
            sent = [json.loads(data) for url, data in self.http_calls if url.endswith("/url")][-1]
            self.assertEqual(sent["idleTimeoutMs"], expected)

    def test_wda_request_reads_are_session_scoped_without_a_full_observation(self):
        result = self.request(path="/source?format=json")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["value"], None)
        self.assertTrue(self.http_calls[-1][0].endswith("/session/SESSION-ID/source?format=json"))
        self.assertEqual(len(self.http_calls), 2)

    def test_wda_request_device_scope_uses_only_the_owned_controller(self):
        result = self.request(path="/status", scope="device")
        self.assertTrue(self.http_calls[-1][0].endswith(":8100/status"))
        self.assertEqual(result.data["value"], {"ready": True})

    def test_wda_request_element_queries_do_not_consume_a_snapshot(self):
        result = self.request("POST", "/elements", data='{"using":"accessibility id","value":"Search"}')
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["value"], self.element_results)
        self.assertEqual(len(self.http_calls), 2)

    def test_wda_request_typing_consumes_one_fresh_observation(self):
        snapshot = self.observe().data["snapshot"]
        result = self.request("POST", "/wda/keys", data='{"value":["ChatGPT"]}', snapshot=snapshot)
        self.assertEqual(result.status, "requested")
        self.assertTrue(self.http_calls[-1][0].endswith("/session/SESSION-ID/wda/keys"))
        self.assertEqual(json.loads(self.http_calls[-1][1]), {"value": ["ChatGPT"]})
        self.assertFalse((self.root / "snapshots" / (snapshot + ".json")).exists())
        with self.assertRaises(IPhoneError):
            self.request("POST", "/wda/keys", data='{"value":["ChatGPT"]}', snapshot=snapshot)

    def test_wda_request_mutations_require_an_observation_before_contacting_device(self):
        for method, path in [("POST", "/wda/scroll"), ("PUT", "/custom"), ("DELETE", "/"), ("GET", "/wda/shutdown")]:
            with self.subTest(method=method, path=path), self.assertRaisesRegex(IPhoneError, "--snapshot"):
                self.request(method, path)
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.native_calls, [])

    def test_wda_request_rejects_changed_screen_before_dispatch(self):
        snapshot = self.observe().data["snapshot"]
        self.source = '<App name="Changed"/>'
        with self.assertRaisesRegex(IPhoneError, "Screen changed"):
            self.request("POST", "/wda/scroll", data='{"direction":"down"}', snapshot=snapshot)
        self.assertFalse(any(url.endswith("/wda/scroll") for url, _ in self.http_calls))

    def test_wda_request_rejects_unsafe_paths_and_foreign_session_ids(self):
        for path in ["https://example.com/status", "//example.com/status", "/../status", "/%2e%2e/status",
                     "/%252e%252e/status", "/wda\\status", "/wda//status", "/status#fragment",
                     "/session/OTHER-ID/source", "/%0astatus", "/status\r\nX:evil"]:
            with self.subTest(path=path), self.assertRaises(IPhoneError):
                self.request(path=path)
        self.assertEqual(self.http_calls, [])

    def test_wda_request_rejects_invalid_json_without_contacting_device(self):
        for data in ["{", "[]", "null", '{"value":NaN}', '{"value":Infinity}']:
            with self.subTest(data=data), self.assertRaises(IPhoneError):
                self.request("POST", "/elements", data=data)
        self.assertEqual(self.http_calls, [])

    def test_wda_request_cannot_enable_automatic_permission_approval(self):
        for data in [
            '{"settings":{"defaultAlertAction":"accept"}}',
            '{"settings":{"appium:defaultAlertAction":" ACCEPT "}}',
            '{"capabilities":{"alwaysMatch":{"autoAcceptAlerts":true}}}',
        ]:
            with self.subTest(data=data), self.assertRaisesRegex(IPhoneError, "Automatic permission"):
                self.request("POST", "/appium/settings", data=data, snapshot=str(uuid.uuid4()))
        self.assertEqual(self.http_calls, [])

    def test_wda_request_reports_unknown_mutation_outcome_without_replay(self):
        snapshot = self.observe().data["snapshot"]
        normal = self.open

        def fail(request, **kwargs):
            if request.full_url.endswith("/wda/keys"):
                self.http_calls.append((request.full_url, request.data))
                raise URLError("connection lost")
            return normal(request, **kwargs)

        with patch("urllib.request.build_opener", return_value=SimpleNamespace(open=fail)):
            with self.assertRaisesRegex(IPhoneError, "outcome is unknown"):
                self.request("POST", "/wda/keys", snapshot=snapshot)
        self.assertEqual(sum(url.endswith("/wda/keys") for url, _ in self.http_calls), 1)
        self.assertFalse((self.root / "snapshots" / (snapshot + ".json")).exists())

    def test_wda_request_saves_response_as_a_new_private_json_file(self):
        output = self.root / "response.json"
        self.request(path="/status", scope="device", output=str(output))
        self.assertEqual(json.loads(output.read_text()), {"ready": True})
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        previous = len(self.http_calls)
        with self.assertRaisesRegex(IPhoneError, "new file"):
            self.request(path="/status", scope="device", output=str(output))
        self.assertEqual(len(self.http_calls), previous)

    def test_wda_request_session_deletion_requests_owned_runner_cleanup(self):
        snapshot = self.observe().data["snapshot"]
        self.request("DELETE", "/", snapshot=snapshot)
        self.assertTrue((self.root / (self.nonce + ".stop")).exists())
        self.assertLessEqual(ui._read(self.root / "state.json")["deadline"], time.time())
        self.assertEqual(self.http_methods[-1], "DELETE")

    def test_wda_request_session_creation_updates_owned_session_identity(self):
        snapshot = self.observe().data["snapshot"]
        self.request("POST", "/session", scope="device", snapshot=snapshot,
                     data='{"capabilities":{"alwaysMatch":{}}}')
        self.assertEqual(ui._read(self.root / "state.json")["session"], "CREATED-SESSION")

    def test_session_creation_observes_the_new_session(self):
        snapshot = self.observe().data["snapshot"]
        self.request("POST", "/session", scope="device", snapshot=snapshot, observe=True,
                     data='{"capabilities":{"alwaysMatch":{}}}')
        self.assertTrue(self.http_calls[-1][0].endswith("/session/CREATED-SESSION/source"))

    def test_session_ending_request_rejects_post_action_observation(self):
        with self.assertRaisesRegex(IPhoneError, "session-ending"):
            self.request("DELETE", "/", snapshot=self.nonce, observe=True)
        self.assertEqual(self.http_calls, [])

    def test_wda_request_dry_run_and_parser_support_the_full_transport(self):
        args = build_parser().parse_args(["ui", "request", "DELETE", "/wda/simulatedLocation",
                                         "--scope", "device", "--snapshot", self.nonce, "--dry-run"])
        result = ui.handle_ui(args)
        self.assertEqual(result.status, "dry-run")
        self.assertEqual(result.data["method"], "DELETE")
        self.assertEqual(result.data["scope"], "device")
        self.assertEqual(self.http_calls, [])

    def test_handle_ui_dry_run_does_not_touch_device(self):
        result = ui.handle_ui(argparse.Namespace(action="start", dry_run=True, timeout=30))
        self.assertEqual(result.status, "dry-run")
        self.assertEqual(self.native_calls, [])
        self.assertEqual(self.http_calls, [])

    def test_handle_ui_rejects_invalid_timeout(self):
        for timeout in [float("nan"), float("inf"), 0, 301]:
            with self.subTest(timeout=timeout), self.assertRaises(IPhoneError):
                ui.handle_ui(argparse.Namespace(action="status", timeout=timeout))



    def test_handle_ui_rejects_locked_device_before_action(self):
        self.locked = True
        with self.assertRaisesRegex(IPhoneError, "Unlock"):
            self.request("POST", "/wda/apps/activate", data='{"bundleId":"com.apple.shortcuts"}')
        self.assertEqual(len(self.http_calls), 1)
        self.assertTrue(self.http_calls[0][0].endswith("/wda/locked"))

    def test_handle_ui_rejects_unknown_controller_lock_state(self):
        self.response = json.dumps({"value": None}).encode()
        with self.assertRaisesRegex(IPhoneError, "Cannot verify"):
            self.observe()

    def test_handle_ui_rejects_changed_tunnel(self):
        self.address = "fd12:3456::2"
        with self.assertRaisesRegex(IPhoneError, "tunnel changed"):
            self.observe()

    def test_handle_ui_rejects_lan_or_public_endpoint(self):
        for address in ["192.168.1.1", "2001:4860::1", "::1", None]:
            self.address = address
            with self.subTest(address=address), self.assertRaises(IPhoneError):
                self.observe()

    def test_handle_ui_rejects_stale_changed_and_malformed_snapshots(self):
        for change in [{"created": time.time() - 31}, {"created": float("nan")},
                       {"nonce": "other"}, {"session": "other"}, {"digest": "wrong"}, {"elements": None}]:
            result = self.observe()
            snapshot = result.data["snapshot"]
            path = self.root / "snapshots" / (snapshot + ".json")
            record = json.loads(path.read_text())
            record.update(change)
            path.write_text(json.dumps(record))
            with self.subTest(change=change), self.assertRaises(IPhoneError):
                self.request("POST", "/wda/keys", snapshot=snapshot)

    def test_handle_ui_rejects_path_traversal_snapshot(self):
        with self.assertRaises(IPhoneError):
            self.request("POST", "/wda/keys", snapshot="../../config")





    def test_handle_ui_redacts_secure_values_and_limits_output(self):
        self.source = '<App><XCUIElementTypeSecureTextField name="Password" value="private" visible="true"/>' + ''.join(
            f'<Text name="item-{n}" visible="true"/>' for n in range(201)) + '</App>'
        result = self.observe()
        self.assertNotIn("value", result.data["elements"][0])
        self.assertEqual(len(result.data["elements"]), 200)
        self.assertTrue(result.data["truncated"])

    def test_handle_ui_rejects_xml_declarations_and_invalid_responses(self):
        for response in [b"not json", b"{}", json.dumps({"value": "<!DOCTYPE x><App/>"}).encode(),
                         json.dumps({"value": "<broken>"}).encode(), b"a" * (ui.MAX_RESPONSE + 1),
                         json.dumps({"value": {"error": "bad", "message": "failure"}}).encode()]:
            self.response = response
            with self.subTest(length=len(response)), self.assertRaises(IPhoneError):
                self.observe()

    def test_handle_ui_surfaces_http_and_connection_failures(self):
        for error in [URLError("unreachable"), HTTPError("url", 404, "missing", {}, io.BytesIO(b"no such element"))]:
            self.response = error
            with self.subTest(error=error), self.assertRaises(IPhoneError):
                self.observe()


    def test_handle_ui_status_and_expired_session(self):
        self.write("state.json", {"status": "running", "deadline": time.time() - 1})
        self.assertEqual(self.call("status").data["session"]["status"], "expired")
        with self.assertRaisesRegex(IPhoneError, "expired"):
            self.observe()


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
            self.observe()

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
        ui.handle_ui(argparse.Namespace(action="request", method="GET", path="/source", scope="session", data=None, observe=True, timeout=2.5, dry_run=False))
        for command in self.native_calls:
            self.assertEqual(command[command.index("--timeout") + 1], "3")

    def test_handle_ui_serializes_native_device_commands(self):
        with (self.root / "device.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(IPhoneError, "Device service is busy"):
                ui.handle_ui(argparse.Namespace(action="request", method="GET", path="/source", scope="session", data=None, observe=True, timeout=0.01, dry_run=False))
        self.assertEqual(self.native_calls, [])
