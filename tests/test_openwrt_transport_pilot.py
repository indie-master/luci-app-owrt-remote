"""Static safety contract for isolated OpenWrt shadow transport pilot."""
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] /
          "files/usr/sbin/owrt-remote")


class OpenWrtTransportPilotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SCRIPT.read_text()
        cls.start = cls.source.split("pilot_start() (", 1)[1].split("\nrun_xray() {", 1)[0]

    def test_pilot_has_independent_short_ttl(self):
        self.assertIn("sleep 180", self.start)
        self.assertIn("transport-pilot-stop", self.start)
        self.assertIn("nohup", self.start)

    def test_pilot_does_not_reconfigure_live_service(self):
        for command in ("uci set", "uci commit", "/etc/init.d/owrt-remote restart",
                        "procd_set_param", "systemctl", "killall xray"):
            with self.subTest(command=command):
                self.assertNotIn(command, self.start)

    def test_shadow_reverse_tags_cannot_equal_primary(self):
        self.assertIn("pilot would reuse current admin reverse tag", self.start)
        self.assertIn("pilot would reuse current SSH reverse tag", self.start)

    def test_pid_verified_before_termination(self):
        stop = self.source.split("pilot_stop() {", 1)[1].split("\npilot_status() {", 1)[0]
        self.assertIn('if pilot_matches "$pid"; then', stop)
        self.assertIn('kill -TERM "$pid"', stop)

    def test_client_preflight_does_not_change_uci(self):
        code = self.source.split("transport_preflight() (", 1)[1].split("\nrun_xray() {", 1)[0]
        self.assertIn('render_client_config --stdout', code)
        self.assertIn('validate_xray_config "$candidate"', code)
        self.assertNotIn("uci commit", code)


if __name__ == "__main__":
    unittest.main()
