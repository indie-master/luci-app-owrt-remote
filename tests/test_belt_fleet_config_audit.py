"""Synthetic read-only regression tests for Belt HUB/Xray fleet drift guard."""
import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import belt_fleet_config_audit as audit


class FleetAuditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = self.root / "hub.db"
        self.cfg = self.root / "owrt-remote.json"
        self.routers = [
            ("alpha", 18080, 19080, "alpha-luci", "alpha-ssh", "ws", 443),
            ("bravo", 18100, 19100, "bravo-luci", "bravo-ssh", "tcp", 18443),
        ]
        self.write_db()
        clients = lambda router: [
            {"email": router[0] + "@owrt-remote", "id": router[3]},
            {"email": router[0] + "-ssh@owrt-remote", "id": router[4]},
        ]
        self.data = {
            "inbounds": [
                {"tag": "owrt-remote-vless", "port": 18443,
                 "settings": {"clients": sum((clients(row) for row in self.routers), [])}},
                {"tag": "owrt-remote-wss", "port": 18444,
                 "settings": {"clients": clients(self.routers[0])}},
            ],
            "routing": {"rules": []}
        }
        for row in self.routers:
            rid, lp, sp, *_ = row
            for tag, port, route in (
                ("entry-" + rid, lp, "reverse-" + rid),
                ("ssh-" + rid, sp, "reverse-" + rid + "-ssh")
            ):
                self.data["inbounds"].append({"tag": tag, "port": port, "protocol": "tunnel"})
                self.data["routing"]["rules"].append({"inboundTag": [tag], "outboundTag": route})
        self.write_config()

    def write_db(self):
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("""create table routers (
                id text primary key,
                entry_port integer,
                ssh_entry_port integer,
                vless_uuid text,
                ssh_vless_uuid text,
                vps_transport text,
                vless_port integer,
                deleted_at integer default 0
            )""")
            conn.executemany(
                "insert into routers values (?,?,?,?,?,?,?,0)",
                self.routers
            )
            conn.commit()
        finally:
            conn.close()

    def write_config(self):
        self.cfg.write_text(json.dumps(self.data), encoding="utf-8")

    def check(self, wss=("alpha",), tcp=("bravo",)):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = audit.audit(self.db, self.cfg, set(wss), set(tcp))
        return rc, output.getvalue()

    def test_matching_fleet_passes_without_mutating_db_or_config(self):
        original_db = self.db.read_bytes()
        original_config = self.cfg.read_bytes()
        rc, output = self.check()
        self.assertEqual(rc, 0, output)
        self.assertIn("SUMMARY: checked=2", output)
        self.assertIn("issues=0", output)
        self.assertEqual(self.db.read_bytes(), original_db)
        self.assertEqual(self.cfg.read_bytes(), original_config)

    def test_missing_wss_client_fails(self):
        self.data["inbounds"][1]["settings"]["clients"].pop()
        self.write_config()
        rc, output = self.check()
        self.assertEqual(rc, 2)
        self.assertIn("ws client missing: SSH", output)

    def test_db_transport_stale_blocks_confirmed_wss(self):
        conn = sqlite3.connect(self.db)
        conn.execute("update routers set vps_transport='tcp' where id='alpha'")
        conn.commit()
        conn.close()
        rc, output = self.check()
        self.assertEqual(rc, 2)
        self.assertIn("expected=ws", output)

    def test_duplicate_wss_registration_blocks(self):
        self.data["inbounds"][1]["settings"]["clients"].append(
            self.data["inbounds"][1]["settings"]["clients"][0].copy()
        )
        self.write_config()
        self.assertEqual(self.check()[0], 2)

    def test_conflicting_uuid_blocks_without_secret_output(self):
        secret = "DO-NOT-PRINT-ME"
        self.data["inbounds"][1]["settings"]["clients"][0]["id"] = secret
        self.write_config()
        rc, output = self.check()
        self.assertEqual(rc, 2)
        self.assertIn("UUID mismatch", output)
        self.assertNotIn(secret, output)

    def test_bad_reverse_port_blocks(self):
        self.data["inbounds"][3]["port"] = 1
        self.write_config()
        rc, output = self.check()
        self.assertEqual(rc, 2)
        self.assertIn("SSH reverse port differs", output)

    def test_missing_reverse_rule_blocks(self):
        self.data["routing"]["rules"] = self.data["routing"]["rules"][:-1]
        self.write_config()
        rc, output = self.check()
        self.assertEqual(rc, 2)
        self.assertIn("SSH reverse routing rule missing", output)

    def test_expected_id_not_in_db_blocks(self):
        rc, output = self.check(wss=("alpha", "missing"))
        self.assertEqual(rc, 2)
        self.assertIn("not found in active HUB inventory", output)

    def test_no_dual_auth_inference_without_operator_input(self):
        # A WSS registration by itself is not proof that a router uses WSS.
        rc, output = self.check(wss=(), tcp=("bravo",))
        self.assertEqual(rc, 0, output)


if __name__ == "__main__":
    unittest.main()
