"""Static safety gates for the separate WSS staging step.

No OpenWrt device is modified by these tests. Real Xray render/validation
remains a hardware acceptance gate.
"""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/router_wss_pilot_stage.sh"


class WssPilotStageContract(unittest.TestCase):
    def test_shell_syntax(self):
        p = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_staging_uses_shadow_uci_and_no_live_uci_writes(self):
        data = SCRIPT.read_text()
        self.assertIn('uci -c "$work/uci" set', data)
        self.assertIn('UCI_CONFIG_DIR="$work/uci"', data)
        self.assertIn('render-client --stdout', data)
        self.assertIn('cp -p "$xray_config" "$stage/original-xray.json"', data)
        self.assertIn('cp -p "$work/config/owrtremote" "$stage/original-uci"', data)
        self.assertIn('sha256sum original-uci original-xray.json', data)
        for forbidden in ('uci set owrtremote.main', 'uci commit owrtremote',
                          '/etc/init.d/owrt-remote restart',
                          '/etc/init.d/owrt-remote reload', 'kill -9', 'procd_kill',
                          'mv -f "$work/candidate.json" "$xray_config"'):
            self.assertNotIn(forbidden, data)

    def test_blocks_live_execution_without_correct_target(self):
        data = SCRIPT.read_text()
        self.assertIn('= main ] || fail', data)
        self.assertIn('= 18443 ] || fail', data)
        self.assertIn('installed agent does not support WSS', data)
        self.assertIn('previous stage exists', data)
        self.assertIn('STAGED ONLY', data)


if __name__ == "__main__":
    unittest.main()
