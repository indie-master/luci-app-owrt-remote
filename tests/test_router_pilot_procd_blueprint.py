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
        (self.root / ".ro-pilot-synthetic-root").write_text("TEST ONLY\n")
        (self.state / "secondary-guard.sh").write_text("#!/bin/sh\nexit 0\n")
        self.log = Path(tmp.name) / "calls.log"
        self.transaction = self.state / "transaction.sh"
        self.transaction.write_text(
            '#!/bin/sh\n'
            'printf "transaction %s\\n" "$1" >> "$RO_PROCD_TEST_LOG"\n'
            'exit "${RO_PROCD_TRANSACTION_EXIT:-0}"\n',
            encoding="utf-8"
        )
        self.transaction.chmod(0o700)

    def run_blueprint(self, *, test_mode=True, root=None, exit_code="0", boot=False, cleanup=False):
        # Source in a detached test shell and mock procd helper functions.
        cmd = '''
procd_open_instance() { printf 'open %s\\n' "$*" >> "$RO_PROCD_TEST_LOG"; }
procd_set_param() { printf 'set %s\\n' "$*" >> "$RO_PROCD_TEST_LOG"; }
procd_close_instance() { printf 'close\\n' >> "$RO_PROCD_TEST_LOG"; }
procd_kill() { printf 'kill %s\\n' "$*" >> "$RO_PROCD_TEST_LOG"; }
. "$1"
[ "$RO_TEST_BOOT" = 1 ] && RO_PILOT_BOOT=1
if [ "$RO_TEST_CLEANUP" = 1 ]; then
    cleanup_confirmed
else
    start_service
fi
'''
        env = {**os.environ, "RO_PROCD_TEST_LOG": str(self.log),
               "RO_PROCD_TRANSACTION_EXIT": exit_code,
               "RO_TEST_BOOT": "1" if boot else "0",
               "RO_TEST_CLEANUP": "1" if cleanup else "0",
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
        (self.state / "deadline.uptime").write_text("12345\n")
        result, calls = self.run_blueprint()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("open rollback-watchdog", calls)
        self.assertIn(
            f"set command /bin/sh {self.transaction} watchdog", calls)
        self.assertIn("set respawn 3600 2 0", calls)
        self.assertIn("close", calls)
        self.assertNotIn("owrt-remote", calls)

    def test_pending_reboots_roll_back_before_registering_watchdog(self):
        (self.state / "pending").touch()
        (self.state / "armed").touch()
        (self.state / "deadline.uptime").write_text("12345\n")
        result, calls = self.run_blueprint(boot=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "transaction boot-recover\n")

    def test_two_independent_supervised_instances_and_unlimited_retry(self):
        (self.state / "armed").touch()
        (self.state / "deadline.uptime").write_text("12345\n")
        result, calls = self.run_blueprint()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(calls.count("open rollback-watchdog"), 1)
        self.assertEqual(calls.count("open independent-deadline-guard"), 1)
        self.assertIn(f"set command /bin/sh {self.state / 'secondary-guard.sh'}",calls)
        self.assertEqual(calls.count("set respawn 3600 2 0"), 2)

    def test_missing_secondary_guard_fails_closed_before_registering(self):
        (self.state / "armed").touch()
        (self.state / "deadline.uptime").write_text("12345\n")
        (self.state / "secondary-guard.sh").unlink()
        result, calls = self.run_blueprint()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, "")

    def test_same_boot_pending_registers_watchdog_without_rollback(self):
        (self.state / "pending").touch()
        (self.state / "armed").touch()
        (self.state / "deadline.uptime").write_text("12345\n")
        result, calls = self.run_blueprint(boot=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("open rollback-watchdog", calls)
        self.assertIn("set env RO_TEST_MODE=1", calls)
        self.assertNotIn("boot-recover", calls)

    def test_missing_deadline_prevents_registration(self):
        (self.state / "armed").touch()
        result, calls = self.run_blueprint()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, "")
        self.assertIn("missing pinned deadline", result.stderr)

    def test_boot_recovery_failure_never_claims_success(self):
        (self.state / "pending").touch()
        result, calls = self.run_blueprint(exit_code="1", boot=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, "transaction boot-recover\n")

    def test_live_registration_is_explicitly_disabled(self):
        (self.state / "armed").touch()
        (self.state / "deadline.uptime").write_text("12345\n")
        result, calls = self.run_blueprint(test_mode=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, "")
        self.assertIn("TEST-ONLY", result.stderr)

    def test_cleanup_rejected_during_active_transaction(self):
        (self.state / "pending").touch()
        (self.state / "armed").touch()
        (self.state / "confirmed").touch()
        result, calls = self.run_blueprint(cleanup=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refuse cleanup", result.stderr)
        self.assertNotIn("kill", calls)

    def test_cleanup_allowed_only_after_complete_confirmation(self):
        (self.state / "confirmed").touch()
        result, calls = self.run_blueprint(cleanup=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, "kill ro-pilot-recovery rollback-watchdog\nkill ro-pilot-recovery independent-deadline-guard\n")

    def test_cleanup_live_registration_is_disabled(self):
        (self.state / "confirmed").touch()
        result, calls = self.run_blueprint(cleanup=True, test_mode=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEST-ONLY", result.stderr)
        self.assertEqual(calls, "")

    def test_root_slash_and_relative_root_are_rejected(self):
        for bad in ("/", "relative", "/tmp/../etc"):
            with self.subTest(path=bad):
                result, calls = self.run_blueprint(root=bad)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, "")

if __name__ == "__main__":
    unittest.main()
