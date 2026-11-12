"""Synthetic 6-router full-state Docker rehearsal gate; no Belt access."""
from __future__ import annotations

import io
import json
import pathlib
import sqlite3
import sys
import tarfile
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_belt_docker_migration_gate as gate


def build_fleet(root: pathlib.Path):
    state = root / "live"
    state.mkdir()
    with sqlite3.connect(state / "hub.db") as c:
        c.execute("""
          create table routers(
            id text primary key, vless_uuid text not null,
            ssh_vless_uuid text not null, entry_port integer,
            ssh_entry_port integer, vps_transport text, vless_port integer,
            deleted_at integer default 0, status_json text
          )
        """)
        c.execute("create table router_logs(router_id text, ts integer, line text)")
        c.execute("create table audit_events(ts integer, event text)")
        for n, name in enumerate(("main", "WR3000S_2", "PaxoBs",
                                   "Netis_N6_router5", "Movietonn",
                                   "AX3000T-Shteinberg")):
            mode = "tcp" if n == 5 else "ws"
            c.execute("insert into routers values(?,?,?,?,?,?,?,?,?)",
                      (name, f"uuid-main-{n}", f"uuid-ssh-{n}",
                       18080 + n * 10, 19080 + n * 10,
                       mode, 18443 if mode == "tcp" else 443, 0,
                       '{"connected":true}'))
            c.execute("insert into router_logs values(?,?,?)",
                      (name, 1700000+n, "history is retained"))
        c.execute("insert into audit_events values(?,?)",
                  (1700000, "first deployment"))
        c.commit()
    for filename in gate.MUST_KEEP:
        (state / filename).write_text("PRIVATE_" + filename, encoding="utf-8")
    # Unknown future nested state must not be discarded.
    (state / "custom" / "deep").mkdir(parents=True)
    (state / "custom" / "deep" / "note.json").write_text('{"keep":"yes"}')
    (state / "hub-push-subscriptions.json").write_text('{"keep":"push"}')
    config = {"inbounds": [
        {"tag": "owrt-remote-vless", "settings": {"clients": []}},
        {"tag": "owrt-remote-wss", "settings": {"clients": []}},
    ], "routing": {"rules": []}}
    for n, name in enumerate(("main", "WR3000S_2", "PaxoBs",
                               "Netis_N6_router5", "Movietonn",
                               "AX3000T-Shteinberg")):
        mode = 0 if n == 5 else 1
        for channel, uid in (("", f"uuid-main-{n}"),
                             ("-ssh", f"uuid-ssh-{n}")):
            config["inbounds"][mode]["settings"]["clients"].append({
                "id": uid, "email": name + channel + "@owrt-remote",
                "reverse": {"tag": "reverse-" + name + channel},
            })
            local = "entry-" + name if channel == "" else "ssh-" + name
            port = 18080 + n * 10 if channel == "" else 19080 + n * 10
            config["inbounds"].append({
                "tag": local, "listen": "127.0.0.1",
                "port": port, "protocol": "tunnel", "settings": {}})
            config["routing"]["rules"].append({
                "type": "field", "inboundTag": [local],
                "outboundTag": "reverse-" + name + channel,
            })
    xray = root / "xray.json"
    xray.write_text(json.dumps(config))
    hub_code = root / "hub.py"
    hub_code.write_text("print('fake hub source')\n")
    return state, xray, hub_code


class MigrationGateTests(unittest.TestCase):
    def setUp(self):
        self.root_obj = tempfile.TemporaryDirectory()
        self.addCleanup(self.root_obj.cleanup)
        self.root = pathlib.Path(self.root_obj.name)
        self.state, self.xray, self.hub_code = build_fleet(self.root)

    def test_six_mixed_routers_and_history_count(self):
        inventory, _ = gate.analyze(self.state, self.xray)
        self.assertEqual(inventory["active_count"], 6)
        self.assertEqual(inventory["wss"], 5)
        self.assertEqual(inventory["tcp"], 1)
        self.assertEqual(inventory["tables"]["router_logs"], 6)
        self.assertEqual(inventory["tables"]["audit_events"], 1)

    def test_snapshot_preserves_all_state_secrets_and_history(self):
        db_before = (self.state / "hub.db").read_bytes()
        xray_before = self.xray.read_bytes()
        folder, manifest = gate.snapshot(self.state, self.xray, self.hub_code,
                                         self.root)
        self.assertEqual(folder.stat().st_mode & 0o077, 0)
        self.assertEqual((folder / "state-and-references.tar.gz").stat().st_mode & 0o077, 0)
        self.assertEqual(manifest["database"]["routers"], 6)
        self.assertTrue(all(manifest["required_files_present"].values()))
        self.assertIn("state/custom/deep/note.json", manifest["files_sha256"])
        self.assertIn("state/hub-push-subscriptions.json", manifest["files_sha256"])
        for item in gate.MUST_KEEP:
            self.assertIn("state/" + item, manifest["files_sha256"])
        self.assertIn("reference/hub-source.py", manifest["files_sha256"])
        self.assertIn("reference/xray.json", manifest["files_sha256"])
        ok = gate.inspect_archive(folder)
        self.assertEqual(ok, manifest)
        self.assertEqual((self.state / "hub.db").read_bytes(), db_before)
        self.assertEqual(self.xray.read_bytes(), xray_before)
        with tarfile.open(folder / "state-and-references.tar.gz", "r:gz") as tar:
            snapshot_bytes = tar.extractfile("state/hub.db").read()
            restored = self.root / "test-restore.sqlite"
            restored.write_bytes(snapshot_bytes)
        with sqlite3.connect(restored) as c:
            self.assertEqual(c.execute("select count(*) from router_logs").fetchone()[0], 6)
            self.assertEqual(c.execute("select count(*) from audit_events").fetchone()[0], 1)
            self.assertEqual(c.execute("select count(*) from routers").fetchone()[0], 6)

    def test_live_identity_missing_blocks_cutover_audit(self):
        data = json.loads(self.xray.read_text())
        data["inbounds"][1]["settings"]["clients"] = [
            c for c in data["inbounds"][1]["settings"]["clients"]
            if c["email"] != "Movietonn-ssh@owrt-remote"]
        self.xray.write_text(json.dumps(data))
        with self.assertRaisesRegex(gate.GateError, "Movietonn-ssh"):
            gate.analyze(self.state, self.xray)

    def test_port_conflict_or_wrong_reverse_route_blocks(self):
        original = json.loads(self.xray.read_text())
        for op in ("port", "route", "missing_tcp"):
            data = json.loads(json.dumps(original))
            if op == "port":
                item = next(x for x in data["inbounds"] if x["tag"] == "entry-main")
                item["port"] = 12345
            elif op == "route":
                item = next(x for x in data["routing"]["rules"]
                            if "ssh-main" in x["inboundTag"])
                item["outboundTag"] = "reverse-wrong"
            else:
                data["inbounds"] = [x for x in data["inbounds"]
                                   if x["tag"] != "owrt-remote-vless"]
            with self.subTest(op=op), self.assertRaises(gate.GateError):
                gate.xray_parity(gate.db_inventory(self.state / "hub.db"), data)

    def test_orphan_wss_credential_and_ghost_reverse_port_block(self):
        original = json.loads(self.xray.read_text())
        for variant in ("orphan-client", "orphan-listener", "duplicate-client"):
            cfg = json.loads(json.dumps(original))
            if variant == "orphan-client":
                cfg["inbounds"][1]["settings"]["clients"].append({
                    "id": "forgotten-uuid", "email": "deleted-router@owrt-remote"})
            elif variant == "orphan-listener":
                cfg["inbounds"].append({
                    "tag": "ssh-deleted-router", "listen": "127.0.0.1",
                    "protocol": "tunnel", "port": 19999})
            else:
                cfg["inbounds"][1]["settings"]["clients"].append(
                    cfg["inbounds"][1]["settings"]["clients"][0].copy())
            with self.subTest(variant=variant), self.assertRaises(gate.GateError):
                gate.xray_parity(gate.db_inventory(self.state / "hub.db"), cfg)

    def test_legacy_wss_fallback_credential_is_preserved_if_present(self):
        with sqlite3.connect(self.state / "hub.db") as c:
            c.execute("alter table routers add column vps_allow_legacy integer default 0")
            c.execute("update routers set vps_allow_legacy=1 where id='Movietonn'")
            c.commit()
        cfg = json.loads(self.xray.read_text())
        wss = next(x for x in cfg["inbounds"] if x["tag"] == "owrt-remote-wss")
        tcp = next(x for x in cfg["inbounds"] if x["tag"] == "owrt-remote-vless")
        for row in wss["settings"]["clients"]:
            if row["email"].startswith("Movietonn"):
                tcp["settings"]["clients"].append(row.copy())
        gate.xray_parity(gate.db_inventory(self.state / "hub.db"), cfg)

    def test_symlink_cannot_escape_state_archive(self):
        (self.state / "secret-link").symlink_to(self.xray)
        with self.assertRaises(gate.GateError):
            gate.snapshot(self.state, self.xray, self.hub_code, self.root)

    def test_unknown_sqlite_database_is_refused(self):
        (self.state / "hidden.sqlite").write_text("unknown db")
        with self.assertRaises(gate.GateError):
            gate.record_state_files(self.state)

    def test_missing_auth_or_hold_prevents_snapshot(self):
        (self.state / "agent.token").unlink()
        with self.assertRaisesRegex(gate.GateError, "required"):
            gate.snapshot(self.state, self.xray, self.hub_code, self.root)

    def test_archive_tamper_detected(self):
        folder, _ = gate.snapshot(self.state, self.xray, self.hub_code, self.root)
        manifest = json.loads((folder / "manifest.json").read_text())
        manifest["files_sha256"]["state/hub.db"] = "00" * 32
        (folder / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(gate.GateError):
            gate.inspect_archive(folder)


if __name__ == "__main__":
    unittest.main()
