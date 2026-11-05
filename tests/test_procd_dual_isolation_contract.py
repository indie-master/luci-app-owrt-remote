"""Safety contract for isolated real-procd *test* only.

No ubus/procd service is contacted by this test. It checks explicit opt-in,
isolated naming and cleanup, then executes the marker-only offline worker.
"""
from pathlib import Path
import os
import signal
import subprocess
import tempfile
import time
import unittest

BASE=Path(__file__).resolve().parents[1]
PROBE=BASE/"tools/openwrt_procd_dual_isolated_probe.sh"
WORKER=BASE/"tools/openwrt_procd_dual_worker.sh"

class ProcdDualIsolationTests(unittest.TestCase):
    def test_default_invocation_never_registers_a_service(self):
        r=subprocess.run(["sh",str(PROBE)],capture_output=True,text=True,timeout=4)
        self.assertNotEqual(r.returncode,0)
        self.assertIn("usage:",r.stderr)

    def test_worker_refuses_a_non_temporary_root(self):
        r=subprocess.run(["sh",str(WORKER),"secondary","/etc"],
                         capture_output=True,text=True,timeout=4)
        self.assertNotEqual(r.returncode,0)
        self.assertIn("not a dedicated temporary fixture",r.stderr)

    def test_unique_name_and_cleanup_policy(self):
        s=PROBE.read_text()
        worker=WORKER.read_text()
        self.assertIn('name="ro-procd-dual-$id"',s)
        self.assertIn('dir="$(mktemp -d /tmp/ro-procd-dual.XXXXXX)"',s)
        self.assertIn('case "$dir" in',s)
        self.assertIn('service delete',s)
        self.assertIn('safety_cleanup:',s)
        self.assertIn('ubus call service signal',s)
        self.assertIn('instance:$target,signal:9',s)
        self.assertIn('--run-secondary-crash',s)
        self.assertIn('target=secondary',s)
        self.assertIn('starts="$dir/secondary-starts"',s)
        self.assertIn('secondary did not show exactly one supervised restart',s)
        self.assertIn('respawn:[3600,1,0]',s)
        self.assertIn('exec sleep 120',worker)
        self.assertIn('rm -f "$dir/pending" "$dir/armed"',worker)
        self.assertIn('ubus call service delete',worker)

    def test_secondary_worker_survives_sigkill_with_same_original_deadline(self):
        # Simulate procd relaunch using actual worker processes in /tmp,
        # with no ubus calls and no real OpenWrt service.
        with tempfile.TemporaryDirectory(prefix="ro-procd-dual.", dir="/tmp") as d:
            root = Path(d)
            (root / ".dual-probe-marker").touch()
            (root / "armed").touch()
            (root / "pending").touch()
            pinned = int(float(Path("/proc/uptime").read_text().split()[0])) + 4
            (root / "deadline").write_text(str(pinned) + chr(10))
            first = subprocess.Popen(["sh",str(WORKER),"secondary",d],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
            try:
                limit = time.monotonic() + 2
                while not (root/"secondary-events").exists() and time.monotonic() < limit:
                    time.sleep(.05)
                self.assertTrue((root/"secondary-events").exists())
                os.kill(first.pid, signal.SIGKILL)
                first.wait(timeout=2)
            finally:
                if first.poll() is None:
                    first.kill()
                    first.wait(timeout=2)
                first.stderr.close()
            second = subprocess.Popen(["sh",str(WORKER),"secondary",d],
                                      stdout=subprocess.DEVNULL,
                                      stderr=subprocess.PIPE,text=True)
            try:
                limit = time.monotonic() + 9
                while not (root/"synthetic-rollback").exists() and time.monotonic() < limit:
                    time.sleep(.1)
                self.assertTrue((root/"synthetic-rollback").exists())
                self.assertFalse((root/"pending").exists())
                self.assertEqual(int((root/"deadline").read_text()),pinned)
                logs = (root/"secondary-events").read_text()
                self.assertEqual(logs.count("SECONDARY_START"), 2)
                self.assertIn("SECONDARY_EXPIRE", logs)
                # Assert actual numeric shell PIDs, not merely start counts.
                # A prior shell-template bug wrote a literal "$" instead of
                # "$", passing all earlier tests until the on-router probe.
                pids = (root / "secondary-starts").read_text().splitlines()
                self.assertEqual(pids, [str(first.pid), str(second.pid)])
                self.assertTrue(all(pid.isascii() and pid.isdecimal() for pid in pids))
                self.assertIn(f"SECONDARY_START pid={first.pid}", logs)
                self.assertIn(f"SECONDARY_START pid={second.pid}", logs)
                self.assertIn(f"SECONDARY_EXPIRE pid={second.pid}", logs)
            finally:
                if second.poll() is None:
                    second.terminate()
                    try:
                        second.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        second.kill()
                        second.wait(timeout=2)
                second.stderr.close()

    def test_worker_pid_shell_expansion_is_not_lost(self):
        code = WORKER.read_text()
        self.assertNotIn('"$"', code)
        self.assertIn('printf \'%s\\n\' "$" >> "$dir/secondary-starts"', code)
        self.assertIn('printf \'SECONDARY_START pid=%s', code)
        self.assertIn('printf \'SECONDARY_EXPIRE pid=%s', code)

    def test_probe_waits_for_absolute_deadline_plus_grace(self):
        s = PROBE.read_text()
        self.assertIn('stop_at=$((deadline + 8))', s)
        self.assertIn('[ "$check_now" -lt "$stop_at" ] || break', s)
        self.assertIn('max_polls=35', s)

    def test_offline_worker_clears_only_fake_pending(self):
        r=subprocess.run(["sh",str(PROBE),"--self-test"],
                         capture_output=True,text=True,timeout=12)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertIn("PASS - offline marker-only",r.stdout)

if __name__=="__main__":
    unittest.main()
