"""Synthetic Docker shadow-restore tests. No Docker, Belt or network required."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import orchestrator_belt_docker_migration_gate as gate
import orchestrator_docker_rehearsal_restore as restore
from test_orchestrator_belt_docker_migration_gate import build_fleet


class RestoreRehearsalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state, self.xray, self.hub_source = build_fleet(self.root)
        self.snapshot, self.manifest = gate.snapshot(
            self.state, self.xray, self.hub_source, self.root)

    def test_snapshot_restores_every_state_file_into_private_shadow(self):
        original = {p.relative_to(self.state).as_posix(): p.read_bytes()
                    for p in self.state.rglob("*") if p.is_file()}
        xray_hash = gate.file_sha(self.xray)
        folder, report = restore.restore(self.snapshot, self.root)
        self.assertEqual(folder.stat().st_mode & 0o077, 0)
        self.assertEqual(report["routers"], 6)
        self.assertEqual(report["wss"], 5)
        self.assertEqual(report["tcp"], 1)
        self.assertEqual(report["file_count"], len(self.manifest["files_sha256"]))
        self.assertEqual(report["xray_sha256"], xray_hash)
        for name, contents in original.items():
            if name in {"hub.db-wal", "hub.db-shm", "hub.db-journal"}:
                continue
            path = folder / "state" / name
            self.assertTrue(path.is_file(), name)
            if name != "hub.db":
                self.assertEqual(path.read_bytes(), contents)
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        with sqlite3.connect(folder / "state" / "hub.db") as db:
            self.assertEqual(db.execute("select count(*) from routers").fetchone()[0], 6)
            self.assertEqual(db.execute("select count(*) from router_logs").fetchone()[0], 6)
            self.assertEqual(db.execute("select count(*) from audit_events").fetchone()[0], 1)
            self.assertEqual(db.execute("pragma integrity_check").fetchone()[0], "ok")
        self.assertEqual(gate.file_sha(self.xray), xray_hash)
        self.assertEqual(
            {p.relative_to(self.state).as_posix(): p.read_bytes()
             for p in self.state.rglob("*") if p.is_file()}, original)
        self.assertEqual(restore.verify_staged(folder), report)

    def test_rehearsal_state_writes_cannot_affect_original_state(self):
        folder, _ = restore.restore(self.snapshot, self.root)
        auth = folder / "state" / "hub-auth.json"
        auth.write_text('{"shadow":"changed"}')
        self.assertEqual((self.state / "hub-auth.json").read_text(),
                         "PRIVATE_hub-auth.json")
        with self.assertRaises(restore.Blocked):
            restore.verify_staged(folder)

    def test_mismatched_archive_digest_refuses_restore(self):
        with (self.snapshot / "manifest.json").open("r+") as f:
            data = json.load(f)
            data["files_sha256"]["state/hub.db"] = "0" * 64
            f.seek(0)
            json.dump(data, f)
            f.truncate()
        before = set(self.root.iterdir())
        with self.assertRaises(gate.GateError):
            restore.restore(self.snapshot, self.root)
        self.assertEqual(set(self.root.iterdir()), before)

    def test_world_visible_or_symlink_snapshots_are_rejected(self):
        self.snapshot.chmod(0o755)
        with self.assertRaises(restore.Blocked):
            restore.restore(self.snapshot, self.root)
        self.snapshot.chmod(0o700)
        link = self.root / "sym-snapshot"
        link.symlink_to(self.snapshot, target_is_directory=True)
        with self.assertRaises(restore.Blocked):
            restore.restore(link, self.root)

    def test_missing_history_table_in_staged_copy_detected(self):
        folder, _ = restore.restore(self.snapshot, self.root)
        with sqlite3.connect(folder / "state" / "hub.db") as conn:
            conn.execute("delete from router_logs")
        with self.assertRaises(restore.Blocked):
            restore.verify_staged(folder)

    def test_extracted_state_keeps_mandatory_hold(self):
        folder, _ = restore.restore(self.snapshot, self.root)
        self.assertTrue((folder / "state" / ".xray-maintenance-hold").is_file())
        self.assertEqual((folder / "state" / ".xray-maintenance-hold").stat().st_mode & 0o077, 0)

    def test_archive_references_never_mount_to_hub_state(self):
        folder, _ = restore.restore(self.snapshot, self.root)
        self.assertFalse((folder / "state" / "xray.json").exists())
        self.assertTrue((folder / "reference" / "xray.json").exists())
        self.assertTrue((folder / "reference" / "hub-source.py").exists())

    def test_bad_output_parent_permissions_block_before_creating_snapshot(self):
        out = self.root / "unsafe_parent"
        out.mkdir(mode=0o777)
        out.chmod(0o777)
        with self.assertRaises(restore.Blocked):
            restore.restore(self.snapshot, out)
        self.assertEqual(list(out.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
