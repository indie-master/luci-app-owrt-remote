"""Safety contract for one-shot physical acceptance runner, without touching procd."""
import subprocess
from pathlib import Path
import unittest

BASE=Path(__file__).resolve().parents[1]
RUNNER=BASE/"tools/openwrt_release_hardware_gate.sh"

class HardwareGateContracts(unittest.TestCase):
    def test_requires_full_immutable_revision(self):
        for args in ([],["--run"],["--run","main"],
                     ["--run","abc"],["--run","Z"*40]):
            with self.subTest(args=args):
                r=subprocess.run(["sh",str(RUNNER),*args],
                    capture_output=True,text=True,timeout=3)
                self.assertNotEqual(r.returncode,0)

    def test_no_direct_live_instance_mutations(self):
        script=RUNNER.read_text()
        self.assertIn("openwrt_synthetic_update_cycle.sh",script)
        self.assertIn("--run-procd",script)
        self.assertIn("readonly cycle_mode",script)
        self.assertIn("wait_deleted",script)
        self.assertIn("xray_before",script)
        self.assertIn("xray_after",script)
        self.assertIn('heartbeat_before',script)
        self.assertIn('heartbeat_after',script)
        self.assertIn("temporary procd service absent",script)
        self.assertIn("production deployment remains disabled",script)
        self.assertNotIn("ubus call service signal",script)
        self.assertNotIn("service restart",script)
        self.assertNotIn("ubus call service delete",script)

if __name__=="__main__":
    unittest.main()
