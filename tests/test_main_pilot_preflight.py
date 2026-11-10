"""The named pilot preflight must fail safely before touching installed files."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "openwrt_pilot_preflight.sh"


class MainPilotPreflightTests(unittest.TestCase):
    def run_with(self, transport="tcp", port="18443"):
        with tempfile.TemporaryDirectory(prefix="router-orchestrator-canary-") as folder:
            fake = Path(folder) / "uci"
            fake.write_text(
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  '-q get owrtremote.main.router_id') echo main ;;\n"
                "  '-q get owrtremote.main.router_name') echo Main-test ;;\n"
                "  '-q get owrtremote.main.vps_transport') printf '%s\\n' \"$TEST_TRANSPORT\" ;;\n"
                "  '-q get owrtremote.main.vps_port') printf '%s\\n' \"$TEST_PORT\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n"
            )
            fake.chmod(0o755)
            # A preflight never invokes jq parsing on a rejected transport/port.
            jq = Path(folder) / "jq"
            jq.write_text("#!/bin/sh\nexit 127\n")
            jq.chmod(0o755)
            return subprocess.run(
                ["sh", str(SCRIPT)],
                env={**os.environ, "PATH": folder + os.pathsep + os.environ["PATH"],
                     "TEST_TRANSPORT": transport, "TEST_PORT": port},
                text=True, capture_output=True, timeout=10,
            )

    def test_refuses_wss_pilot(self):
        p = self.run_with(transport="ws")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("only allows existing TCP", p.stderr)

    def test_refuses_unexpected_vps_port(self):
        p = self.run_with(port="443")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("unexpected legacy VPS port", p.stderr)

    def test_read_only_scope_stays_explicit(self):
        content = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("PREFLIGHT: READ-ONLY PASS", content)
        self.assertNotIn("uci -q set ", content)
        self.assertNotIn("uci commit ", content)
        self.assertNotIn("systemctl restart", content)
        self.assertNotIn("opkg install", content)
        self.assertNotIn("apk add", content)


if __name__ == "__main__":
    unittest.main()
