"""End-to-end synthetic integration of procd blueprint and real transaction logic.

These tests do NOT register real procd services. They reuse the existing
10-file fake router fixture, emulate OpenWrt's procd helper calls, and then
run the exactly recorded watchdog command against its temporary root.
"""
import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import unittest

BASE = Path(__file__).resolve().parents[1]
INIT = BASE / "tools/router_pilot_procd_recovery.init"
TEST_FILE = BASE / "tests/test_router_pilot_transaction.py"

def load_fixture_class():
    spec = importlib.util.spec_from_file_location("pilot_fixture_for_procd", TEST_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RouterPilotTransactionTests

class ProcdTransactionSyntheticE2ETests(unittest.TestCase):
    def setUp(self):
        fixture_type = load_fixture_class()
        # Use a full snapshot + candidate fixture, not mocked recovery logic.
        self.fx = fixture_type("test_manual_rollback_restores_every_file")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.prepared()
        (self.fx.router / ".ro-pilot-synthetic-root").write_text("TEST ONLY\n")
        (self.fx.state / "secondary-guard.sh").write_bytes(
            (BASE / "tools/router_pilot_secondary_guard.sh").read_bytes())
        self.events = self.fx.temp / "procd-events.log"

    def procd(self, boot=False, cleanup=False):
        shell = r'''
procd_open_instance() { printf 'open %s\n' "$*" >> "$RO_PROCD_EVENTS"; }
procd_set_param() { printf 'param %s\n' "$*" >> "$RO_PROCD_EVENTS"; }
procd_close_instance() { printf 'close\n' >> "$RO_PROCD_EVENTS"; }
procd_kill() { printf 'kill %s\n' "$*" >> "$RO_PROCD_EVENTS"; }
. "$1"
[ "$RO_PROCD_BOOT" = 1 ] && RO_PILOT_BOOT=1
if [ "$RO_PROCD_CLEANUP" = 1 ]; then
    cleanup_confirmed
else
    start_service
fi
'''
        env = {**os.environ, "RO_PROCD_TEST_MODE": "1",
               "RO_TEST_ROOT": str(self.fx.router),
               "RO_PROCD_BOOT": "1" if boot else "0",
               "RO_PROCD_CLEANUP": "1" if cleanup else "0",
               "RO_PROCD_EVENTS": str(self.events)}
        p = subprocess.run(["sh", "-c", shell, "sh", str(INIT)],
                           text=True, capture_output=True, env=env, timeout=8)
        lines = self.events.read_text().splitlines() if self.events.exists() else []
        return p, lines

    def test_supervised_worker_uses_original_monotonic_deadline(self):
        self.fx.apply(timeout=12)
        pinned = int((self.fx.state / "deadline.uptime").read_text())
        p, lines = self.procd(boot=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("open rollback-watchdog", lines)
        argv = [line[len("param command "):]
                for line in lines if line.startswith("param command ")]
        env_parts = [line[len("param env "):]
                     for line in lines if line.startswith("param env ")]
        self.assertEqual(len(argv), 1)
        self.assertEqual(len(env_parts), 1)
        cmd = shlex.split(argv[0])
        self.assertEqual(cmd[-1], "watchdog")
        self.assertEqual(Path(cmd[1]), self.fx.state / "transaction.sh")
        supplied_env = dict(x.split("=", 1) for x in shlex.split(env_parts[0]))
        self.assertEqual(supplied_env["RO_TEST_ROOT"], str(self.fx.router))
        self.assertEqual(supplied_env["RO_TEST_MODE"], "1")
        # Emulate procd relaunch after crash: the new worker sees uptime after
        # the ORIGINAL deadline, and immediately restores the old code bundle.
        env = {**os.environ, **supplied_env, "RO_TEST_NOW": str(pinned + 1)}
        before = time.monotonic()
        worker = subprocess.run(cmd, env=env, text=True,
                                capture_output=True, timeout=8)
        self.assertEqual(worker.returncode, 0, worker.stdout + worker.stderr)
        self.assertLess(time.monotonic() - before, 4)
        self.assertEqual(int((self.fx.state / "deadline.uptime").read_text()), pinned)
        self.assertTrue((self.fx.state / "rolled-back").exists())
        self.fx.restore_assertions()

    def test_cleanup_rejected_before_real_transaction_confirm(self):
        self.fx.apply(timeout=12)
        p, lines = self.procd(cleanup=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertNotIn("kill ro-pilot-recovery", lines)
        r = self.fx.run_cmd(self.fx.state / "transaction.sh", "rollback")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_cleanup_succeeds_after_real_transaction_confirm(self):
        self.fx.apply(timeout=12)
        nonce=(self.fx.state/"confirm-nonce").read_text().strip()
        r=self.fx.run_cmd(self.fx.state/"transaction.sh","confirm",nonce)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse((self.fx.state/"pending").exists())
        self.assertFalse((self.fx.state/"armed").exists())
        p, lines=self.procd(cleanup=True)
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn("kill ro-pilot-recovery rollback-watchdog",lines)
        self.assertIn("kill ro-pilot-recovery independent-deadline-guard", lines)

    def test_secondary_guard_restores_when_primary_is_crashed(self):
        self.fx.apply(timeout=12)
        pinned = int((self.fx.state / "deadline.uptime").read_text())
        p, lines = self.procd(boot=False)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("open independent-deadline-guard", lines)
        env_lines = [line[len("param env "):] for line in lines
                     if line.startswith("param env ")]
        cmd_lines = [line[len("param command "):] for line in lines
                     if line.startswith("param command ")]
        self.assertEqual(len(cmd_lines), 2)
        self.assertEqual(len(env_lines), 2)
        secondary_argv = shlex.split(cmd_lines[1])
        env = dict(part.split("=", 1) for part in shlex.split(env_lines[1]))
        env = {**os.environ, **env, "RO_TEST_NOW": str(pinned + 1)}
        # Primary watchdog remains alive but is not the process performing
        # this recovery; the separate command must independently roll back.
        worker = subprocess.run(secondary_argv, env=env,
                                capture_output=True,text=True, timeout=8)
        self.assertEqual(worker.returncode, 0, worker.stdout + worker.stderr)
        self.assertTrue((self.fx.state / "rolled-back").exists())
        self.fx.restore_assertions()
        self.assertFalse((self.fx.state / "pending").exists())

    def test_boot_recovery_runs_real_transaction_before_procd_registration(self):
        self.fx.apply(timeout=12)
        p, lines = self.procd(boot=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(lines, [])
        self.assertTrue((self.fx.state / "rolled-back").exists())
        self.fx.restore_assertions()
        self.assertFalse((self.fx.state / "pending").exists())

if __name__ == "__main__":
    unittest.main()
