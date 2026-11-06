"""Contract tests for the isolated real-procd heartbeat-only rehearsal.

The hardware-specific --run mode is NEVER invoked by CI, since CI has
no OpenWrt procd. Only synthetic roots are permitted.
"""
import subprocess
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PROBE=ROOT/"tools/openwrt_runtime_heartbeat_probe.sh"
ACTIVATE=ROOT/"tools/router_heartbeat_activation.sh"

class HeartbeatRuntimeProbeContract(unittest.TestCase):
    def test_posix_syntax(self):
        for script in (PROBE,ACTIVATE):
            r=subprocess.run(["sh","-n",str(script)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_default_and_bad_arguments_cannot_register_service(self):
        for args in ([],["--run"],["--inspect"],["--run","/does/not/exist"]):
            r=subprocess.run(["sh",str(PROBE),*args],
                capture_output=True,text=True,timeout=3)
            self.assertNotEqual(r.returncode,0)

    def test_isolated_service_and_both_activation_directions(self):
        s=PROBE.read_text()
        self.assertIn("ro-runtime-test-",s)
        self.assertIn("mktemp -d /tmp/ro-runtime-test-",s)
        self.assertIn('name="ro-runtime-test-$nonce"',s)
        self.assertIn('export RO_TEST_MODE=1',s)
        self.assertIn('export RO_TEST_ROOT="$root"',s)
        self.assertIn('export RO_TEST_SERVICE_NAME="$name"',s)
        self.assertIn('sh "$activation" --activate',s)
        self.assertIn('sh "$activation" --restore',s)
        self.assertIn('fake Xray PID unchanged',s)
        self.assertIn('keep fixture',s)
        self.assertNotIn('instance:"xray",signal:',s)
        self.assertNotIn('service state',s)
        self.assertNotIn('/etc/init.d/owrt-remote restart',s)
        self.assertNotIn('/usr/bin/ubus',s)
        self.assertIn('ubus call service delete',s)

if __name__=="__main__":
    unittest.main()
