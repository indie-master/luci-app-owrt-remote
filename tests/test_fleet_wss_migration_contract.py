"""Guardrails for the fleet-wide WSS migration prototype."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
TX = ROOT / "tools/router_wss_pilot_transaction.sh"
STAGE = ROOT / "tools/router_wss_pilot_stage.sh"
INIT = ROOT / "tools/router_wss_recovery.init"


class FleetMigrationPrototypeTests(unittest.TestCase):
    def test_shell_syntax(self):
        for script in (TX, STAGE, INIT):
            with self.subTest(script=script.name):
                result = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_pilot_router_or_original_port_baked_into_transaction(self):
        code = TX.read_text()
        self.assertNotIn("WR3000S_2", code)
        self.assertNotIn("= 18443", code)
        self.assertIn('"$ST/router-id"', code)
        self.assertIn('"$ST/original-port"', code)

    def test_fail_closed_until_real_recovery_is_validated(self):
        code = TX.read_text()
        self.assertIn("development prototype: live transactions are disabled", code)
        for cmd in ("prepare", "apply", "confirm", "rollback", "boot-recover", "watchdog"):
            with self.subTest(command=cmd):
                result = subprocess.run(["sh", str(TX), cmd], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("live transactions are disabled", result.stdout + result.stderr)

    def test_stage_explicit_identity_is_not_hardware_specific(self):
        code = STAGE.read_text()
        self.assertIn("EXPECTED_ROUTER_ID", code)
        self.assertIn('"$stage/router-id"', code)
        self.assertIn('"$stage/original-port"', code)
        self.assertNotIn("only named Cudy main", code)
        self.assertNotIn("expected original TCP 18443", code)


if __name__ == "__main__":
    unittest.main()
