"""Guarded post-migration Hub Python update; never changes production Xray."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/update_belt_hub_code_only.py"
FIXTURE = ROOT / "tests/test_stage_belt_wss.py"
spec = importlib.util.spec_from_file_location("guarded_code_only", SCRIPT)
up = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = up
spec.loader.exec_module(up)

spec2 = importlib.util.spec_from_file_location("hub_test_fixture_for_code", FIXTURE)
fixture = importlib.util.module_from_spec(spec2)
sys.modules[spec2.name] = fixture
spec2.loader.exec_module(fixture)


class CodeOnlyTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.BeltOfflineStageTests(
            "test_produces_exact_working_config_without_live_changes")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.root = self.case.root
        with sqlite3.connect(self.case.db) as conn:
            conn.execute("update routers set vps_transport='ws', vless_port=443 "
                         "where id='wss-router'")
            conn.commit()
        self.installed = self.root / "old-hub.py"
        self.installed.write_text("print('old hub')\n")
        self.args = SimpleNamespace(
            source=ROOT / "vps/owrt-remote-hub.py",
            installed=self.installed,
            db=self.case.db,
            xray_config=self.case.live,
            xray_bin=self.case.bin,
            backup_root=self.root,
        )
        self.hold = self.case.db.parent / ".xray-maintenance-hold"
        self.hold.write_text("maintenance\n")
        self.core_sha = up.digest(self.args.xray_bin)
        self.json_sha = up.digest(self.args.xray_config)
        self.db_sha = up.digest(self.args.db)
        self.calls = []
        self.hub_running = True

        def fake_execute(*cmd, **kwargs):
            self.calls.append(cmd)
            if cmd[:2] == ("systemctl", "is-active"):
                svc = cmd[-1]
                rc = 0 if svc == up.XRAY_SERVICE or (svc == up.HUB_SERVICE and self.hub_running) else 3
                return subprocess.CompletedProcess(cmd, rc, "", "")
            if cmd[:2] == ("systemctl", "show"):
                return subprocess.CompletedProcess(cmd, 0, "54321\n", "")
            if cmd[:2] == ("systemctl", "cat"):
                return subprocess.CompletedProcess(cmd, 0, "Unit text\n", "")
            if cmd[:2] == ("systemctl", "stop"):
                self.hub_running = False
                return subprocess.CompletedProcess(cmd, 0, "", "")
            if cmd[:2] == ("systemctl", "start"):
                self.hub_running = True
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=kwargs.get("timeout", 35))

        self.exec_patch = mock.patch.object(up, "execute", side_effect=fake_execute)
        self.exec_patch.start()
        self.addCleanup(self.exec_patch.stop)
        self.health_patch = mock.patch.object(up, "health", return_value=True)
        self.health_patch.start()
        self.addCleanup(self.health_patch.stop)
        self.listen_patch = mock.patch.object(up, "listen", return_value=True)
        self.listen_patch.start()
        self.addCleanup(self.listen_patch.stop)

    def test_check_is_read_only_and_rejects_bad_core_mutation(self):
        stage = self.root / "preflight"
        result = up.inspect(self.args, stage)
        self.assertEqual(result["xray_pid"], 54321)
        self.assertEqual(self.db_sha, up.digest(self.args.db))
        self.assertEqual(self.json_sha, up.digest(self.args.xray_config))
        self.assertEqual(self.core_sha, up.digest(self.args.xray_bin))
        self.assertEqual(self.installed.read_text(), "print('old hub')\n")
        self.assertTrue(self.hold.exists())
        self.assertFalse(any(cmd[:2] == ("systemctl", "stop") for cmd in self.calls))

    def test_apply_swaps_only_hub_and_preserves_xray_and_db(self):
        up.apply(self.args)
        self.assertEqual(self.installed.read_bytes(), self.args.source.read_bytes())
        self.assertEqual(self.db_sha, up.digest(self.args.db))
        self.assertEqual(self.json_sha, up.digest(self.args.xray_config))
        self.assertEqual(self.core_sha, up.digest(self.args.xray_bin))
        self.assertTrue(self.hold.exists())
        self.assertTrue(self.hub_running)
        self.assertEqual(
            [cmd for cmd in self.calls if cmd[:2] in
             (("systemctl", "stop"), ("systemctl", "start"))],
            [("systemctl", "stop", up.HUB_SERVICE),
             ("systemctl", "start", up.HUB_SERVICE)]
        )
        backups = list(self.root.glob("owrt-hub-code-stage-*/files/manifest.json"))
        self.assertEqual(len(backups), 1)

    def test_failed_health_restores_previous_python(self):
        new_health = mock.patch.object(up, "health", side_effect=[True, True, False, True])
        # inspect calls health() twice; new revision check fails; rollback health passes.
        with new_health:
            with self.assertRaises(up.GuardError):
                up.apply(self.args)
        self.assertEqual(self.installed.read_text(), "print('old hub')\n")
        # SQLite backup restores logical content, not necessarily identical
        # page layout / file SHA-256.
        with sqlite3.connect(self.args.db) as conn:
            rows = conn.execute(
                "select id, vps_transport, vless_port, vless_uuid, ssh_vless_uuid "
                "from routers order by id"
            ).fetchall()
            self.assertEqual(len(rows), 5)
            self.assertEqual(sum(row[1] == "ws" and row[2] == 443 for row in rows), 1)
            self.assertEqual(sum(row[1] == "tcp" and row[2] == 18443 for row in rows), 4)
            self.assertEqual(conn.execute("pragma integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.json_sha, up.digest(self.args.xray_config))
        self.assertTrue(self.hold.exists())

    def test_core_sha_or_xray_pid_change_stops_apply(self):
        baseline = up.inspect(self.args, self.root / "preflight")
        self.args.xray_bin.write_text("replaced core")
        with self.assertRaises(up.GuardError):
            up.assert_unchanged(self.args, baseline)


if __name__ == "__main__":
    unittest.main()
