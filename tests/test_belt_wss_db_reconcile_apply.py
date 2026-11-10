"""Synthetic tests of guarded, targeted SQLite reconciliation and rollback."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import belt_wss_db_reconcile_apply as apply


class ReconcileApplyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "db.sqlite"
        with sqlite3.connect(self.db) as c:
            c.execute("""create table routers (
              id text primary key, vps_transport text, vless_port integer,
              vps_allow_legacy integer, entry_port integer,
              last_seen integer, status_json text, deleted_at integer default 0
            )""")
            c.executemany(
                "insert into routers values (?,?,?,?,?,?,?,0)",
                [("main", "tcp", 18443, 0, 18080, 1, "{}"),
                 ("PaxoBs", "tcp", 18443, 0, 18110, 1, "{}"),
                 ("WR3000S_2", "tcp", 18443, 0, 18100, 1, "{}"),
                 ("Netis_N6_router5", "ws", 443, 0, 18120, 1, "{}"),
                 ("AX3000T-Shteinberg", "tcp", 18443, 0, 18090, 1, "{}")])
        self.targets = {"main", "PaxoBs", "WR3000S_2"}
        self.original = apply.config_rows(self.db)
        self.migrated = {k: dict(v) for k, v in self.original.items()}
        for name in self.targets:
            self.migrated[name].update(
                vps_transport="ws", vless_port=443, vps_allow_legacy=1)

    def test_exact_targeted_apply_and_rollback(self):
        apply.update_transport(self.db, self.original, self.migrated, self.targets)
        self.assertEqual(apply.config_rows(self.db), self.migrated)
        apply.update_transport(self.db, self.migrated, self.original, self.targets)
        self.assertEqual(apply.config_rows(self.db), self.original)

    def test_heartbeat_columns_are_never_lost(self):
        with sqlite3.connect(self.db) as c:
            c.execute("update routers set last_seen=567,status_json='{\"online\":true}'")
        apply.update_transport(self.db, self.original, self.migrated, self.targets)
        with sqlite3.connect(self.db) as c:
            found = c.execute("select distinct last_seen,status_json from routers").fetchall()
        self.assertEqual(found, [(567, '{"online":true}')])

    def test_stale_preflight_is_blocked_without_partial_changes(self):
        with sqlite3.connect(self.db) as c:
            c.execute("update routers set entry_port=2222 where id='PaxoBs'")
        with self.assertRaises(apply.Blocked):
            apply.update_transport(self.db, self.original, self.migrated, self.targets)
        rows = apply.config_rows(self.db)
        self.assertEqual(rows["main"]["vps_transport"], "tcp")
        self.assertEqual(rows["PaxoBs"]["entry_port"], 2222)

    def test_out_of_scope_stage_mutation_rejected(self):
        mutated = {k: dict(v) for k, v in self.migrated.items()}
        mutated["Netis_N6_router5"]["entry_port"] = 8081
        with self.assertRaises(ValueError):
            apply.changed_rows(self.original, mutated, self.targets)

    def test_original_legacy_flag_restored_when_not_zero(self):
        # The updater must not assume a static old fallback value.
        with sqlite3.connect(self.db) as c:
            c.execute("update routers set vps_allow_legacy=1 where id='main'")
        original = apply.config_rows(self.db)
        migrated = {k: dict(v) for k, v in original.items()}
        for name in self.targets:
            migrated[name].update(
                vps_transport="ws", vless_port=443, vps_allow_legacy=1)
        apply.update_transport(self.db, original, migrated, self.targets)
        apply.update_transport(self.db, migrated, original, self.targets)
        self.assertEqual(apply.config_rows(self.db), original)


if __name__ == "__main__":
    unittest.main()
