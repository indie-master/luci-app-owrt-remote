"""Read-only production main-router inventory safety and syntax contract."""
from pathlib import Path
import subprocess
import unittest
ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/"tools/router_live_pilot_inventory.sh"

class LivePilotInventoryTests(unittest.TestCase):
    def test_posix_syntax(self):
        p=subprocess.run(["sh","-n",str(SCRIPT)],text=True,capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
    def test_default_cannot_perform_any_action(self):
        for argv in ([],["--install"],["--apply"],["--inspect","extra"]):
            with self.subTest(argv=argv):
                p=subprocess.run(["sh",str(SCRIPT),*argv],
                    text=True,capture_output=True,timeout=3)
                self.assertNotEqual(p.returncode,0)
                self.assertIn("usage",p.stderr)
    def test_no_write_signals_or_credentials_displayed(self):
        s=SCRIPT.read_text()
        for term in ("ubus call service add","ubus call service signal",
                     "ubus call service delete","uci set","uci commit",
                     "service restart", "kill -", "sysupgrade", "reboot",
                     "wget ", "curl ", "opkg install","printf '%s\\n' \"$config\""):
            self.assertNotIn(term,s)
        self.assertIn('router_id',s)
        self.assertIn('vps_port',s)
        self.assertIn('START',s)
        self.assertIn('sha256sum "/$rel"',s)
        self.assertIn('READ-ONLY BASELINE PASS',s)
    def test_explicit_main_only_and_state_conflict(self):
        s=SCRIPT.read_text()
        self.assertIn('[ "$router_id" = main ]',s)
        self.assertIn('[ "$port" = 18443 ]',s)
        self.assertIn('existing persistent upgrade state',s)
        self.assertIn('stale test service:',s)
        self.assertIn('pid_for heartbeat',s)
        self.assertIn('pid_for xray',s)

if __name__=="__main__":
    unittest.main()
