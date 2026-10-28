"""Verify fork installation upgrades original v119 clients without changing UCI transport."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORK_SOURCE = "https://raw.githubusercontent.com/indie-master/luci-app-owrt-remote/feature/wss-reverse-v119"


class ClientV119UpgradeTests(unittest.TestCase):
    def test_fork_installer_provenance_and_client_xray_version(self):
        installer = (ROOT / "install.sh").read_text()
        agent = (ROOT / "files/usr/sbin/owrt-remote").read_text()
        hub = (ROOT / "vps/owrt-remote-hub.py").read_text()
        self.assertIn(FORK_SOURCE, installer)
        self.assertIn(FORK_SOURCE, hub)
        self.assertIn('XRAY_RELEASE_VERSION=', agent)
        self.assertIn('26.10.21', agent)
        self.assertIn('uci_get vps_transport tcp', agent)
        self.assertIn('stream_fields="$stream_fields$wan_sockopt"', agent)
        self.assertIn('finalRules', agent)
        self.assertIn('vps_allow_legacy', hub)

    def test_installer_preserves_existing_router_configuration(self):
        with tempfile.TemporaryDirectory(prefix="owrt-v119-router-upgrade-") as d:
            root = Path(d)
            config = root / "etc/config/owrtremote"
            config.parent.mkdir(parents=True)
            exact = (
                "config remote 'main'\n"
                "\toption enabled '1'\n"
                "\toption router_id 'legacy-one'\n"
                "\toption vps_port '18443'\n"
                "\toption vps_transport 'tcp'\n"
                "\toption vless_uuid 'DO-NOT-ROTATE'\n"
                "\toption xray_bin '/tmp/owrt-xray/xray'\n"
            )
            config.write_text(exact)
            result = subprocess.run(
                ["sh", str(ROOT / "install.sh")],
                env={**os.environ, "ROOT": str(root)},
                text=True, capture_output=True, timeout=35,
            )
            self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
            self.assertEqual(config.read_text(), exact)
            installed = (root / "usr/sbin/owrt-remote").read_bytes()
            self.assertEqual(installed, (ROOT / "files/usr/sbin/owrt-remote").read_bytes())
            self.assertIn("OpenWrt Remote установлен.", result.stdout)

    def test_preflight_refuses_existing_wss_without_uci_flag(self):
        installer = (ROOT / "install.sh").read_text()
        segment = installer.split("transport_preflight() {", 1)[1].split("\nmake_key() {", 1)[0]
        source = "transport_preflight() {" + segment
        with tempfile.TemporaryDirectory(prefix="owrt-client-preflight-") as d:
            config = Path(d) / "client.json"
            config.write_text('{"outbounds":[{"tag":"vps-interconn","streamSettings":{"network":"ws"}}]}')
            script = (
                "ROOT=/\n"
                "uci() { case \"$*\" in "
                "'-q get owrtremote.main.xray_config') printf '%s' \"$TEST_CONFIG\" ;; "
                "'-q get owrtremote.main.vps_transport') printf '' ;; "
                "esac; }\n"
                "die() { echo \"BLOCKED: $*\"; exit 55; }\n"
                + source + "\ntransport_preflight\n"
            )
            result = subprocess.run(["sh", "-c", script],
                                    env={**os.environ, "TEST_CONFIG": str(config)},
                                    capture_output=True, text=True, timeout=12)
        self.assertEqual(result.returncode, 55, result.stderr)
        self.assertIn("BLOCKED", result.stdout)

    def test_new_install_is_tcp_safe_until_hub_provisions_wss(self):
        with tempfile.TemporaryDirectory(prefix="owrt-v119-new-client-") as d:
            root = Path(d)
            result = subprocess.run(["sh", str(ROOT / "install.sh")],
                                    env={**os.environ, "ROOT": str(root)},
                                    text=True, capture_output=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stderr)
            uci = (root / "etc/config/owrtremote").read_text()
            self.assertIn("option vps_transport 'tcp'", uci)
            self.assertIn("option vps_ws_path '/owrt-ws/'", uci)
            self.assertIn("option enabled '0'", uci)


if __name__ == "__main__":
    unittest.main()
