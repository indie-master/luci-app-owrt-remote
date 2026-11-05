"""Synthetic tests for the non-deployable OpenWrt procd service blueprint.

The service refuses live startup and only ever executes a synthetic
transaction.sh inside a TemporaryDirectory. Real procd is not contacted.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "tools/router_pilot_procd_recovery.init"

class ProcdRecoveryBlueprintTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="procd-recovery-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "synthetic-router"
        self.state = self.root / "etc/router-orchestrator/upgrade"
        self.state.mkdir(parents=True)
        self.log = Path(tmp.name) / "calls.log"
        self.transaction = self.state / "transaction.sh"
        self.transaction.write_text(
            '#!/bin/sh\n'
            'printf "transaction %s\\n" "$1" >> "$RO_PROCD_TEST_LOG"\n'
            'exit "${RO_PROCD_TRANSACTION_EXIT:-0}"\n',
            encoding="utf-8"
        )
        self.transaction.chmod(0o700)

    def run_blueprint(self, *, test_mode=True, root=None, exit_code="0"):
        # Source in a detached test shell and mock procd helper functions.
        cmd = '''
procd_open_instance() { printf 'open %s\\n' "$*" >> "$RO_PROCD_TEST_LOG"; }
procd_set_param() { printf 'set %s\\n' "$*" >> "$RO_PROCD_TEST_LOG"; }
procd_close_instance() { printf 'close\\n' >> "$RO_PROCD_TEST_LOG"; }
. "$1"
start_service
'''
        env = {**os.environ, "RO_PROCD_TEST_LOG": str(self.log),
               "RO_PROCD_TRANSACTION_EXIT": exit_code,
               "RO_TEST_ROOT": str(root or self.root)}
        if test_mode:
            env["RO_PROCD_TEST_MODE"] = "1"
        else:
            env.pop("RO_PROCD_TEST_MODE", None)
        result = subprocess.run(
            ["sh", "-c", cmd, "sh", str(INIT)],
            env=env, text=True, capture_output=True, timeout=6)
        calls = self.log.read_text() if self.log.exists() else ""
        return result, calls

    def test_unconfigured_service_does_not_start_watchdog(self):
        result, calls = self.run_blueprint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "")

    def test_armed_state_registers_watchdog_not_legacy_agent(self):
        (self.state / "armed").touch()
        result, calls = self.run_blueprint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("open rollback-watchdog", calls)
        self.assertIn(
            f"set command /bin/sh {self.transaction} watchdog", calls)
        self.assertIn("set respawn 3600 2 3", calls)
        self.assertIn("close", calls)
        self.assertNotIn("owrt-remote", calls)

    def test_pending_reboots_roll_back_before_registering_watchdog(self):
        (self.state / "pending").touch()
        (self.state / "armed").touch()
        result, calls = self.run_blueprint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "transaction boot-recover\n")

    def test_boot_recovery_failure_never_claims_success(self):
        (self.state / "pending").touch()
        result, calls = self.run_blueprint(exit_code="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, "transaction boot-recover\n")

    def test_live_registration_is_explicitly_disabled(self):
        (self.state / "armed").touch()
        result, calls = self.run_blueprint(test_mode=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, "")
        self.assertIn("TEST-ONLY", result.stderr)

    def test_root_slash_and_relative_root_are_rejected(self):
        for bad in ("/", "relative", "/tmp/../etc"):
            with self.subTest(path=bad):
                result, calls = self.run_blueprint(root=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, "")

if __name__ == "__main__":
    unittest.main()
