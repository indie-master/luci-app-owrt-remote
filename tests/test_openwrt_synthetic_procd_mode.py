"""Prevent --run-procd silently falling back to local shell workers.

The fake ubus *rejects* service registration. For the explicit --run-procd
mode, this MUST be a test failure rather than a local-worker success.
All router files remain under the script's dedicated /tmp fixture.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

BASE=Path(__file__).resolve().parents[1]
SCRIPT=BASE/"tools/openwrt_synthetic_update_cycle.sh"

class CycleProcdModeTests(unittest.TestCase):
    def test_fixture_file_modes_do_not_override_execution_mode(self):
        s=SCRIPT.read_text()
        self.assertIn('cycle_mode="${1:-legacy}"',s)
        self.assertIn('if [ "$cycle_mode" = --run-procd ]; then',s)
        self.assertIn('file_mode=0755',s)
        self.assertIn('file_mode=0644',s)
        self.assertIn('"$digest" "$file_mode" "$rel" >> "$src/manifest.txt"',s)
        self.assertNotIn('mode=0755',s.replace('file_mode=0755',''))
        self.assertNotIn('mode=0644',s.replace('file_mode=0644',''))

    def test_explicit_real_procd_mode_never_falls_back_after_rejected_registration(self):
        with tempfile.TemporaryDirectory(prefix="ro-ci-fake-ubus-",dir="/tmp") as d:
            root=Path(d)
            fakebin=root/"bin"
            fakebin.mkdir()
            events=root/"events.log"
            fakeubus=fakebin/"ubus"
            fakeubus.write_text("""#!/bin/sh
case "$1:$2:$3" in
    list::) printf 'service\\n';;
    call:service:list) printf '{}\\n';;
    call:service:add)
        echo 'PROCD_ADD_CALLED' >> "$RO_FAKE_UBUS_EVENTS"
        exit 91 ;;
    call:service:delete)
        echo 'PROCD_DELETE_CALLED' >> "$RO_FAKE_UBUS_EVENTS"
        printf '{}\\n' ;;
    *) exit 70 ;;
esac
""")
            fakeubus.chmod(0o700)
            result=subprocess.run(["sh",str(SCRIPT),"--run-procd"],
                env={**os.environ,
                     "PATH":str(fakebin)+":"+os.environ.get("PATH",""),
                     "RO_FAKE_UBUS_EVENTS":str(events)},
                text=True,capture_output=True,timeout=40)
            output=result.stdout+result.stderr
            self.assertNotEqual(result.returncode,0,output)
            self.assertTrue(events.exists(),
                            "real procd registration was skipped: "+output)
            self.assertIn("PROCD_ADD_CALLED",events.read_text())
            self.assertIn("real procd did not start both watchdogs",output)
            self.assertNotIn("full supervised synthetic rollback completed",output)
            self.assertNotIn("real transaction watchdog restored all ten",output)

if __name__=="__main__":
    unittest.main()
