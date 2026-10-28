"""Offline integration tests: Belt staging must never mutate the source DB/config."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vps" / "owrt-remote-hub.py"
STAGER = ROOT / "tools" / "stage_belt_wss.py"
state = tempfile.TemporaryDirectory(prefix="test-stage-hub-state-")
spec = importlib.util.spec_from_file_location("stage_belt_hub", SOURCE)
hub = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hub
with mock.patch.dict(os.environ, {"OWRT_REMOTE_STATE_DIR": state.name}):
    spec.loader.exec_module(hub)

NAMES = ("primary", "node-a", "node-b", "backup", "wss-router")


class BeltOfflineStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-belt-stage-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "hub.db"
        self.live = self.root / "live.json"
        self.bin = self.root / "xray-test"
        self.bin.write_text("#!/bin/sh\necho 'Configuration OK.'\n", encoding="utf-8")
        self.bin.chmod(0o700)
        with hub.connect(self.db) as db:
            hub.init_db(db)
            for index, rid in enumerate(NAMES):
                hub.upsert_router(db, {
                    "id": rid,
                    "name": rid,
                    "vps_transport": "tcp",
                    "vless_port": 18443,
                    "vps_host": "hub.example",
                    "entry_port": 18080 + index * 10,
                    "ssh_entry_port": 19080 + index * 10,
                    "vless_uuid": str(uuid.uuid4()),
                    "ssh_vless_uuid": str(uuid.uuid4()),
                })
            rows = [dict(r) for r in hub.list_router_rows(db)]
            for r in rows:
                if r["id"] == "wss-router":
                    r["vps_transport"] = "ws"
                    r["vless_port"] = 443
        live_json = hub.make_server_xray_config(rows, listen_port=18443)
        self.live.write_text(json.dumps(live_json, ensure_ascii=False, indent=2) + "\n")
        self.before_hash = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.live_before_hash = hashlib.sha256(self.live.read_bytes()).hexdigest()

    def stage(self, *extra_args):
        return subprocess.run(
            [sys.executable, str(STAGER), "--db", str(self.db),
             "--live", str(self.live), "--hub-source", str(SOURCE),
             "--xray-bin", str(self.bin), "--output-root", str(self.root), *extra_args],
            capture_output=True, text=True, timeout=60
        )

    def test_produces_exact_working_config_without_live_changes(self):
        result = self.stage()
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertIn("Semantic match with currently working Xray JSON: YES", result.stdout)
        self.assertIn("SAFE STAGING PASSED", result.stdout)
        self.assertEqual(self.before_hash, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(self.live_before_hash, hashlib.sha256(self.live.read_bytes()).hexdigest())
        staged = list(self.root.glob("owrt-wss-v119-*/candidate.json"))
        self.assertEqual(len(staged), 1)
        self.assertEqual(json.loads(staged[0].read_text()), json.loads(self.live.read_text()))
        staged_db = staged[0].with_name("hub-snapshot.db")
        with sqlite3.connect(staged_db) as db:
            self.assertEqual(db.execute("select count(*) from routers where vps_transport='ws'").fetchone()[0], 1)
            self.assertEqual(db.execute(
                "select vless_port from routers where id=?", ("wss-router",)).fetchone()[0], 443)

    def test_route_order_difference_does_not_reject_safe_fallback(self):
        live = json.loads(self.live.read_text(encoding="utf-8"))
        # Every reverse selector is disjoint, and the live server may have
        # been assembled in a different order than the DB renderer.
        live["routing"]["rules"] = list(reversed(live["routing"]["rules"]))
        self.live.write_text(json.dumps(live, ensure_ascii=False, indent=2) + "\\n")
        live_hash = hashlib.sha256(self.live.read_bytes()).hexdigest()
        result = self.stage("--simulate-fallback")
        self.assertEqual(result.returncode, 0, result.stderr + "\\n" + result.stdout)
        self.assertIn("Temporary fallback simulated: SNAPSHOT ONLY", result.stdout)
        self.assertEqual(hashlib.sha256(self.live.read_bytes()).hexdigest(), live_hash)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.before_hash)

    def test_overlapping_routes_fail_closed_in_fallback(self):
        live = json.loads(self.live.read_text(encoding="utf-8"))
        live["routing"]["rules"].append(dict(live["routing"]["rules"][0]))
        self.live.write_text(json.dumps(live, indent=2) + "\\n")
        result = self.stage("--simulate-fallback")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("routing", result.stderr.lower())
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), self.before_hash)

    def test_simulated_dual_transport_fallback_keeps_live_unchanged(self):
        result = self.stage("--simulate-fallback")
        self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        self.assertIn("Temporary fallback simulated: SNAPSHOT ONLY", result.stdout)
        self.assertIn("Existing TCP clients: 8, preserved unchanged", result.stdout)
        self.assertIn("WSS clients: 4", result.stdout)
        self.assertEqual(self.before_hash, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(self.live_before_hash, hashlib.sha256(self.live.read_bytes()).hexdigest())
        candidate = list(self.root.glob("owrt-wss-v119-*/fallback-rehearsal.json"))
        self.assertEqual(len(candidate), 1)
        config = json.loads(candidate[0].read_text())
        clients = {x["tag"]: x["settings"]["clients"] for x in config["inbounds"]
                   if x["tag"] in ("owrt-remote-vless", "owrt-remote-wss")}
        self.assertEqual(len(clients["owrt-remote-vless"]), 8)
        self.assertEqual(len(clients["owrt-remote-wss"]), 4)

    def test_mismatched_live_client_refuses_stage(self):
        live = json.loads(self.live.read_text())
        ws = next(x for x in live["inbounds"] if x["tag"] == "owrt-remote-wss")
        ws["settings"]["clients"][0]["id"] = str(uuid.uuid4())
        self.live.write_text(json.dumps(live, indent=2) + "\n")
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DO NOT DEPLOY", result.stderr)
        self.assertEqual(self.before_hash, hashlib.sha256(self.db.read_bytes()).hexdigest())

    def test_missing_router_refuses_stage(self):
        with hub.connect(self.db) as db:
            db.execute("update routers set deleted_at=10 where id='backup'")
            db.commit()
        db_after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        result = self.stage()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unexpected number of active DB routers", result.stderr)
        self.assertEqual(db_after, hashlib.sha256(self.db.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
