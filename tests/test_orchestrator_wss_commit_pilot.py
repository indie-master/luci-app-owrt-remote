"""Offline tests of the operator-gated one-router Xray commit transaction."""
import contextlib
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
import orchestrator_wss_commit_pilot as pilot


class PilotTests(unittest.TestCase):
    def workspace(self, directory: Path):
        control = directory / "control"
        control.mkdir(mode=0o700)
        live = directory / "xray.json"
        old = b'{"remarks":"before","inbounds":[]}\n'
        new = b'{"remarks":"after","inbounds":[]}\n'
        live.write_bytes(old)
        return control, live, old, new

    def patch_paths(self, control, live):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(pilot, "CONTROL", control))
        stack.enter_context(patch.object(pilot, "CONFIG", live))
        stack.enter_context(patch.object(pilot, "BACKUP", control / "xray-before.json"))
        stack.enter_context(patch.object(pilot, "JOURNAL", control / "transaction.json"))
        stack.enter_context(patch.object(pilot, "transaction_lock", return_value=contextlib.nullcontext()))
        stack.enter_context(patch.object(pilot, "secure_transaction_dir"))
        stack.enter_context(patch.object(pilot, "service_active", return_value=True))
        stack.enter_context(patch.object(pilot, "fleet_identity_digest", return_value="fleet"))
        return stack

    def test_apply_creates_private_backup_and_pending_journal(self):
        with tempfile.TemporaryDirectory() as td:
            control, live, original, new = self.workspace(Path(td))
            stage = {"router_id": "new", "current_sha": pilot.sha(original),
                     "candidate_sha": pilot.sha(new), "fleet_sha": "fleet",
                     "original": original, "candidate": new}
            with self.patch_paths(control, live), \
                 patch.object(pilot, "preview", return_value=stage), \
                 patch.object(pilot, "restart_xray") as restart:
                doc = pilot.apply("new", "a" * 40, pilot.sha(new))
                self.assertEqual(doc["state"], "APPLIED_PENDING_ROUTER")
                self.assertEqual(live.read_bytes(), new)
                self.assertEqual((control / "xray-before.json").read_bytes(), original)
                self.assertEqual(json.loads((control / "transaction.json").read_text())["state"], "APPLIED_PENDING_ROUTER")
                restart.assert_called_once()
                self.assertEqual((control / "xray-before.json").stat().st_mode & 0o777,
                                 0o600)

    def test_reject_wrong_approved_candidate_hash_without_writes(self):
        with tempfile.TemporaryDirectory() as td:
            control, live, original, new = self.workspace(Path(td))
            stage = {"router_id": "new", "current_sha": pilot.sha(original),
                     "candidate_sha": pilot.sha(new), "fleet_sha": "fleet",
                     "original": original, "candidate": new}
            with self.patch_paths(control, live), \
                 patch.object(pilot, "preview", return_value=stage), \
                 patch.object(pilot, "restart_xray") as restart:
                with self.assertRaisesRegex(pilot.Blocked, "differs"):
                    pilot.apply("new", "a" * 40, "f" * 64)
                self.assertEqual(live.read_bytes(), original)
                self.assertFalse((control / "transaction.json").exists())
                restart.assert_not_called()

    def test_restart_failure_recovers_original_and_marks_rolled_back(self):
        with tempfile.TemporaryDirectory() as td:
            control, live, original, new = self.workspace(Path(td))
            stage = {"router_id": "new", "current_sha": pilot.sha(original),
                     "candidate_sha": pilot.sha(new), "fleet_sha": "fleet",
                     "original": original, "candidate": new}
            with self.patch_paths(control, live), \
                 patch.object(pilot, "preview", return_value=stage), \
                 patch.object(pilot, "restart_xray",
                              side_effect=[pilot.Blocked("test restart failed"), None]) as restart:
                with self.assertRaises(pilot.Blocked):
                    pilot.apply("new", "a" * 40, pilot.sha(new))
                self.assertEqual(live.read_bytes(), original)
                self.assertEqual(json.loads((control / "transaction.json").read_text())["state"], "ROLLED_BACK")
                self.assertEqual(restart.call_count, 2)

    def test_manual_rollback_after_interrupted_commit(self):
        with tempfile.TemporaryDirectory() as td:
            control, live, original, new = self.workspace(Path(td))
            (control / "xray-before.json").write_bytes(original)
            (control / "transaction.json").write_text(
                '{"schema":1,"state":"APPLYING","router_id":"new",'
                '"original_sha":"' + pilot.sha(original) + '",'
                '"candidate_sha":"' + pilot.sha(new) + '"}\n'
            )
            live.write_bytes(new)
            with self.patch_paths(control, live), \
                 patch.object(pilot, "require_root"), \
                 patch.object(pilot, "read_journal", side_effect=lambda: json.loads((control / "transaction.json").read_text())), \
                 patch.object(pilot, "restart_xray") as restart:
                doc = pilot.rollback(confirm=True)
                self.assertEqual(doc["state"], "ROLLED_BACK")
                self.assertEqual(live.read_bytes(), original)
                restart.assert_called_once()
                self.assertEqual(json.loads((control / "transaction.json").read_text())["state"], "ROLLED_BACK")

    def test_refuses_external_config_drift_during_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            control, live, original, new = self.workspace(Path(td))
            (control / "xray-before.json").write_bytes(original)
            (control / "transaction.json").write_text(
                '{"schema":1,"state":"APPLYING","router_id":"new",'
                '"original_sha":"' + pilot.sha(original) + '",'
                '"candidate_sha":"' + pilot.sha(new) + '"}\n'
            )
            external = b'{"unrelated":true}'
            live.write_bytes(external)
            with self.patch_paths(control, live), \
                 patch.object(pilot, "require_root"), \
                 patch.object(pilot, "read_journal", side_effect=lambda: json.loads((control / "transaction.json").read_text())), \
                 patch.object(pilot, "restart_xray") as restart:
                with self.assertRaisesRegex(pilot.Blocked, "outside transaction"):
                    pilot.rollback(confirm=True)
                self.assertEqual(live.read_bytes(), external)
                restart.assert_not_called()

    def test_preconditions_require_maintenance_hold_and_active_services(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(pilot, "require_root"), \
                 patch.object(pilot, "checkout"), \
                 patch.object(pilot, "CONFIG", root / "not-installed"), \
                 patch.object(pilot, "JOURNAL", root / "journal"):
                with self.assertRaisesRegex(pilot.Blocked, "required file"):
                    pilot.preconditions("a" * 40, "new")
            with patch.object(pilot, "require_root"), \
                 self.assertRaisesRegex(pilot.Blocked, "invalid router"):
                pilot.preconditions("a" * 40, "../unsafe")

    def test_status_on_six_router_fixture_does_not_write_or_restart(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, db, hold, unit = (root / name for name in
                                      ("xray.json", "hub.db", ".hold", "adapter.service"))
            config.write_bytes(b'{"inbounds":[]}\n')
            hold.write_text("keep hold in place")
            unit.write_text(
                "ExecStart=python3 host_adapter.py --peer-uid 23088\n"
                "ProtectSystem=strict\n"
                "ReadWritePaths=/run/router-orchestrator\n")
            with sqlite3.connect(db) as conn:
                conn.execute(
                    "CREATE TABLE routers (id TEXT PRIMARY KEY, "
                    "vless_uuid TEXT, ssh_vless_uuid TEXT, "
                    "entry_port INTEGER, ssh_entry_port INTEGER, "
                    "vps_transport TEXT, vless_port INTEGER, "
                    "vps_allow_legacy INTEGER, deleted_at TEXT)"
                )
                for idx in range(5):
                    conn.execute(
                        "INSERT INTO routers VALUES (?,?,?,?,?,?,?,?,?)",
                        (f"router{idx}", f"admin{idx}", f"ssh{idx}",
                         18090 + idx, 19090 + idx, "ws", 443, 0, "")
                    )
                conn.execute(
                    "INSERT INTO routers VALUES (?,?,?,?,?,?,?,?,?)",
                    ("legacy", "tcp-admin", "tcp-ssh", 18099, 19099,
                     "tcp", 18443, 0, None)
                )
                conn.execute(
                    "INSERT INTO routers VALUES (?,?,?,?,?,?,?,?,?)",
                    ("deleted", "former", "former-ssh", 18199, 19199,
                     "ws", 443, 0, "2026-01-01")
                )
            with patch.object(pilot.gate, "xray_parity") as parity, \
                 patch.object(pilot, "require_root"), \
                 patch.object(pilot, "checkout"), \
                 patch.object(pilot, "ensure_services"), \
                 patch.object(pilot, "CONFIG", config), \
                 patch.object(pilot, "DB", db), \
                 patch.object(pilot, "HOLD", hold), \
                 patch.object(pilot, "UNIT", unit), \
                 patch.object(pilot, "JOURNAL", root / "absent"), \
                 patch.object(pilot, "atomic_write") as write, \
                 patch.object(pilot, "restart_xray") as restart, \
                 patch.object(pilot, "save_journal") as journal:
                report = pilot.status("a" * 40)
                self.assertEqual((report["wss"], report["tcp"]), (5, 1))
                self.assertEqual(report["active_count"], 6)
                self.assertEqual(report["transaction"], "NONE")
                parity.assert_called_once()
                self.assertTrue(hold.exists())
                write.assert_not_called()
                restart.assert_not_called()
                journal.assert_not_called()

    def test_status_refuses_adapter_write_flags(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, db, hold, unit = (root / name for name in
                                      ("xray.json", "hub.db", ".hold", "adapter.service"))
            config.write_bytes(b'{}')
            db.touch()  # Status must reach the adapter-policy guard first.
            hold.write_text("keep")
            unit.write_text(
                "ExecStart=python3 host_adapter.py --allow-xray-apply\n"
                "ProtectSystem=strict\n"
                "ReadWritePaths=/run/router-orchestrator\n")
            with patch.object(pilot, "require_root"), \
                 patch.object(pilot, "checkout"), \
                 patch.object(pilot, "ensure_services"), \
                 patch.object(pilot, "CONFIG", config), \
                 patch.object(pilot, "DB", db), \
                 patch.object(pilot, "HOLD", hold), \
                 patch.object(pilot, "UNIT", unit):
                with self.assertRaisesRegex(pilot.Blocked, "read-only"):
                    pilot.status("a" * 40)

    def test_status_blocks_empty_or_mismatched_active_inventory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, db, hold, unit = (root / name for name in
                                      ("xray.json", "hub.db", ".hold", "adapter.service"))
            config.write_text('{"inbounds":[]}')
            hold.write_text("keep")
            unit.write_text(
                "ExecStart=python3 host_adapter.py --peer-uid 23088\n"
                "ProtectSystem=strict\n"
                "ReadWritePaths=/run/router-orchestrator\n")
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE routers (id TEXT PRIMARY KEY, "
                             "vless_uuid TEXT, ssh_vless_uuid TEXT, "
                             "entry_port INTEGER, ssh_entry_port INTEGER, "
                             "vps_transport TEXT, vless_port INTEGER, deleted_at TEXT)")
                # Empty-string means active; unknown transport must not count.
                conn.execute(
                    "INSERT INTO routers VALUES (?,?,?,?,?,?,?,?)",
                    ("unexpected", "v", "s", 18099, 19099, "grpc", 443, "")
                )
            with patch.object(pilot, "require_root"), \
                 patch.object(pilot, "checkout"), \
                 patch.object(pilot, "ensure_services"), \
                 patch.object(pilot, "CONFIG", config), \
                 patch.object(pilot, "DB", db), \
                 patch.object(pilot, "HOLD", hold), \
                 patch.object(pilot, "UNIT", unit), \
                 patch.object(pilot, "JOURNAL", root / "absent"), \
                 patch.object(pilot.gate, "xray_parity") as parity:
                with self.assertRaisesRegex(
                        pilot.Blocked, "SQLite/Xray router parity failed"):
                    pilot.status("a" * 40)
                parity.assert_not_called()

    def test_status_blocks_xray_client_drift_even_when_counts_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, db, hold, unit = (root / name for name in
                                      ("xray.json", "hub.db", ".hold", "adapter.service"))
            config.write_text('{"inbounds":[],"routing":{"rules":[]}}')
            hold.write_text("keep")
            unit.write_text(
                "ExecStart=python3 host_adapter.py --peer-uid 23088\n"
                "ProtectSystem=strict\n"
                "ReadWritePaths=/run/router-orchestrator\n")
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE routers (id TEXT PRIMARY KEY, "
                             "vless_uuid TEXT, ssh_vless_uuid TEXT, "
                             "entry_port INTEGER, ssh_entry_port INTEGER, "
                             "vps_transport TEXT, vless_port INTEGER, deleted_at TEXT)")
                conn.execute(
                    "INSERT INTO routers VALUES (?,?,?,?,?,?,?,?)",
                    ("existing", "admin-id", "ssh-id", 18099, 19099, "ws", 443, "")
                )
            with patch.object(pilot, "require_root"), \
                 patch.object(pilot, "checkout"), \
                 patch.object(pilot, "ensure_services"), \
                 patch.object(pilot, "CONFIG", config), \
                 patch.object(pilot, "DB", db), \
                 patch.object(pilot, "HOLD", hold), \
                 patch.object(pilot, "UNIT", unit), \
                 patch.object(pilot, "JOURNAL", root / "absent"):
                with self.assertRaisesRegex(
                        pilot.Blocked, "SQLite/Xray router parity failed"):
                    pilot.status("a" * 40)

    def test_status_is_readonly_and_has_no_seventh_router_requirement(self):
        text = (REPO / "tools/orchestrator_wss_commit_pilot.py").read_text()
        fragment = text.split("def status(", 1)[1].split("\ndef preview(", 1)[0]
        self.assertIn("gate.db_inventory(DB)", fragment)
        self.assertIn("gate.xray_parity(inventory", fragment)
        self.assertNotIn("build_candidate(", fragment)
        self.assertNotIn("atomic_write(", fragment)
        self.assertNotIn("restart_xray(", fragment)
        self.assertNotIn("save_journal(", fragment)

    def test_rollback_refuses_unconfirmed(self):
        with self.assertRaisesRegex(pilot.Blocked, "requires --confirm"):
            pilot.rollback(False)

    def test_source_requires_approved_window_and_skips_main_hub_restart(self):
        content = (REPO / "tools/orchestrator_wss_commit_pilot.py").read_text()
        self.assertIn('require(args.approve_maintenance', content)
        self.assertIn("APPLIED_PENDING_ROUTER", content)
        self.assertIn('\"restart\", XRAY_SERVICE', content)
        self.assertNotIn("systemctl restart owrt-remote.service", content)
        self.assertNotIn("xray.apply", content)
        self.assertNotIn("os.unlink(HOLD)", content)


if __name__ == "__main__":
    unittest.main()
