from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from iphone_cli.errors import IPhoneError
from iphone_cli.ocr import recognize_text


class OCRTests(unittest.TestCase):
    def test_recognize_text_uses_configured_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "screen.png"
            image.write_bytes(b"image")
            helper = root / "ocr"
            helper.write_text("#!/bin/sh\nprintf 'Settings\\nWi-Fi\\n'")
            helper.chmod(0o755)
            with patch.dict(os.environ, {"IPHONE_OCR_COMMAND": str(helper)}):
                self.assertEqual(recognize_text(image), "Settings\nWi-Fi")

    def test_recognize_text_rejects_missing_input(self):
        with self.assertRaisesRegex(IPhoneError, "OCR input is missing"):
            recognize_text(Path("/missing/screen.png"))

    def test_recognize_text_reports_helper_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "screen.png"
            image.write_bytes(b"image")
            helper = root / "ocr"
            helper.write_text("#!/bin/sh\necho 'bad image' >&2\nexit 1\n")
            helper.chmod(0o755)
            with patch.dict(os.environ, {"IPHONE_OCR_COMMAND": str(helper)}):
                with self.assertRaisesRegex(IPhoneError, "Screen OCR failed: bad image"):
                    recognize_text(image)


if __name__ == "__main__":
    unittest.main()
