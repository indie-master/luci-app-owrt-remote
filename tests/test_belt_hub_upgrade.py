"""Offline checks for guarded, Hub-only deployment and WSS DB migration."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "deploy_belt_hub_v119.py"
HUB = ROOT / "vps" / "owrt-remote-hub.py"


spec = importlib.util.spec_from_file_location("belt_upgrade", SCRIPT)
up = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = up
spec.loader.exec_module(up)


class GuardedUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="belt-upgrade-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "db.sqlite"
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "create table routers (id text primary key, deleted_at integer, "
                "vless_port integer, vless_uuid text, ssh_vless_uuid text)"
            )
            for rid in ("old-a", "old-b", "old-c", "old-d", "already-wss"):
                conn.execute(
                    "insert into routers values (?,?,?,?,?)",
                    (rid, 0, 18443, rid + "-admin-uuid", rid + "-ssh-uuid"),
                )
            conn.commit()

    def test_db_migration_preserves_existing_uuid_and_legacy_ports(self):
        with sqlite3.connect(self.db) as conn:
            old = conn.execute(
                "select id,vless_uuid,ssh_vless_uuid from routers order by id"
            ).fetchall()
        up.migrate_db(self.db, "already-wss")
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "select id,vless_port,vps_transport,vps_allow_legacy,"
                "vless_uuid,ssh_vless_uuid from routers order by id"
            ).fetchall()
        self.assertEqual([(x[0], x[4], x[5]) for x in rows], old)
        self.assertEqual(sum(r[2] == "ws" for r in rows), 1)
        for row in rows:
            self.assertEqual(
                (row[1], row[2], row[3]),
                (443, "ws", 0) if row[0] == "already-wss" else (18443, "tcp", 0),
            )
        # Repeat migration to prove idempotence.
        up.migrate_db(self.db, "already-wss")

    def test_refuses_migration_with_unexpected_current_wss_port(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("update routers set vless_port=55555 where id='already-wss'")
        with self.assertRaises(up.UpgradeError):
            up.migrate_db(self.db, "already-wss")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("select vless_port from routers where id='already-wss'").fetchone()[0], 55555)

    def test_apply_stops_only_hub_and_leaves_xray_maintenance_hold(self):
        source = self.root / "new-hub.py"
        installed = self.root / "old-hub.py"
        source.write_text("print('new')\n")
        installed.write_text("print('old')\n")
        info = {
            "source": source, "installed": installed, "db": self.db,
            "ws_router": "already-wss",
            "xray_config": self.root / "current-xray.json",
        }
        args = type("Args", (), {"backup_root": self.root})()
        calls = []

        def command(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def backup(_args, _info, folder):
            folder.mkdir()
            (folder / "manifest.json").write_text("{}")

        with mock.patch.object(up, "inspect", return_value=info), \
             mock.patch.object(up, "perform_staging"), \
             mock.patch.object(up, "call", side_effect=command), \
             mock.patch.object(up, "backup_files", side_effect=backup), \
             mock.patch.object(up, "migrate_db", wraps=up.migrate_db), \
             mock.patch.object(up, "validate_render"), \
             mock.patch.object(up, "verify_xray_untouched"), \
             mock.patch.object(up, "health", return_value=True), \
             mock.patch.object(up, "is_active", return_value=False):
            up.apply(args)
        self.assertEqual(installed.read_text(), source.read_text())
        self.assertEqual(calls, [
            ["systemctl", "stop", "owrt-remote"],
            ["systemctl", "start", "owrt-remote"],
        ])
        self.assertTrue((self.db.parent / ".xray-maintenance-hold").exists())

    def test_all_xray_restarts_are_guarded_during_maintenance(self):
        source = HUB.read_text(encoding="utf-8")
        self.assertIn('_XRAY_MAINTENANCE_HOLD = STATE_DIR / ".xray-maintenance-hold"', source)
        self.assertIn("def assert_xray_mutation_allowed():", source)
        reload_start = source.index("def reload_vps_xray(")
        restart_start = source.index("def restart_vps_xray(")
        self.assertIn("assert_xray_mutation_allowed()", source[reload_start:reload_start+330])
        self.assertIn("assert_xray_mutation_allowed()", source[restart_start:restart_start+180])


if __name__ == "__main__":
    unittest.main()
