"""Offline transport profile catalog and failover safety tests."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_profile_catalog as catalog
from orchestrator_setup_profiles import InvalidPlan

ROUTER = {"id": "pilot-new",
          "vless_uuid": "11111111-1111-4111-8111-111111111111",
          "ssh_vless_uuid": "22222222-2222-4222-8222-222222222222"}
SECRETS = {"private_key": "a" * 43, "public_key": "b" * 43,
           "short_id": "0123456789abcdef"}


class CatalogTests(unittest.TestCase):
    def mixed(self):
        config = catalog.starter_catalog("hub.example.org",
                                        backup_domain="backup.example.org",
                                        add_reality=True)
        config["profiles"][-1]["enabled"] = True
        config["routers"]["pilot-new"] = {
            "mode": "manual", "active": "wss-primary",
            "fallback_order": ["wss-backup", "reality-pilot"],
        }
        return catalog.validate(config)

    def test_wss_backup_domain_uses_correct_tls_sni_and_backend(self):
        config = self.mixed()
        for name, endpoint in (("wss-primary", "hub.example.org"),
                               ("wss-backup", "backup.example.org")):
            x = catalog.candidate_pair(config, name, ROUTER)
            self.assertFalse(x["deployable"])
            self.assertEqual(x["profile_id"], name)
            self.assertEqual(x["server_inbound"]["port"], 18444)
            self.assertEqual(len(x["server_inbound"]["settings"]["clients"]), 2)
            for outbound in x["router_outbounds"]:
                self.assertEqual(outbound["settings"]["address"], endpoint)
                self.assertEqual(outbound["streamSettings"]["tlsSettings"]["serverName"],
                                 endpoint)
                self.assertEqual(outbound["settings"]["port"], 443)
                self.assertFalse(
                    outbound["streamSettings"]["tlsSettings"]["allowInsecure"])

    def test_reality_backup_has_only_server_side_private_key(self):
        x = catalog.candidate_pair(self.mixed(), "reality-pilot", ROUTER, SECRETS)
        self.assertEqual(x["server_inbound"]["port"], 24443)
        self.assertEqual(x["server_inbound"]["streamSettings"]["security"], "reality")
        self.assertEqual(x["server_inbound"]["streamSettings"]["realitySettings"]["privateKey"],
                         SECRETS["private_key"])
        self.assertNotIn(SECRETS["private_key"], json.dumps(x["router_outbounds"]))
        self.assertEqual(len(x["router_outbounds"]), 2)

    def test_draft_reality_starts_disabled_and_cannot_render(self):
        config = catalog.starter_catalog("hub.example.org", add_reality=True)
        self.assertFalse(config["profiles"][-1]["enabled"])
        with self.assertRaises(InvalidPlan):
            catalog.candidate_pair(config, "reality-pilot", ROUTER, SECRETS)

    def test_manual_selection_never_auto_proposes_a_switch(self):
        config = self.mixed()
        strong = {key: True for key in catalog.REQUIRED_ACCEPT}
        result = catalog.failover_decision(config, "pilot-new", failures=50,
                                           cooldown_elapsed=True,
                                           probes={"wss-backup": strong})
        self.assertEqual(result["action"], "MANUAL_SELECT_REQUIRED")
        self.assertTrue(result["active_retained"])
        self.assertIsNone(result["target"])

    def test_auto_switch_stays_blocked_without_router_side_candidate(self):
        config = self.mixed()
        config["routers"]["pilot-new"]["mode"] = "automatic_verified"
        verified = {key: True for key in catalog.REQUIRED_ACCEPT}
        verified["candidate_pre_staged"] = False
        result = catalog.failover_decision(config, "pilot-new", failures=4,
                                           cooldown_elapsed=True,
                                           probes={"wss-backup": verified})
        self.assertEqual(result["action"], "NO_SAFE_ALTERNATIVE")
        self.assertTrue(result["active_retained"])

    def test_auto_switch_requires_two_reverse_channels_and_http_ssh(self):
        config = self.mixed()
        config["routers"]["pilot-new"]["mode"] = "automatic_verified"
        full = {key: True for key in catalog.REQUIRED_ACCEPT}
        for failed in catalog.REQUIRED_ACCEPT:
            state = dict(full)
            state[failed] = False
            with self.subTest(failed=failed):
                result = catalog.failover_decision(
                    config, "pilot-new", failures=4,
                    cooldown_elapsed=True, probes={"wss-backup": state})
                self.assertEqual(result["action"], "NO_SAFE_ALTERNATIVE")
        result = catalog.failover_decision(
            config, "pilot-new", failures=4,
            cooldown_elapsed=True, probes={"wss-backup": full})
        self.assertEqual(result["action"], "PILOT_VERIFIED_SWITCH_PROPOSAL")
        self.assertEqual(result["target"], "wss-backup")
        self.assertTrue(result["active_retained"])
        self.assertFalse(result["deployable"])

    def test_hysteresis_and_cooldown_are_enforced(self):
        config = self.mixed()
        config["routers"]["pilot-new"]["mode"] = "automatic_verified"
        full = {key: True for key in catalog.REQUIRED_ACCEPT}
        for failures, cool in ((0, True), (2, True), (5, False)):
            result = catalog.failover_decision(config, "pilot-new",
                                               failures=failures,
                                               cooldown_elapsed=cool,
                                               probes={"wss-backup": full})
            self.assertEqual(result["action"], "KEEP_ACTIVE")

    def test_invalid_secret_material_and_collision_in_catalog_rejected(self):
        for patch in (
            lambda x: x["profiles"][2].update(private_key="not-allowed"),
            lambda x: x["profiles"][2].update(public_port=443),
            lambda x: x["profiles"][0].update(ws_path="/invalid path/"),
            lambda x: x["profiles"][1].update(server_local_port=18444,
                                            ws_path="/different/"),
            lambda x: x["profiles"][1].update(id="wss-primary"),
            lambda x: x["profiles"][0].update(endpoint_host="https://hub.example.org"),
            lambda x: x["routers"]["pilot-new"].update(
                fallback_order=["wss-backup", "wss-backup"]),
        ):
            config = self.mixed()
            patch(config)
            with self.assertRaises(InvalidPlan):
                catalog.validate(config)

    def test_cli_generates_private_non_deployable_catalog(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "profiles.json"
            proc = subprocess.run([
                sys.executable, str(ROOT / "tools" / "orchestrator_profile_catalog.py"),
                "init", "--domain", "hub.example.net",
                "--backup-domain", "backup.example.net", "--add-reality",
                "--out", str(dest),
            ], capture_output=True, text=True, timeout=8)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(dest.stat().st_mode & 0o077, 0)
            config = json.loads(dest.read_text())
            self.assertFalse(config["deployable"])
            self.assertEqual(len(config["profiles"]), 3)
            self.assertFalse(config["profiles"][-1]["enabled"])


if __name__ == "__main__":
    unittest.main()
