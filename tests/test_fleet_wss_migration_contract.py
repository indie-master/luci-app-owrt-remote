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

    def test_single_dispatch_no_trailing_shell_source(self):
        code = TX.read_text()
        self.assertEqual(code.count('action="$1"; shift'), 1)
        self.assertEqual(code.count("  *) die 'unknown action' ;;"), 1)
        self.assertTrue(code.rstrip().endswith("esac"))

    def test_no_pilot_router_or_original_port_baked_into_transaction(self):
        code = TX.read_text()
        self.assertNotIn("WR3000S_2", code)
        self.assertNotIn("= 18443", code)
        self.assertIn('"$ST/router-id"', code)
        self.assertIn('"$ST/original-port"', code)

    def test_recovery_is_generic_and_persistent(self):
        code = TX.read_text()
        self.assertIn('"$ST/original-uci"', code)
        self.assertIn('"$ST/original-xray.json"', code)
        self.assertIn('"$ST/original-agent"', code)
        self.assertIn('"$ST/pending"', code)
        self.assertIn('"$ST/armed"', code)
        self.assertIn('boot_recover()', code)
        self.assertIn('watchdog()', code)
        self.assertIn('confirm()', code)
        self.assertNotIn('development prototype: live transactions are disabled', code)
        init = INIT.read_text()
        self.assertIn('START=08', init)
        self.assertIn('boot-recover', init)
        self.assertIn('procd_set_param command', init)

    def test_stage_explicit_identity_is_not_hardware_specific(self):
        code = STAGE.read_text()
        self.assertIn("EXPECTED_ROUTER_ID", code)
        self.assertIn('"$stage/router-id"', code)
        self.assertIn('"$stage/original-port"', code)
        self.assertNotIn("only named Cudy main", code)
        self.assertNotIn("expected original TCP 18443", code)


if __name__ == "__main__":
    unittest.main()
