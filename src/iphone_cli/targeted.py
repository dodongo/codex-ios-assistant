"""Live, app-bound element transactions without full-screen capture."""

import json
import math
import re
import time

from . import ui
from .errors import IPhoneError


CAPABILITIES = {
    "shouldUseCompactResponses": False,
    "elementResponseAttributes": "type,rect,enabled,displayed,label,selected,attribute/name,attribute/value",
    "waitForIdleTimeout": 0,
}

READINESS_SETTINGS = {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0}


class TargetedRequest:
    """Resolve one live control, dispatch at most once, and optionally verify state."""

    def __init__(self, args, path, body):
        try:
            if (not isinstance(args.target, str) or len(args.target) > 16384
                    or args.ensure is not None and (not isinstance(args.ensure, str) or len(args.ensure) > 16384)):
                raise ValueError("target and ensure must be bounded JSON objects")
            self.target = json.loads(args.target)
            self.ensure = json.loads(args.ensure) if args.ensure is not None else None
            if not isinstance(self.target, dict) or set(self.target) - {"bundleId", "using", "value", "point", "end"}:
                raise ValueError("invalid target fields")
            ui._bundle(self.target.get("bundleId"))
            if self.target.get("using") not in {"predicate string", "accessibility id", "class chain"}:
                raise ValueError("target requires a native element selector")
            if not isinstance(self.target.get("value"), str) or not 0 < len(self.target["value"]) <= 8192:
                raise ValueError("target requires a selector value")
            if args.ensure is not None and (not isinstance(self.ensure, dict) or not {"attribute", "equals"} <= set(self.ensure)
                    or set(self.ensure) - {"attribute", "equals", "target"}
                    or self.ensure["attribute"] not in {"value", "name", "label", "enabled", "visible", "selected"}
                    or not isinstance(self.ensure["equals"], (str, bool, int, float, type(None)))
                    or isinstance(self.ensure["equals"], float) and not math.isfinite(self.ensure["equals"])):
                raise ValueError("ensure requires an attribute and scalar equals value")
            self.verification_target = self.ensure.get("target", self.target) if self.ensure else self.target
            if self.verification_target is not self.target:
                if (not isinstance(self.verification_target, dict) or set(self.verification_target) != {"using", "value"}
                        or self.verification_target["using"] not in {"predicate string", "accessibility id", "class chain"}
                        or not isinstance(self.verification_target["value"], str)
                        or not 0 < len(self.verification_target["value"]) <= 8192):
                    raise ValueError("ensure target requires a native selector in the same app")
            if args.method != "POST" or args.scope != "session" or path not in {
                "/wda/tap", "/actions", "/element/{id}/click", "/element/{id}/value", "/element/{id}/clear",
            }:
                raise ValueError("targeted requests support session POST tap, actions, click, value, or clear")
            if getattr(args, "snapshot", None):
                raise ValueError("targeted requests use live element checks rather than snapshots")
            point = self.target.get("point")
            end = self.target.get("end")
            if "end" in self.target and (path != "/actions" or not isinstance(end, dict) or set(end) != {"x", "y"}
                    or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in end.values())):
                raise ValueError("end requires x/y fractions within the live target for a pointer swipe")
            if path in {"/wda/tap", "/actions"}:
                if (not isinstance(point, dict) or set(point) != {"x", "y"}
                        or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point.values())):
                    raise ValueError("targeted pointer requests require point x/y fractions within the live element rectangle")
                if body:
                    raise ValueError("targeted tap derives its coordinates from the live control")
            elif point is not None:
                raise ValueError("point is only used with coordinate or pointer actions")
        except (ValueError, TypeError, KeyError, RecursionError) as error:
            raise IPhoneError(f"Invalid targeted request: {error}") from error
        self.path, self.body = path, body

    def _remaining(self):
        remaining = self.until - time.monotonic()
        if remaining <= 0:
            raise IPhoneError("Targeted request deadline exceeded; no action will be replayed.")
        return remaining

    def _foreground(self, wait=False):
        while True:
            foreground = ui._http(self.address, "/wda/activeAppInfo", timeout=self._remaining())
            if not isinstance(foreground, dict) or not isinstance(foreground.get("bundleId"), str):
                raise IPhoneError("Invalid foreground app response.")
            if foreground["bundleId"] == self.target["bundleId"]:
                return
            if not wait:
                raise IPhoneError("Target app is not in the foreground; action was not replayed.")
            try:
                time.sleep(min(0.05, self._remaining()))
            except IPhoneError as error:
                raise IPhoneError("Target app did not become foreground before the deadline.") from error

    def _find(self, selector, missing_ok=False, wait_foreground=False):
        self._foreground(wait=wait_foreground)
        elements = ui._http(self.address, self.prefix + "/elements", {
            "using": selector["using"], "value": selector["value"],
        }, timeout=self._remaining())
        if elements == [] and missing_ok:
            return None
        if not isinstance(elements, list) or len(elements) != 1 or not isinstance(elements[0], dict):
            raise IPhoneError("Target must match exactly one live control; inspect the screen.")
        element = elements[0]
        identifier = element.get("element-6066-11e4-a52e-4f735466cecf")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", identifier):
            raise IPhoneError("Invalid target element identity.")
        if element.get("enabled") is not True or element.get("displayed") is not True:
            raise IPhoneError("Target is not verified visible and enabled.")
        return identifier, element

    def _value(self, element):
        attribute = self.ensure["attribute"]
        key = {"value": "attribute/value", "name": "attribute/name", "visible": "displayed"}.get(attribute, attribute)
        if key not in element:
            raise IPhoneError("Controller omitted target attributes; restart with the configured element response capabilities.")
        return element[key]

    def execute(self, address, prefix, timeout, state):
        """Run under the controller command lock, preserving the finite session deadline."""
        self.address, self.prefix = address, prefix
        self.until = time.monotonic() + min(timeout, max(0, state["deadline"] - time.time()))
        desired = self.ensure["equals"] if self.ensure else None
        while True:
            existing = self._find(self.verification_target, missing_ok=True, wait_foreground=True)
            self._remaining()
            before = self._value(existing[1]) if existing and self.ensure else None
            if self.ensure and existing and type(before) is type(desired) and before == desired:
                return {"changed": False, "verified": True, "before": before, "after": before}
            found = existing if self.verification_target is self.target else self._find(self.target, missing_ok=True)
            if found:
                identifier, element = found
                break
            time.sleep(min(0.05, self._remaining()))
        path = prefix + self.path.replace("{id}", identifier)
        body = self.body
        if self.path in {"/wda/tap", "/actions"}:
            rect = element.get("rect")
            if (not isinstance(rect, dict) or not {"x", "y", "width", "height"} <= rect.keys()
                    or any(type(rect[k]) not in (int, float) or not math.isfinite(rect[k]) for k in ("x", "y", "width", "height"))
                    or rect["width"] <= 0 or rect["height"] <= 0):
                raise IPhoneError("Target has no valid live rectangle.")
            body = {axis: rect[axis] + rect[size] * self.target["point"][axis]
                    for axis, size in (("x", "width"), ("y", "height"))}
            if any(not math.isfinite(v) or v < 0 for v in body.values()):
                raise IPhoneError("Target resolves to an invalid screen coordinate.")
            if self.path == "/actions":
                move = []
                if "end" in self.target:
                    end = {axis: rect[axis] + rect[size] * self.target["end"][axis]
                           for axis, size in (("x", "width"), ("y", "height"))}
                    if any(not math.isfinite(v) or v < 0 for v in end.values()):
                        raise IPhoneError("Swipe resolves to an invalid screen coordinate.")
                    move = [{"type": "pointerMove", "duration": 300, "origin": "viewport", **end}]
                body = {"actions": [{"type": "pointer", "id": "target", "parameters": {"pointerType": "touch"}, "actions": [
                    {"type": "pointerMove", "duration": 0, "origin": "viewport", **body},
                    {"type": "pointerDown", "button": 0}, {"type": "pause", "duration": 50},
                    *move,
                    {"type": "pointerUp", "button": 0},
                ]}]}
        self._foreground()
        current = ui._active()
        if current["nonce"] != state["nonce"] or current["session"] != state["session"]:
            raise IPhoneError("UI session changed; target was not dispatched.")
        ui._unlocked({}, self._remaining(), address)
        try:
            ui._http(address, path, body, timeout=self._remaining(), method="POST")
        except IPhoneError as error:
            raise IPhoneError(f"Targeted action outcome is unknown; action was not replayed. {error}") from error
        if self.ensure is None:
            return {"dispatched": True, "verified": False}
        try:
            while True:
                found = self._find(self.verification_target, missing_ok=True)
                self._remaining()
                after = self._value(found[1]) if found else None
                if found and type(after) is type(desired) and after == desired:
                    return {"changed": True, "verified": True, "before": before, "after": after}
                time.sleep(min(0.05, self._remaining()))
        except IPhoneError as error:
            raise IPhoneError(f"Targeted action was dispatched but its result was not verified; action was not replayed. {error}") from error
