"""Static regression guard for original OpenWrt Remote Hub / router agent surfaces.

Deliberately additive: this is a smoke test, NOT a proof that every historical
feature works. Pair with upstream runtime/e2e tests and preserved snapshots.
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

# All these are original project building blocks, not disposable scaffolding.
ORIGINAL_FILES = (
    "vps/owrt-remote-hub.py",
    "vps/install-vps.sh",
    "install.sh",
    "files/usr/sbin/owrt-remote",
    "files/etc/init.d/owrt-remote",
    "files/etc/init.d/owrt-remote-logs",
    "files/usr/lib/owrt-remote/wan.sh",
    "files/usr/lib/owrt-remote/logs.sh",
    "files/etc/hotplug.d/iface/99-owrt-remote",
    "files/www/cgi-bin/owrt-remote",
    "files/www/luci-static/resources/view/owrt_remote.js",
    "files/usr/share/luci/menu.d/luci-app-owrt-remote.json",
    "files/usr/share/rpcd/acl.d/luci-app-owrt-remote.json",
    "files/etc/config/owrtremote",
    "LICENSE",
)

AGENT_COMMANDS = (
    "status", "doctor", "install-xray-tmp", "prepare-xray",
    "stop-xray", "render-client", "run", "heartbeat",
    "heartbeat-loop", "heartbeat-diagnostics", "logs-sync",
    "network-diagnostics",
)

class OriginalFeatureContractTests(unittest.TestCase):
    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_original_files_still_exist(self):
        for relative in ORIGINAL_FILES:
            with self.subTest(path=relative):
                target = ROOT / relative
                self.assertTrue(target.is_file(), f"Original file removed: {relative}")
                self.assertGreater(target.stat().st_size, 0)

    def test_legacy_agent_command_surface_is_retained(self):
        source = self.read("files/usr/sbin/owrt-remote")
        for cmd in AGENT_COMMANDS:
            with self.subTest(command=cmd):
                self.assertIn("owrt-remote " + cmd, source)

    def test_reverse_service_and_heartbeat_are_retained(self):
        service = self.read("files/etc/init.d/owrt-remote")
        self.assertIn("procd_open_instance xray", service)
        self.assertIn("procd_open_instance heartbeat", service)
        self.assertIn("OWRT_REMOTE_SERVICE_LOG", service)
        self.assertIn("stop_xray_processes", service)

    def test_existing_luci_cgi_and_permissions_are_retained(self):
        cgi = self.read("files/www/cgi-bin/owrt-remote")
        luci = self.read("files/www/luci-static/resources/view/owrt_remote.js")
        self.assertIn('KEY_FILE=', cgi)
        self.assertIn('AGENT=', cgi)
        self.assertIn("'require view'", luci)
        self.assertIn("'require fs'", luci)
        self.assertIn("rpcd", self.read("install.sh"))

    def test_legacy_hub_update_and_router_upgrade_features_remain(self):
        hub = self.read("vps/owrt-remote-hub.py")
        for marker in (
            "class HubUpdateManager:",
            "def run_hub_update_worker(",
            "function initHubUpdate()",
            "panel-update-worker",
            "hub_update_api",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, hub)
        installer = self.read("install.sh")
        self.assertIn("install_file", installer)
        self.assertIn("install_config", installer)
        self.assertIn("transport_preflight", installer)

    def test_original_license_and_upstream_attribution_remain(self):
        license_text = self.read("LICENSE")
        self.assertIn("MIT License", license_text)
        self.assertIn("kzolotarev95", license_text)
        readme = self.read("README.md")
        self.assertIn("kzolotarev95/luci-app-owrt-remote", readme)
        self.assertIn("UPSTREAM_FEATURE_PARITY.md", readme)

    def test_new_product_naming_does_not_rename_wire_protocol(self):
        arch = self.read("docs/ROUTER_ORCHESTRATOR_ARCHITECTURE.md")
        self.assertIn("Router Orchestrator", arch)
        self.assertIn("Remote HUB", arch)
        self.assertIn("original", arch.lower())
        # These UCI keys are part of the deployed legacy protocol.
        uci = self.read("files/etc/config/owrtremote")
        self.assertIn("config remote 'main'", uci)
        self.assertIn("option vps_transport", uci)


if __name__ == "__main__":
    unittest.main()
