"""Offline failure-injection tests for independent Belt Xray rescue.

All tests use temporary JSON, no systemd actions and no actual host changes.
"""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_wss_emergency_rescue as rescue


class RescueTests(unittest.TestCase):
    def fixture(self, root: Path, *, phase="APPLIED_PENDING_ROUTER"):
        state = root / "pilot"
        state.mkdir()
        config = root / "xray.json"
        hold = root / ".hold"
        backup = state / "xray-before.json"
        journal = state / "transaction.json"
        old = b'{"inbounds":[{"tag":"existing"}]}\n'
        new = b'{"inbounds":[{"tag":"existing"},{"tag":"new"}]}\n'
        config.write_bytes(new)
        backup.write_bytes(old)
        hold.write_text("keep")
        doc = {
            "schema": 1, "state": phase, "router_id": "pilot_wss",
            "original_sha": rescue.digest(old),
            "candidate_sha": rescue.digest(new),
        }
        journal.write_text(json.dumps(doc))
        return state, config, hold, backup, journal, old, new, doc

    def isolate(self, fixture):
        state, config, hold, backup, journal, *_ = fixture
        manager = contextlib.ExitStack()
        for label, item in (
            ("ROOT", state), ("CONFIG", config), ("HOLD", hold),
            ("BACKUP", backup), ("JOURNAL", journal)
        ):
            manager.enter_context(patch.object(rescue, label, item))
        manager.enter_context(patch.object(rescue.os, "geteuid", return_value=0))
        manager.enter_context(patch.object(rescue, "exclusive",
                                           return_value=contextlib.nullcontext()))
        manager.enter_context(patch.object(rescue, "secure_parent"))
        # System-auth checks are tested separately on malformed symlinks.
        manager.enter_context(patch.object(
            rescue, "secure_file", side_effect=lambda p, **kw: p.stat()))
        manager.enter_context(patch.object(rescue.os, "fchown"))
        return manager

    def test_restores_original_exact_bytes_and_keeps_journal(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = self.fixture(Path(td))
            _, config, hold, backup, journal, old, new, doc = fixture
            with self.isolate(fixture), \
                 patch.object(rescue, "restart_xray") as restart:
                outcome = rescue.restore(confirm=True,
                                         expected_candidate=doc["candidate_sha"])
                self.assertEqual(outcome["state"], "ROLLED_BACK")
                self.assertEqual(config.read_bytes(), old)
                self.assertEqual(backup.read_bytes(), old)
                self.assertTrue(hold.exists())
                self.assertEqual(json.loads(journal.read_text())["state"],
                                 "ROLLED_BACK")
                restart.assert_called_once()

    def test_abort_on_unexpected_external_edit(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = self.fixture(Path(td))
            _, config, _, _, journal, _, _, doc = fixture
            foreign = b'{"inbounds":["unrelated"]}\n'
            config.write_bytes(foreign)
            with self.isolate(fixture), \
                 patch.object(rescue, "restart_xray") as restart:
                with self.assertRaisesRegex(rescue.Blocked, "unexpected Xray"):
                    rescue.restore(confirm=True,
                                   expected_candidate=doc["candidate_sha"])
                self.assertEqual(config.read_bytes(), foreign)
                self.assertEqual(json.loads(journal.read_text())["state"],
                                 "APPLIED_PENDING_ROUTER")
                restart.assert_not_called()

    def test_abort_on_wrong_operator_sha(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = self.fixture(Path(td))
            _, config, _, _, journal, _, new, _ = fixture
            with self.isolate(fixture), \
                 patch.object(rescue, "restart_xray") as restart:
                with self.assertRaisesRegex(rescue.Blocked, "operator candidate"):
                    rescue.restore(confirm=True,
                                   expected_candidate="0" * 64)
                self.assertEqual(config.read_bytes(), new)
                self.assertEqual(json.loads(journal.read_text())["state"],
                                 "APPLIED_PENDING_ROUTER")
                restart.assert_not_called()

    def test_resume_interrupted_rollback_when_old_config_already_restored(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = self.fixture(Path(td), phase="RECOVERING")
            _, config, _, _, journal, old, _, doc = fixture
            config.write_bytes(old)
            with self.isolate(fixture), \
                 patch.object(rescue, "restart_xray") as restart:
                result = rescue.restore(confirm=True,
                                        expected_candidate=doc["candidate_sha"])
                self.assertTrue(result["restored"])
                self.assertEqual(config.read_bytes(), old)
                self.assertEqual(json.loads(journal.read_text())["state"],
                                 "ROLLED_BACK")
                # On-disk original could be out of sync with the running core.
                restart.assert_called_once()

    def test_repeated_rollback_does_not_restart_healthy_old_xray(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = self.fixture(Path(td), phase="ROLLED_BACK")
            _, config, _, _, journal, old, _, doc = fixture
            config.write_bytes(old)
            with self.isolate(fixture), \
                 patch.object(rescue, "listener_ready", return_value=True), \
                 patch.object(rescue, "restart_xray") as restart:
                result = rescue.restore(confirm=True,
                                        expected_candidate=doc["candidate_sha"])
                self.assertFalse(result["restored"])
                restart.assert_not_called()
                self.assertEqual(json.loads(journal.read_text())["state"],
                                 "ROLLED_BACK")

    def test_backup_mismatch_refuses_to_restore(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = self.fixture(Path(td))
            _, config, _, backup, journal, _, new, doc = fixture
            backup.write_text("corrupt backup")
            with self.isolate(fixture), \
                 patch.object(rescue, "restart_xray") as restart:
                with self.assertRaisesRegex(rescue.Blocked, "backup SHA"):
                    rescue.restore(confirm=True,
                                   expected_candidate=doc["candidate_sha"])
                self.assertEqual(config.read_bytes(), new)
                self.assertEqual(json.loads(journal.read_text())["state"],
                                 "APPLIED_PENDING_ROUTER")
                restart.assert_not_called()

    def test_missing_confirmation_rejects_before_lock(self):
        with patch.object(rescue, "exclusive") as lock:
            with self.assertRaisesRegex(rescue.Blocked, "--confirm"):
                rescue.restore(confirm=False,
                               expected_candidate="a" * 64)
            lock.assert_not_called()

    def test_status_without_journal_is_readonly(self):
        with tempfile.TemporaryDirectory() as td:
            fixture = self.fixture(Path(td))
            _, config, _, _, journal, _, new, _ = fixture
            journal.unlink()
            with self.isolate(fixture), \
                 patch.object(rescue, "atomic_bytes") as write, \
                 patch.object(rescue, "restart_xray") as restart:
                result = rescue.status()
                self.assertEqual(result["transaction"], "NONE")
                self.assertEqual(result["config_sha"], rescue.digest(new))
                write.assert_not_called()
                restart.assert_not_called()
                self.assertFalse(journal.exists())

    def test_symlink_never_passes_root_private_file_check(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "protected"
            target.write_text("unchanged")
            link = root / "backup"
            link.symlink_to(target)
            with self.assertRaisesRegex(rescue.Blocked, "root-owned regular"):
                rescue.secure_file(link, private=True)
            self.assertEqual(target.read_text(), "unchanged")

    def test_rescue_has_no_git_hub_docker_or_mutation_endpoint(self):
        source = (ROOT / "tools/orchestrator_wss_emergency_rescue.py").read_text()
        self.assertNotIn("import orchestrator_", source)
        self.assertNotIn("git", source.lower().split('"""', 2)[-1])
        self.assertNotIn('"restart", "owrt-remote.service"', source)
        self.assertNotIn('"restart", "nginx.service"', source)
        self.assertNotIn("xray.apply", source)
        self.assertNotIn("docker compose", source)


if __name__ == "__main__":
    unittest.main()
