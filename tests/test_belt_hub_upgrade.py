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

    def test_stop_command_error_still_attempts_recovery(self):
        source = self.root / "candidate.py"
        installed = self.root / "installed.py"
        source.write_text("print('new')\n")
        installed.write_text("print('old')\n")
        info = {"source": source, "installed": installed, "db": self.db}
        args = type("Args", (), {"backup_root": self.root})()
        commands = []

        def fake_call(command, **kwargs):
            commands.append(command)
            if command == ["systemctl", "stop", "owrt-remote"]:
                raise up.UpgradeError("systemctl stop returned failure after shutting down")
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(up, "inspect", return_value=info), \
             mock.patch.object(up, "perform_staging"), \
             mock.patch.object(up, "call", side_effect=fake_call):
            with self.assertRaises(up.UpgradeError):
                up.apply(args)
        self.assertEqual(commands, [
            ["systemctl", "stop", "owrt-remote"],
            ["systemctl", "start", "owrt-remote"],
        ])
        self.assertEqual(installed.read_text(), "print('old')\n")

    def test_rollback_restores_code_database_and_clears_own_hold(self):
        old_hub = self.root / "old-hub.py"
        old_hub.write_text("print('old')\n")
        installed = self.root / "installed.py"
        installed.write_text("print('new')\n")
        xray_bin = self.root / "xray"
        xray_bin.write_text("core 26.3.27")
        xray_config = self.root / "xray.json"
        xray_config.write_text('{"network":"tcp","security":"none"}')
        backup_dir = self.root / "backup"
        backup_dir.mkdir()
        before_db = backup_dir / "db-before.sqlite"
        up.sqlite_backup(self.db, before_db)
        (backup_dir / "hub-before.py").write_bytes(old_hub.read_bytes())
        hold = self.root / ".xray-maintenance-hold"
        hold.write_text("maintenance")
        metadata = {
            "installed": str(installed),
            "db": str(self.db),
            "xray_config": str(xray_config),
            "xray_json_sha": up.sha256(xray_config),
            "xray_sha": up.sha256(xray_bin),
            "hub_before_sha": up.sha256(old_hub),
            "maintenance_hold_created": True,
            "maintenance_hold": str(hold),
        }
        (backup_dir / "manifest.json").write_text(json.dumps(metadata))
        up.migrate_db(self.db, "already-wss")
        commands = []
        def call(cmd, **kwargs):
            commands.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        args = type("Args", (), {"xray_bin": xray_bin})()
        with mock.patch.object(up, "call", side_effect=call), \
             mock.patch.object(up, "is_active", side_effect=lambda s: s == up.XRAY_SERVICE or (s == up.HUB_SERVICE and ["systemctl", "start", "owrt-remote"] in commands)), \
             mock.patch.object(up, "health", return_value=True):
            up.restore_from_backup(args, backup_dir)
        self.assertEqual(installed.read_bytes(), old_hub.read_bytes())
        self.assertFalse(hold.exists())
        with sqlite3.connect(self.db) as conn:
            self.assertNotIn("vps_transport",
                             {x[1] for x in conn.execute("pragma table_info(routers)")})
        self.assertEqual(commands, [
            ["systemctl", "stop", "owrt-remote"],
            ["systemctl", "start", "owrt-remote"],
        ])

    def test_live_hub_refuses_both_xray_operations_when_hold_exists(self):
        import os
        from unittest import mock as m
        with tempfile.TemporaryDirectory(prefix="wss-hold-test-") as path:
            name = "guarded_hub_test_" + str(id(self))
            spec = importlib.util.spec_from_file_location(name, HUB)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            with m.patch.dict(os.environ, {"OWRT_REMOTE_STATE_DIR": path}):
                spec.loader.exec_module(module)
            (Path(path) / ".xray-maintenance-hold").write_text("hold")
            with m.patch.object(module.subprocess, "run") as subprocess_mock:
                with self.assertRaises(RuntimeError):
                    module.reload_vps_xray()
                with self.assertRaises(RuntimeError):
                    module.restart_vps_xray()
                subprocess_mock.assert_not_called()

    def test_wan_reconnect_does_not_restart_xray_by_default(self):
        import os
        from unittest import mock as m
        with tempfile.TemporaryDirectory(prefix="wss-no-wan-restart-") as state:
            name = "wan_reconnect_test_" + str(id(self))
            spec = importlib.util.spec_from_file_location(name, HUB)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            with m.patch.dict(os.environ, {
                "OWRT_REMOTE_STATE_DIR": state
            }):
                os.environ.pop("OWRT_REMOTE_RESTART_XRAY_ON_WAN_RECONNECT", None)
                spec.loader.exec_module(module)
                with m.patch.object(module, "restart_vps_xray") as restart:
                    result = module.maybe_restart_vps_xray_after_wan_reconnect("synthetic")
                    self.assertEqual(result, {"skipped": "disabled"})
                    restart.assert_not_called()

    def test_wan_reconnect_explicit_opt_in_still_guarded_by_maintenance_hold(self):
        import os
        from unittest import mock as m
        with tempfile.TemporaryDirectory(prefix="wss-wan-hold-") as state:
            name = "wan_reconnect_hold_" + str(id(self))
            spec = importlib.util.spec_from_file_location(name, HUB)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            with m.patch.dict(os.environ, {
                "OWRT_REMOTE_STATE_DIR": state,
                "OWRT_REMOTE_RESTART_XRAY_ON_WAN_RECONNECT": "1",
            }):
                spec.loader.exec_module(module)
                (Path(state) / ".xray-maintenance-hold").write_text("hold")
                with m.patch.object(module, "active_ssh_http_sessions", return_value=0), \
                     m.patch.object(module, "restart_vps_xray", wraps=module.restart_vps_xray):
                    with self.assertRaisesRegex(RuntimeError, "locked"):
                        module.maybe_restart_vps_xray_after_wan_reconnect("synthetic")

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
