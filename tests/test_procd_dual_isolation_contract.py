"""Safety contract for isolated real-procd *test* only.

No ubus/procd service is contacted by this test. It checks explicit opt-in,
isolated naming and cleanup, then executes the marker-only offline worker.
"""
from pathlib import Path
import subprocess
import tempfile
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
        self.assertIn('instance:"primary",signal:9',s)
        self.assertIn('respawn:[3600,1,0]',s)
        self.assertIn('exec sleep 120',worker)
        self.assertIn('rm -f "$dir/pending" "$dir/armed"',worker)
        self.assertIn('ubus call service delete',worker)

    def test_offline_worker_clears_only_fake_pending(self):
        r=subprocess.run(["sh",str(PROBE),"--self-test"],
                         capture_output=True,text=True,timeout=12)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertIn("PASS - offline marker-only",r.stdout)

if __name__=="__main__":
    unittest.main()
