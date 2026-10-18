import argparse
import copy
import json
import time
import unittest
from unittest.mock import patch

from iphone_cli import targeted, ui
from iphone_cli.errors import IPhoneError


TARGET = {"bundleId": "com.example.App", "using": "predicate string",
          "value": 'name == "Example" AND type == "XCUIElementTypeSwitch"', "point": {"x": 0.9, "y": 0.5}}


class TargetedTests(unittest.TestCase):
    def setUp(self):
        self.value = "0"
        self.calls = []
        self.clock = 0
        self.state = {"nonce": "NONCE", "session": "SESSION", "deadline": time.time() + 60}
        self.controls = [{"element-6066-11e4-a52e-4f735466cecf": "ELEMENT-ID", "type": "XCUIElementTypeSwitch",
                          "enabled": True, "displayed": True, "attribute/value": "0",
                          "rect": {"x": 10, "y": 20, "width": 100, "height": 40}}]
        self.foreground = "com.example.App"
        self.change = True
        self.failure = False
        self.patches = [patch.object(ui, "_http", side_effect=self.http), patch.object(ui, "_active", return_value=self.state),
                        patch.object(ui, "_unlocked"), patch.object(targeted.time, "sleep")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def request(self, **changes):
        args = dict(target=json.dumps(TARGET), ensure='{"attribute":"value","equals":"1"}',
                    method="POST", scope="session", snapshot=None, observe=False)
        args.update(changes)
        return targeted.TargetedRequest(argparse.Namespace(**args), "/wda/tap", {})

    def http(self, address, path, body=None, **kwargs):
        self.calls.append((path, body))
        self.clock += 0.1
        if path == "/wda/activeAppInfo":
            return {"bundleId": self.foreground}
        if path.endswith("/elements"):
            result = copy.deepcopy(self.controls)
            for e in result:
                e["attribute/value"] = self.value
            return result
        if path.endswith(("/wda/tap", "/actions", "/click", "/value", "/clear")):
            if self.failure:
                raise IPhoneError("connection lost")
            if self.change:
                self.value = "1"
            return None
        self.fail("Unexpected request: " + path)

    def execute(self, request=None, timeout=3):
        return (request or self.request()).execute("unix:private", "/session/SESSION", timeout, self.state)

    def test_resolves_taps_and_verifies_without_full_screen_capture(self):
        value = self.execute()
        self.assertEqual(value, {"before": "0", "after": "1", "changed": True, "verified": True})
        actions = [body for path, body in self.calls if path.endswith("/wda/tap")]
        self.assertEqual(actions, [{"x": 100, "y": 40}])
        self.assertFalse(any(path.endswith(("/source", "/screenshot")) for path, _ in self.calls))

    def test_matching_state_is_a_verified_noop(self):
        self.value = "1"
        self.assertFalse(self.execute()["changed"])
        self.assertFalse(any(path.endswith("/wda/tap") for path, _ in self.calls))

    def test_pointer_actions_use_the_same_live_target_and_verification(self):
        args = argparse.Namespace(target=json.dumps(TARGET), ensure='{"attribute":"value","equals":"1"}',
                                  method="POST", scope="session", snapshot=None, observe=False)
        result = self.execute(targeted.TargetedRequest(args, "/actions", {}))
        self.assertTrue(result["verified"])
        body = next(body for path, body in self.calls if path.endswith("/actions"))
        self.assertEqual(body["actions"][0]["actions"][0]["x"], 100)
        self.assertEqual(body["actions"][0]["actions"][-1]["type"], "pointerUp")

    def test_element_routes_resolve_identity_and_preserve_the_payload(self):
        target = {k: v for k, v in TARGET.items() if k != "point"}
        for suffix in ("click", "value", "clear"):
            self.value = "0"
            args = argparse.Namespace(target=json.dumps(target), ensure='{"attribute":"value","equals":"1"}',
                                      method="POST", scope="session", snapshot=None, observe=False)
            body = {"value": ["exact text"]} if suffix == "value" else {}
            self.assertTrue(self.execute(targeted.TargetedRequest(args, "/element/{id}/" + suffix, body))["verified"])
            self.assertIn(("/session/SESSION/element/ELEMENT-ID/" + suffix, body), self.calls)

    def test_lock_before_dispatch_prevents_the_write(self):
        with patch.object(ui, "_unlocked", side_effect=IPhoneError("Unlock the phone")):
            with self.assertRaisesRegex(IPhoneError, "Unlock"):
                self.execute()
        self.assertFalse(any(path.endswith("/wda/tap") for path, _ in self.calls))

    def test_ambiguous_hidden_disabled_and_invalid_targets_do_not_dispatch(self):
        base = copy.deepcopy(self.controls)
        for controls in ([], base * 2, [{**base[0], "enabled": False}], [{**base[0], "displayed": False}],
                         [{**base[0], "rect": {"x": 0}}], [{**base[0], "element-6066-11e4-a52e-4f735466cecf": "../escape"}]):
            self.controls = controls
            with self.subTest(controls=controls), self.assertRaises(IPhoneError):
                self.execute()
        self.assertFalse(any(path.endswith("/wda/tap") for path, _ in self.calls))

    def test_wrong_app_and_changed_session_do_not_dispatch(self):
        self.foreground = "com.example.Other"
        with self.assertRaisesRegex(IPhoneError, "foreground"):
            self.execute()
        self.foreground = TARGET["bundleId"]
        with patch.object(ui, "_active", return_value={"nonce": "OTHER", "session": "SESSION"}):
            with self.assertRaisesRegex(IPhoneError, "session changed"):
                self.execute()
        self.assertFalse(any(path.endswith("/wda/tap") for path, _ in self.calls))

    def test_uncertain_dispatch_is_never_replayed(self):
        self.failure = True
        with self.assertRaisesRegex(IPhoneError, "outcome is unknown"):
            self.execute()
        self.assertEqual(sum(path.endswith("/wda/tap") for path, _ in self.calls), 1)

    def test_verification_polls_only_reads_until_deadline(self):
        self.change = False
        with patch.object(targeted.time, "monotonic", side_effect=lambda: self.clock):
            with self.assertRaisesRegex(IPhoneError, "not verified"):
                self.execute(timeout=1)
        self.assertEqual(sum(path.endswith("/wda/tap") for path, _ in self.calls), 1)

    def test_invalid_options_fail_before_contacting_device(self):
        for options in ({"target": "[]"}, {"target": "{}"}, {"ensure": "null"}, {"ensure": '{"attribute":"value","equals":[]}'},
                        {"scope": "device"}, {"method": "DELETE"}, {"snapshot": "UUID"}, {"observe": True},
                        {"target": json.dumps({**TARGET, "point": {"x": 2, "y": 0}})}):
            with self.subTest(options=options), self.assertRaises(IPhoneError):
                self.request(**options)
        self.assertEqual(self.calls, [])

    def test_expired_session_does_not_contact_device(self):
        self.state["deadline"] = time.time() - 1
        with self.assertRaisesRegex(IPhoneError, "deadline"):
            self.execute()
        self.assertEqual(self.calls, [])
