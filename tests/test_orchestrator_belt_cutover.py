"""Offline tests of the Belt single-writer cutover. No host operations."""
from contextlib import nullcontext
from pathlib import Path
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import orchestrator_belt_cutover as cutover


def fixture_state(folder: Path) -> None:
    folder.mkdir()
    conn = sqlite3.connect(folder / "hub.db")
    conn.execute("""CREATE TABLE routers (
        id TEXT, vless_uuid TEXT, ssh_vless_uuid TEXT,
        entry_port INT, ssh_entry_port INT, vps_transport TEXT,
        vless_port INT, vps_allow_legacy INT, deleted_at TEXT
    )""")
    conn.execute("""CREATE TABLE router_logs (id INTEGER, message TEXT)""")
    conn.execute("""INSERT INTO routers VALUES
        ('main','u1','u2',18101,19101,'ws',443,0,NULL)""")
    conn.execute("""INSERT INTO router_logs VALUES (1,'history-one')""")
    conn.commit()
    conn.close()
    (folder / "agent.token").write_text("fake-token")
    (folder / "hub-auth.json").write_text('{"test":true}')
    (folder / ".xray-maintenance-hold").write_text("hold")
    (folder / "nested").mkdir()
    (folder / "nested" / "notifications.json").write_text("old history")
    (folder / "empty-directory").mkdir()


class SingleWriterCutoverTests(unittest.TestCase):
    def test_atomic_state_copy_preserves_all_history_and_files(self):
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            source = parent / "old"
            dest = parent / "new"
            fixture_state(source)
            real_inventory = cutover.gate.db_inventory
            def analyze(location, xray):
                return real_inventory(location / "hub.db"), {}
            with patch.object(cutover.gate, "analyze", side_effect=analyze), \
                 patch.object(cutover.os, "chown"):
                result = cutover.stage_state(source, dest, os.getuid())
            self.assertEqual(result["fleet"], 1)
            self.assertEqual(result["tables"], 2)
            self.assertEqual((dest / "agent.token").read_text(), "fake-token")
            self.assertEqual((dest / "hub-auth.json").read_text(), '{"test":true}')
            self.assertEqual((dest / ".xray-maintenance-hold").read_text(), "hold")
            self.assertEqual((dest / "nested/notifications.json").read_text(), "old history")
            self.assertTrue((dest / "empty-directory").is_dir())
            self.assertEqual(cutover.gate.db_inventory(dest / "hub.db")["tables"],
                             {"routers": 1, "router_logs": 1})
            self.assertEqual(cutover.gate.db_inventory(source / "hub.db")["tables"],
                             {"routers": 1, "router_logs": 1})

    def test_copy_refuses_symlink_and_does_not_leave_state_target(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            fixture_state(source)
            outside = root / "outside"
            outside.write_text("protected")
            (source / "nested" / "pointer").symlink_to(outside)
            dest = root / "copied"
            with self.assertRaises(cutover.gate.GateError):
                cutover.stage_state(source, dest, os.getuid())
            self.assertEqual(outside.read_text(), "protected")
            self.assertFalse(dest.exists())

    def test_source_state_is_not_overwritten_by_new_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source"
            dest = root / "destination"
            fixture_state(source)
            dest.mkdir()
            (dest / "private").write_text("do-not-overwrite")
            with self.assertRaises(cutover.Blocked):
                cutover.copy_complete_state(source, dest, os.getuid())
            self.assertEqual((dest / "private").read_text(), "do-not-overwrite")

    def test_no_cutover_without_both_acknowledgements(self):
        args = type("Args", (), {"acknowledge_live_cutover": False,
                                 "acknowledge_candidate_pass": True})()
        with self.assertRaises(cutover.Blocked):
            cutover.activate(args)

    def test_rollback_stops_docker_before_promoting_latest_state(self):
        doc = {"schema": 1, "phase": "DOCKER_ACTIVE", "id": "test"}
        args = type("Args", (), {"confirm": True})()
        operations = []
        def recover(document):
            operations.append("stop Docker, promote latest, restore systemd")
            self.assertIs(document, doc)
        with patch.object(cutover, "exclusive_lock", return_value=nullcontext()), \
             patch.object(cutover, "load_journal", return_value=doc), \
             patch.object(cutover, "service_active", return_value=False), \
             patch.object(cutover, "docker_running", return_value=True), \
             patch.object(cutover, "mark") as journal, \
             patch.object(cutover, "recover_host", side_effect=recover):
            cutover.rollback(args)
        self.assertEqual(operations, ["stop Docker, promote latest, restore systemd"])
        self.assertEqual(journal.call_args.args[1], "ROLLED_BACK")

    def test_no_new_service_started_before_stopped_old_service(self):
        src = (Path(__file__).resolve().parents[1] /
               "tools/orchestrator_belt_cutover.py").read_text()
        activate = src[src.index("def activate("):src.index("\ndef rollback(", src.index("def activate("))]
        stop = activate.index('"disable", "--now", HOST_SERVICE')
        copy = activate.index("stage_state(STATE, DATA, UID)")
        start = activate.index('"compose", "up", "-d", "--no-build", "hub"')
        self.assertLess(stop, copy)
        self.assertLess(copy, start)
        self.assertIn('mark(doc, "RECOVERING")', activate)
        self.assertIn("recover_host(doc)", activate)
        self.assertNotIn("systemctl restart", activate)

    def test_missing_container_does_not_block_auto_recovery(self):
        def query(*args, **kwargs):
            class Result:
                returncode = 0 if args[:2] == ("docker", "info") else 1
                stdout = ""
            return Result()
        with patch.object(cutover, "command", side_effect=query), \
             patch.object(cutover, "docker_running", return_value=False):
            cutover.stop_docker()

    def test_restored_data_fences_container_and_disables_env(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = root / ".env"
            backup = root / "backup"
            backup.mkdir()
            env.write_text("RO_IMAGE=example")
            doc = {"id": "synthetic"}
            with patch.object(cutover, "ENV", env), \
                 patch.object(cutover, "BACKUPS", backup):
                cutover.seal_env(doc)
                self.assertFalse(env.exists())
                saved = backup / "docker-env-synthetic.env"
                self.assertEqual(saved.read_text(), "RO_IMAGE=example")
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_rollback_promotes_latest_history_not_old_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / "var" / "owrt-remote"
            original.parent.mkdir()
            latest = root / "data"
            backup = root / "backups"
            backup.mkdir()
            fixture_state(original)
            fixture_state(latest)
            with sqlite3.connect(latest / "hub.db") as db:
                db.execute("INSERT INTO router_logs VALUES (2,'new-heartbeat')")
            def analyze(location, xray):
                return cutover.gate.db_inventory(location / "hub.db"), {}
            with patch.object(cutover, "STATE", original), \
                 patch.object(cutover, "DATA", latest), \
                 patch.object(cutover, "BACKUPS", backup), \
                 patch.object(cutover, "docker_running", return_value=False), \
                 patch.object(cutover.gate, "analyze", side_effect=analyze), \
                 patch.object(cutover.os, "chown"):
                cutover.promote_latest_state({"id": "synthetic"})
            self.assertEqual(cutover.gate.db_inventory(original / "hub.db")
                             ["tables"]["router_logs"], 2)
            self.assertEqual(cutover.gate.db_inventory(
                backup / "pre-docker-host-state-synthetic" / "hub.db")
                ["tables"]["router_logs"], 1)
            self.assertTrue(latest.exists())

    def test_recover_requires_explicit_acknowledgement(self):
        args = type("Args", (), {"confirm": False})()
        with self.assertRaises(cutover.Blocked):
            cutover.recover(args)

    def test_recover_rejects_dual_writers(self):
        args = type("Args", (), {"confirm": True})()
        doc = {"schema": 1, "id": "test", "phase": "HOST_STOPPED"}
        with patch.object(cutover, "exclusive_lock", return_value=nullcontext()), \
             patch.object(cutover, "load_journal", return_value=doc), \
             patch.object(cutover, "service_active", return_value=True), \
             patch.object(cutover, "docker_running", return_value=True), \
             patch.object(cutover, "recover_host") as restoration:
            with self.assertRaisesRegex(cutover.Blocked, "two active"):
                cutover.recover(args)
            restoration.assert_not_called()

    def test_recover_resume_at_stopped_host_phase(self):
        args = type("Args", (), {"confirm": True})()
        doc = {"schema": 1, "id": "test", "phase": "DOCKER_START_REQUESTED"}
        states = []
        def mark(document, phase):
            states.append(phase)
            document["phase"] = phase
        with patch.object(cutover, "exclusive_lock", return_value=nullcontext()), \
             patch.object(cutover, "load_journal", return_value=doc), \
             patch.object(cutover, "service_active", return_value=False), \
             patch.object(cutover, "docker_running", return_value=True), \
             patch.object(cutover, "mark", side_effect=mark), \
             patch.object(cutover, "recover_host") as recovery:
            cutover.recover(args)
            recovery.assert_called_once_with(doc)
        self.assertEqual(states, ["RECOVERING", "AUTO_ROLLED_BACK"])

    def test_recover_after_systemd_restart_before_journal_commit(self):
        args = type("Args", (), {"confirm": True})()
        doc = {"schema": 1, "id": "test", "phase": "HOST_RESTART_REQUESTED"}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(cutover, "exclusive_lock", return_value=nullcontext()), \
                 patch.object(cutover, "load_journal", return_value=doc), \
                 patch.object(cutover, "service_active", return_value=True), \
                 patch.object(cutover, "docker_running", return_value=False), \
                 patch.object(cutover, "DATA", root / "missing"), \
                 patch.object(cutover, "ENV", root / ".env"), \
                 patch.object(cutover, "mark") as marker, \
                 patch.object(cutover, "stop_docker") as fence, \
                 patch.object(cutover, "recover_host") as recovery:
                cutover.recover(args)
                recovery.assert_not_called()
                fence.assert_called_once_with()
                self.assertEqual(marker.call_args.args[1], "AUTO_ROLLED_BACK")

    def test_power_loss_between_two_state_renames_recovers_latest(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / "host" / "owrt-remote"
            original.parent.mkdir()
            data = root / "data"
            backups = root / "backups"
            backups.mkdir()
            fixture_state(original)
            fixture_state(data)
            with sqlite3.connect(data / "hub.db") as db:
                db.execute("INSERT INTO router_logs VALUES (2,'after-docker-writes')")
            archived = backups / "pre-docker-host-state-reboot"
            # Simulate SIGKILL right after os.replace(STATE, archived).
            os.replace(original, archived)
            self.assertFalse(original.exists())
            def analyze(location, xray):
                return cutover.gate.db_inventory(location / "hub.db"), {}
            with patch.object(cutover, "STATE", original), \
                 patch.object(cutover, "DATA", data), \
                 patch.object(cutover, "BACKUPS", backups), \
                 patch.object(cutover, "docker_running", return_value=False), \
                 patch.object(cutover.gate, "analyze", side_effect=analyze), \
                 patch.object(cutover.os, "chown"):
                cutover.promote_latest_state({"id": "reboot"})
                self.assertEqual(cutover.gate.db_inventory(original / "hub.db")
                                 ["tables"]["router_logs"], 2)
                # Repeated recovery doesn't rename or overwrite anything.
                cutover.promote_latest_state({"id": "reboot"})
            self.assertEqual(cutover.gate.db_inventory(archived / "hub.db")
                             ["tables"]["router_logs"], 1)

    def test_stop_docker_fences_restarts_without_env_file(self):
        events = []
        def call(*args, **kwargs):
            events.append(args)
            class Result:
                returncode = 0
                stdout = "router-orchestrator/hub\n"
            return Result()
        with patch.object(cutover, "command", side_effect=call), \
             patch.object(cutover, "docker_running", return_value=False):
            cutover.stop_docker()
        self.assertIn(("docker", "update", "--restart=no", cutover.DOCKER_NAME),
                      events)
        self.assertIn(("docker", "stop", "-t", "30", cutover.DOCKER_NAME),
                      events)
        self.assertLess(
            events.index(("docker", "update", "--restart=no", cutover.DOCKER_NAME)),
            events.index(("docker", "stop", "-t", "30", cutover.DOCKER_NAME)))

    def test_unavailable_docker_daemon_blocks_recovery(self):
        with patch.object(cutover, "command") as cmd:
            cmd.return_value.returncode = 1
            with self.assertRaisesRegex(cutover.Blocked, "Docker daemon unavailable"):
                cutover.stop_docker()

    def test_seal_env_recovery_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = root / ".env"
            backup = root / "backups"
            backup.mkdir()
            env.write_text("RO_PORT=8088\n")
            with patch.object(cutover, "ENV", env), patch.object(cutover, "BACKUPS", backup):
                cutover.seal_env({"id": "reboot"})
                cutover.seal_env({"id": "reboot"})
                self.assertFalse(env.exists())
                self.assertTrue((backup / "docker-env-reboot.env").exists())

    def test_preflight_does_not_call_writers(self):
        args = type("Args", (), {"expect_commit": "a" * 40,
                                 "public_url": "https://hub.example.org"})()
        report = {"fleet": 6, "wss": 5, "tcp": 1}
        with patch.object(cutover, "basic_preflight", return_value=report) as gate, \
             patch.object(cutover, "command_ok") as command, \
             patch.object(cutover, "mark") as change:
            cutover.preflight(args)
            gate.assert_called_once()
            command.assert_not_called()
            change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
