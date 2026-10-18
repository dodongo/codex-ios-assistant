#!/usr/bin/env python3
"""Check the public Shortcut template's structure and secret-free placeholders."""

from __future__ import annotations

import plistlib
from pathlib import Path
import sys


PUBLIC_PLACEHOLDER = "__IOS_ASSISTANT_PUBLIC_URL__"
TOKEN_PLACEHOLDER = "__IOS_ASSISTANT_RECEIVER_TOKEN__"
FORBIDDEN = ("@gmail.com", "/Users/", "trycloudflare.com")


def walk(value: object):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("shortcut/actions.template.plist")
    with path.open("rb") as source:
        actions = plistlib.load(source)
    if not isinstance(actions, list) or len(actions) != 70:
        raise SystemExit(f"expected 70 Shortcut actions, found {len(actions) if isinstance(actions, list) else 'non-list'}")

    strings = [value for value in walk(actions) if isinstance(value, str)]
    public_count = sum(value.count(PUBLIC_PLACEHOLDER) for value in strings)
    token_count = sum(value.count(TOKEN_PLACEHOLDER) for value in strings)
    if public_count != 2 or token_count != 2:
        raise SystemExit("expected two public URL and two receiver-token placeholders")
    folded = "\n".join(strings).casefold()
    for forbidden in FORBIDDEN:
        if forbidden.casefold() in folded:
            raise SystemExit(f"private-looking value remains in template: {forbidden}")
    if "hola say" in folded or "is.workflow.actions.speaktext" in folded:
        raise SystemExit("unsupported legacy speech branch remains in the Shortcut template")
    for command in ("controlcenter", "openurl", "screentext", "screenshot", "homescreen"):
        if "hola " + command in folded:
            raise SystemExit(f"WebDriverAgent counterpart remains in the Shortcut: {command}")
    literal_web_origins = [
        value
        for value in strings
        if value.startswith("https://") and PUBLIC_PLACEHOLDER not in value
    ]
    if literal_web_origins:
        raise SystemExit("literal HTTPS origin remains in the Shortcut template")

    seen: set[str] = set()
    groups: list[str] = []
    for index, action in enumerate(actions):
        parameters = action.get("WFWorkflowActionParameters", {})
        for value in walk(parameters):
            if isinstance(value, dict) and value.get("Type") == "ActionOutput":
                output_uuid = value.get("OutputUUID")
                if output_uuid not in seen:
                    raise SystemExit(f"action {index} has unresolved output reference {output_uuid}")
        mode = parameters.get("WFControlFlowMode")
        group = parameters.get("GroupingIdentifier")
        if mode == 0:
            groups.append(group)
        elif mode == 2:
            if not groups or groups.pop() != group:
                raise SystemExit(f"unbalanced control-flow group at action {index}")
        action_uuid = parameters.get("UUID")
        if action_uuid:
            seen.add(action_uuid)
    if groups:
        raise SystemExit("unclosed Shortcut control-flow groups")
    print(f"validated {len(actions)} sanitized Shortcut actions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
