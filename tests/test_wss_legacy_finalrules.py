"""Regression guards for WSS candidate on old and modern Xray."""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT / "files/usr/sbin/owrt-remote"
REFRESH = ROOT / "tools/router_wss_refresh_candidate.sh"


class LegacyXrayWssRegression(unittest.TestCase):
    def test_shell_parses(self):
        for file in (AGENT, REFRESH):
            with self.subTest(file=file.name):
                result = subprocess.run(["sh", "-n", str(file)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_finalrules_only_depend_on_xray_version(self):
        source = AGENT.read_text()
        self.assertIn('if xray_reverse_finalrules_required; then', source)
        self.assertNotIn('if [ "$vps_transport" = ws ] || xray_reverse_finalrules_required; then', source)

    def test_refresh_never_restarts_service_or_commits_live_uci(self):
        source = REFRESH.read_text()
        self.assertIn('OWRT_REMOTE_UCI_CONFIG_DIR="$OUT/uci"', source)
        self.assertIn('WSS_HOST', source)
        self.assertIn('uci -c "$OUT/uci" set owrtremote.main.vps_sni="$HOST"', source)
        self.assertIn('cp -p "$OUT/uci/owrtremote" "$ST/candidate-uci.next"', source)
        self.assertIn("UPDATED CANDIDATE ONLY", source)
        for forbidden in ('/etc/init.d/owrt-remote restart',
                          'uci commit owrtremote', 'uci -q set owrtremote.main',
                          'kill -9'):
            self.assertNotIn(forbidden, source)

    def test_refresh_requires_original_unchanged_and_uuid_parity(self):
        source = REFRESH.read_text()
        self.assertIn('cmp -s /etc/config/owrtremote "$ST/original-uci"', source)
        self.assertIn('cmp -s /usr/sbin/owrt-remote "$ST/original-agent"', source)
        self.assertIn('links($old[0])==links($new[0])', source)
        self.assertIn('Xray rejected candidate', source)


if __name__ == "__main__":
    unittest.main()
