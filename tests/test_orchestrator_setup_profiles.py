"""Isolated first-run Router Orchestrator domain and transport planning tests."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_setup_profiles as planner

ROUTER = {
    "id": "Movietonn",
    "vless_uuid": "11111111-1111-4111-8111-111111111111",
    "ssh_vless_uuid": "22222222-2222-4222-8222-222222222222",
}
SECRETS = {
    "private_key": "a" * 43,
    "public_key": "b" * 43,
    "short_id": "0123456789abcdef",
}


class SetupProfileTests(unittest.TestCase):
    def plan(self):
        return planner.new_plan("sprt-wrt.cdnvc.xyz")

    def test_first_install_defaults_without_fixed_swiftless_domain(self):
        p = planner.new_plan("my-hub.example.net", hub_version="26.7.28",
                             router_version="26.10.21")
        self.assertEqual(p["public_url"], "https://my-hub.example.net")
        self.assertFalse(p["reality"]["enabled"])
        self.assertEqual(p["default_profile"], "wss_tls")
        self.assertFalse(p["xray"]["auto_update"])
        self.assertEqual(p["xray"]["hub_version"], "26.7.28")
        self.assertEqual(planner.host_only("https://MY-HUB.example.net:443/path"),
                         "my-hub.example.net")

    def test_wss_staged_server_and_router_outbounds_match(self):
        p = self.plan()
        data = planner.paired_reverse(p, "wss_tls", ROUTER)
        self.assertFalse(data["deployable"])
        inbound = data["server_inbound"]
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["port"], 18444)
        self.assertEqual(inbound["streamSettings"]["security"], "none")
        self.assertEqual(inbound["streamSettings"]["wsSettings"]["path"], "/owrt-ws/")
        self.assertEqual(len(inbound["settings"]["clients"]), 2)
        for out, client in zip(data["router_outbounds"], inbound["settings"]["clients"]):
            self.assertEqual(out["settings"]["id"], client["id"])
            self.assertEqual(out["settings"]["address"], "sprt-wrt.cdnvc.xyz")
            self.assertEqual(out["settings"]["port"], 443)
            self.assertEqual(out["streamSettings"]["security"], "tls")
            self.assertEqual(out["streamSettings"]["wsSettings"]["path"], "/owrt-ws/")
            self.assertFalse(out["streamSettings"]["tlsSettings"]["allowInsecure"])

    def test_reality_private_keys_only_in_server_fragment(self):
        p = self.plan()
        p["reality"].update(enabled=True, target="cdn.example.org:443",
                            server_name="cdn.example.org",
                            server_names=["cdn.example.org"])
        data = planner.paired_reverse(p, "reality_raw", ROUTER, SECRETS)
        ins = data["server_inbound"]
        self.assertEqual(ins["listen"], "0.0.0.0")
        self.assertEqual(ins["port"], 24443)
        self.assertEqual(ins["streamSettings"]["security"], "reality")
        self.assertEqual(ins["streamSettings"]["network"], "raw")
        self.assertEqual(ins["streamSettings"]["realitySettings"]["privateKey"],
                         SECRETS["private_key"])
        self.assertEqual(ins["streamSettings"]["realitySettings"]["target"],
                         "cdn.example.org:443")
        self.assertEqual(ins["streamSettings"]["realitySettings"]["serverNames"],
                         ["cdn.example.org"])
        for out in data["router_outbounds"]:
            st = out["streamSettings"]
            self.assertEqual(st["network"], "raw")
            self.assertEqual(st["security"], "reality")
            self.assertEqual(st["realitySettings"]["serverName"], "cdn.example.org")
            self.assertEqual(st["realitySettings"]["shortId"], SECRETS["short_id"])
            self.assertEqual(st["realitySettings"]["password"], SECRETS["public_key"])
            self.assertNotIn(SECRETS["private_key"], json.dumps(out))
            self.assertNotIn("target", st["realitySettings"])
        self.assertFalse(data["deployable"])

    def test_reality_refuses_missing_keys_and_bad_shortid(self):
        p = self.plan()
        p["reality"]["enabled"] = True
        with self.assertRaises(planner.InvalidPlan):
            planner.paired_reverse(p, "reality_raw", ROUTER)
        for sid in ("", "abc", "0123456789abcdef00", "zzzz"):
            secrets = {**SECRETS, "short_id": sid}
            with self.subTest(short_id=sid), self.assertRaises(planner.InvalidPlan):
                planner.paired_reverse(p, "reality_raw", ROUTER, secrets)

    def test_reality_sni_and_frontend_port_collisions_refused(self):
        for change in (
            lambda x: x["reality"].update(port=443),
            lambda x: x["reality"].update(port=18444),
            lambda x: x["reality"].update(server_name="not-allowed.example.org"),
            lambda x: x["reality"].update(target="https://example.org:443"),
            lambda x: x.update(default_profile="reality_raw"),
            lambda x: x["xray"].update(auto_update=True),
            lambda x: x.update(domain="https://sprt-wrt.cdnvc.xyz"),
        ):
            p = self.plan()
            change(p)
            with self.assertRaises(planner.InvalidPlan):
                planner.validate(p)

    def test_duplicate_router_credentials_refused(self):
        p = self.plan()
        router = {**ROUTER, "ssh_vless_uuid": ROUTER["vless_uuid"]}
        with self.assertRaises(planner.InvalidPlan):
            planner.paired_reverse(p, "wss_tls", router)

    def test_offline_plan_is_private_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "setup.json"
            plan = self.plan()
            planner.safe_write(path, json.dumps(plan))
            self.assertEqual(path.stat().st_mode & 0o077, 0)
            with self.assertRaises(planner.InvalidPlan):
                planner.safe_write(path, "overwrite")
            self.assertEqual(json.loads(path.read_text()), plan)

    def test_cli_first_run_and_preview_never_write_server_configs(self):
        with tempfile.TemporaryDirectory() as td:
            plan = Path(td) / "hub-plan.json"
            script = ROOT / "tools" / "orchestrator_setup_profiles.py"
            result = subprocess.run(
                [sys.executable, str(script), "init",
                 "--domain", "hub.example.net", "--output", str(plan)],
                capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ONLY STAGED", result.stdout)
            result = subprocess.run([sys.executable, str(script), "preview",
                                     str(plan)], capture_output=True,
                                     text=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DEPLOYABLE: NO", result.stdout)
            self.assertIn("HTTPS=https://hub.example.net", result.stdout)


if __name__ == "__main__":
    unittest.main()
