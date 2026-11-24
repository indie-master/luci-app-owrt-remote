"""Offline Xray shadow stage crash/rollback tests; never touches Docker."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "docker"))
import transport_stage as stage


class ShadowStageRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.original = b'{"inbounds":[{"tag":"old","port":18443}],"routing":{"rules":[]}}\n'
        self.candidate = b'{"inbounds":[{"tag":"old","port":18443},{"tag":"new","port":18446}],"routing":{"rules":[]}}\n'
        self.config = self.root / "xray.json"
        self.backup = self.root / "previous-xray.json"
        self.journal = self.root / "stage.json"
        self.hold = self.root / ".hold"
        self.record = {"phase": "STAGED", "baseline_sha256": stage.sha(self.original),
                       "candidate_sha256": stage.sha(self.candidate),
                       "expires_unix": 1, "pilot": {"router_id": "main"}}
        self.backup.write_bytes(self.original)
        self.config.write_bytes(self.candidate)
        self.hold.write_bytes(b"lock")
        for key, value in (("ORIGINAL", self.backup), ("CONFIG", self.config),
                           ("STATE", self.journal), ("HOLD", self.hold)):
            patcher = patch.object(stage, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_failed_candidate_rolls_back_without_needing_candidate_health(self):
        with patch.object(stage, "restart") as restart:
            stage.rollback_impl(self.record)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse(self.hold.exists())
        self.assertEqual(json.loads(self.journal.read_text())["phase"], "ROLLED_BACK")
        restart.assert_called_once()

    def test_rollback_idempotent_if_candidate_never_written(self):
        self.config.write_bytes(self.original)
        with patch.object(stage.lifecycle, "active", return_value=True), \
             patch.object(stage, "restart") as restart:
            stage.rollback_impl(self.record)
        self.assertEqual(self.config.read_bytes(), self.original)
        restart.assert_not_called()
        self.assertFalse(self.hold.exists())

    def test_external_config_write_blocks_overwrite(self):
        self.config.write_bytes(b'{"other":true}')
        with patch.object(stage, "restart") as restart:
            with self.assertRaisesRegex(RuntimeError, "changed externally"):
                stage.rollback_impl(self.record)
        self.assertEqual(self.config.read_bytes(), b'{"other":true}')
        restart.assert_not_called()
        self.assertTrue(self.hold.exists())

    def test_failed_recovery_leaves_hold_and_alarm(self):
        with patch.object(stage, "restart", side_effect=RuntimeError("Xray down")):
            with self.assertRaises(RuntimeError):
                stage.rollback_impl(self.record)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(json.loads(self.journal.read_text())["phase"], "ROLLBACK_FAILED")
        self.assertTrue(self.hold.exists())


if __name__ == "__main__":
    unittest.main()
