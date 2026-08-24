"""Local text recognition for iPhone screenshots."""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess

from .errors import IPhoneError


VISION_HELPER = Path(__file__).with_name("vision_ocr.swift")


def _command(image: Path) -> list[str]:
    configured = os.environ.get("IPHONE_OCR_COMMAND")
    if configured:
        return [*shlex.split(configured), str(image)]
    return ["/usr/bin/xcrun", "swift", str(VISION_HELPER), str(image)]


def recognize_text(image: Path, *, timeout: int = 30) -> str:
    """Return text recognized locally with Apple's Vision framework."""
    if not image.is_file():
        raise IPhoneError(f"OCR input is missing: {image}")
    try:
        completed = subprocess.run(
            _command(image),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise IPhoneError(f"OCR helper is not installed: {error.filename}") from error
    except subprocess.TimeoutExpired as error:
        raise IPhoneError(f"Timed out after {timeout}s while recognizing screen text.") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise IPhoneError(f"Screen OCR failed: {detail}")
    return completed.stdout.strip()
