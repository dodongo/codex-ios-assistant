"""Private transport adapter for the existing iMessage → Shortcuts bridge."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from urllib.error import URLError
from urllib.request import urlopen
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .errors import IPhoneError
from .config import CONFIG_FILE, file_values, receiver_url, sender_socket


OperationKind = Literal[
    "hola",
    "screen-capture",
    "clipboard-read",
    "alarm-read",
]
BRIDGE_MODULE = "iphone_cli.bridge"


@dataclass(frozen=True)
class Operation:
    resource: str
    action: str
    kind: OperationKind
    arguments: tuple[str, ...] = ()
    summary: str = ""
    url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Result:
    resource: str
    action: str
    status: str
    summary: str
    data: dict[str, Any]


def bridge_command(action: str) -> list[str]:
    return [sys.executable, "-m", BRIDGE_MODULE, action]


def command_from_environment(name: str, default: str | list[str]) -> list[str]:
    configured = os.environ.get(name)
    if configured:
        return shlex.split(configured)
    return list(default) if isinstance(default, list) else shlex.split(default)


def command_for(operation: Operation) -> list[str]:
    if operation.kind == "hola":
        return [
            *command_from_environment("IPHONE_IMSG_COMMAND", bridge_command("send")),
            "hola",
            *operation.arguments,
        ]
    if operation.kind == "screen-capture":
        return command_from_environment(
            "IPHONE_SCREENSHOT_COMMAND", bridge_command("screenshot")
        )
    if operation.kind == "clipboard-read":
        return command_from_environment(
            "IPHONE_CLIPBOARD_COMMAND", bridge_command("clipboard")
        )
    if operation.kind == "alarm-read":
        return command_from_environment("IPHONE_ALARM_COMMAND", bridge_command("alarms"))
    raise IPhoneError(f"Unsupported operation kind: {operation.kind}")


def preview(operation: Operation) -> str:
    return shlex.join(command_for(operation))


def _run(command: list[str], *, timeout: float, environment: dict[str, str]) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=environment,
        )
    except FileNotFoundError as error:
        raise IPhoneError(f"Required helper is not installed: {command[0]}") from error
    except subprocess.TimeoutExpired as error:
        raise IPhoneError(f"Timed out after {timeout:g}s while running {command[0]}.") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise IPhoneError(f"{command[0]} failed: {detail}")
    return completed.stdout


def execute(
    operation: Operation,
    *,
    dry_run: bool = False,
    timeout: float = 30,
    output: str | None = None,
) -> Result:
    command = command_for(operation)
    common = {
        "kind": operation.kind,
        "command": command,
        **operation.metadata,
    }
    if operation.url:
        common["url"] = operation.url

    if dry_run:
        return Result(
            resource=operation.resource,
            action=operation.action,
            status="dry-run",
            summary=preview(operation),
            data=common,
        )

    environment = os.environ.copy()
    environment["SCREENSHOT_TIMEOUT"] = str(max(1, int(timeout)))
    environment["CLIPBOARD_TIMEOUT"] = str(max(1, int(timeout)))
    environment["ALARM_TIMEOUT"] = str(max(1, int(timeout)))
    stdout = _run(command, timeout=timeout + 5, environment=environment)

    if operation.kind == "hola":
        # The legacy bridge acknowledges that Messages accepted the command,
        # but it does not yet return a phone-side execution receipt.
        return Result(
            resource=operation.resource,
            action=operation.action,
            status="requested",
            summary=operation.summary,
            data=common,
        )

    if operation.kind == "clipboard-read":
        return Result(
            resource=operation.resource,
            action=operation.action,
            status="completed",
            summary=stdout,
            data={**common, "text": stdout},
        )

    if operation.kind == "alarm-read":
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise IPhoneError("Alarm helper returned invalid JSON.") from error
        alarms = payload.get("alarms") if isinstance(payload, dict) else None
        if not isinstance(alarms, list) or not all(isinstance(alarm, dict) for alarm in alarms):
            raise IPhoneError("Alarm helper response must contain an alarms array.")
        if alarms:
            lines = []
            for alarm in alarms:
                time_value = str(alarm.get("time") or "Unknown time")
                label = str(alarm.get("label") or "Alarm")
                repeat_days = str(alarm.get("repeat_days") or "").strip()
                line = f"{time_value} — {label}"
                if repeat_days and repeat_days.lower() not in {"never", "none"}:
                    line += f" ({repeat_days})"
                lines.append(line)
            summary = "\n".join(lines)
        else:
            summary = "No active alarms."
        return Result(
            resource=operation.resource,
            action=operation.action,
            status="completed",
            summary=summary,
            data={**common, "alarms": alarms},
        )

    source = Path(stdout.strip()).expanduser()
    if not source.is_file():
        raise IPhoneError(f"Screenshot helper returned a missing file: {source}")
    final_path = source.resolve()
    if output:
        destination = Path(output).expanduser()
        if destination.is_dir():
            destination /= source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        final_path = destination.resolve()
    return Result(
        resource=operation.resource,
        action=operation.action,
        status="completed",
        summary=str(final_path),
        data={**common, "path": str(final_path)},
    )


def dependency_report() -> list[dict[str, object]]:
    report: list[dict[str, object]] = []
    configured = file_values()
    required_names = (
        "IPHONE_MSG_TARGET",
        "IPHONE_PUBLIC_URL",
        "IPHONE_RECEIVER_TOKEN",
    )
    configuration_ready = CONFIG_FILE.is_file() and all(
        os.environ.get(name, configured.get(name, "")).strip() for name in required_names
    )
    report.append(
        {
            "name": "Private config",
            "required": True,
            "available": configuration_ready,
            "command": str(CONFIG_FILE),
            "path": str(CONFIG_FILE) if CONFIG_FILE.is_file() else None,
        }
    )

    socket_path = sender_socket()
    report.append(
        {
            "name": "Messages sender",
            "required": True,
            "available": socket_path.exists() and socket_path.is_socket(),
            "command": str(socket_path),
            "path": str(socket_path) if socket_path.exists() else None,
        }
    )

    receiver_available = False
    try:
        with urlopen(receiver_url() + "/health", timeout=1) as response:
            receiver_available = (
                response.status == 200
                and b"codex-ios-assistant receiver up" in response.read(256)
            )
    except (OSError, URLError):
        pass
    report.append(
        {
            "name": "Local receiver",
            "required": True,
            "available": receiver_available,
            "command": receiver_url(),
            "path": receiver_url() if receiver_available else None,
        }
    )

    specifications = [
        ("Contacts lookup", "IPHONE_CONTACTS_COMMAND", "contacts", False),
        ("Messages history", "IPHONE_HISTORY_IMSG_COMMAND", "imsg", False),
    ]
    for label, variable, default, required in specifications:
        command = command_from_environment(variable, default)
        executable = command[0]
        if "/" in executable:
            resolved = str(Path(executable).expanduser().resolve()) if Path(executable).expanduser().exists() else None
        else:
            resolved = shutil.which(executable)
        report.append(
            {
                "name": label,
                "required": required,
                "available": resolved is not None,
                "command": executable,
                "path": resolved,
            }
        )
    return report
