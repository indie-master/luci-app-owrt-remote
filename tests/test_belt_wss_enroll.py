"""Regression tests for Belt-side WSS ID enrollment (no real Xray or service)."""
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "tools" / "belt_wss_enroll.sh"

@unittest.skipUnless(shutil.which("jq"), "jq required for Belt enrollment tests")
class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.cfg = self.root / "owrt-remote.json"
        self.xray = self.root / "fake-xray"
        self.xray.write_text("#!/bin/sh\nexit 0\n")
        self.xray.chmod(0o700)
        self.data = {"inbounds": [
            {"tag": "owrt-remote-vless", "protocol": "vless",
             "settings": {"clients": [
                 {"email": "main@owrt-remote", "id": "main-1", "flow": ""},
                 {"email": "main-ssh@owrt-remote", "id": "main-2", "flow": ""},
                 {"email": "other@owrt-remote", "id": "other"}
             ]}},
            {"tag": "owrt-remote-wss", "protocol": "vless",
             "settings": {"clients": [
                 {"email": "Netis_N6_router5@owrt-remote", "id": "netis-1"}
             ]}}
        ], "routing": {"domainStrategy": "AsIs"}}
        self.save()

    def save(self):
        self.cfg.write_text(json.dumps(self.data))

    def run_script(self, *args):
        return subprocess.run(
            ["bash", str(SCRIPT), "--config", str(self.cfg),
             "--xray", str(self.xray), "--router-id", "main", *args],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def wss_clients(self):
        return json.loads(self.cfg.read_text())["inbounds"][1]["settings"]["clients"]

    def test_dry_run_does_not_write_source(self):
        before = self.cfg.read_bytes()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_install_appends_exactly_two_matching_clients_and_keeps_backup(self):
        r = self.run_script("--install")
        self.assertEqual(r.returncode, 0, r.stderr)
        clients = self.wss_clients()
        self.assertEqual([x["email"] for x in clients],
                         ["Netis_N6_router5@owrt-remote",
                          "main@owrt-remote", "main-ssh@owrt-remote"])
        self.assertEqual(clients[1]["flow"], "")
        self.assertEqual(json.loads(self.cfg.read_text())["routing"], self.data["routing"])
        self.assertEqual(len(list(self.root.glob("owrt-remote.json.bak.wss-*"))), 1)
        again = self.run_script()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("ALREADY PRESENT", again.stdout)

    def test_missing_client_blocks(self):
        self.data["inbounds"][0]["settings"]["clients"].pop(1)
        self.save()
        self.assertNotEqual(self.run_script().returncode, 0)

    def test_conflicting_wss_uuid_blocks(self):
        self.data["inbounds"][1]["settings"]["clients"].append(
            {"email": "evil@owrt-remote", "id": "main-1"})
        self.save()
        self.assertNotEqual(self.run_script().returncode, 0)

    def test_conflicting_identity_blocks(self):
        self.data["inbounds"][1]["settings"]["clients"].append(
            {"email": "main@owrt-remote", "id": "not-main-1"})
        self.save()
        self.assertNotEqual(self.run_script().returncode, 0)


if __name__ == "__main__":
    unittest.main()
