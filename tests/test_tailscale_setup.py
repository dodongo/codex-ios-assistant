import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONFIGURE = ROOT / "scripts" / "configure"
SETUP = ROOT / "scripts" / "setup-tailscale"
ORIGIN = "https://test-mac.example-tailnet.ts.net"


def write_fake_tailscale(directory: Path) -> Path:
    executable = directory / "tailscale"
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = \"status --json\" ]; then\n"
        "  printf '%s\\n' '{\"BackendState\":\"Running\",\"Self\":"
        "{\"DNSName\":\"test-mac.example-tailnet.ts.net.\"}}'\n"
        "  exit 0\n"
        "fi\n"
        "if [ \"$1 $2 $3\" = \"serve status --json\" ]; then\n"
        "  if [ -n \"$TAILSCALE_SERVE_STATUS\" ]; then\n"
        "    printf '%s\\n' \"$TAILSCALE_SERVE_STATUS\"\n"
        "  else\n"
        "    printf '%s\\n' '{}'\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "printf '%s\\n' \"$*\" >> \"$TAILSCALE_TEST_LOG\"\n"
    )
    executable.chmod(0o755)
    return executable


def environment(directory: Path) -> dict[str, str]:
    result = os.environ.copy()
    result["IOS_ASSISTANT_CONFIG_DIR"] = str(directory / "config")
    result["PATH"] = f"{directory}:{result['PATH']}"
    result["TAILSCALE_TEST_LOG"] = str(directory / "tailscale.log")
    return result


class TailscaleSetupTests(unittest.TestCase):
    def test_configure_derives_tailscale_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_fake_tailscale(directory)
            result = subprocess.run(
                [str(CONFIGURE), "--target", "you@example.com"],
                cwd=ROOT,
                env=environment(directory),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            config = (directory / "config" / "config.env").read_text()
            self.assertIn(f'IPHONE_PUBLIC_URL="{ORIGIN}"', config)
            self.assertNotIn("replace-with", config)

    def test_setup_configures_private_loopback_proxy(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_fake_tailscale(directory)
            config_dir = directory / "config"
            config_dir.mkdir()
            (config_dir / "config.env").write_text(
                'IPHONE_MSG_TARGET="you@example.com"\n'
                f'IPHONE_PUBLIC_URL="{ORIGIN}"\n'
                'IPHONE_RECEIVER_TOKEN="abcdefghijklmnopqrstuvwxyz123456"\n'
            )
            result = subprocess.run(
                [str(SETUP)],
                cwd=ROOT,
                env=environment(directory),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            calls = (directory / "tailscale.log").read_text().splitlines()
            self.assertEqual(calls[0], "serve --bg http://127.0.0.1:8787")
            self.assertEqual(calls[1], "serve status")

    def test_setup_rejects_a_different_origin(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_fake_tailscale(directory)
            config_dir = directory / "config"
            config_dir.mkdir()
            (config_dir / "config.env").write_text(
                'IPHONE_MSG_TARGET="you@example.com"\n'
                'IPHONE_PUBLIC_URL="https://other.example-tailnet.ts.net"\n'
                'IPHONE_RECEIVER_TOKEN="abcdefghijklmnopqrstuvwxyz123456"\n'
            )
            result = subprocess.run(
                [str(SETUP)],
                cwd=ROOT,
                env=environment(directory),
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("this Mac's Tailscale origin", result.stderr)
            self.assertFalse((directory / "tailscale.log").exists())

    def test_setup_preserves_an_unrelated_serve_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            write_fake_tailscale(directory)
            config_dir = directory / "config"
            config_dir.mkdir()
            (config_dir / "config.env").write_text(
                'IPHONE_MSG_TARGET="you@example.com"\n'
                f'IPHONE_PUBLIC_URL="{ORIGIN}"\n'
                'IPHONE_RECEIVER_TOKEN="abcdefghijklmnopqrstuvwxyz123456"\n'
            )
            env = environment(directory)
            env["TAILSCALE_SERVE_STATUS"] = (
                '{"Web":{"test-mac.example-tailnet.ts.net:443":{"Handlers":'
                '{"/":{"Proxy":"http://127.0.0.1:3000"}}}}}'
            )
            result = subprocess.run(
                [str(SETUP)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing to replace it", result.stderr)
            self.assertFalse((directory / "tailscale.log").exists())


if __name__ == "__main__":
    unittest.main()
