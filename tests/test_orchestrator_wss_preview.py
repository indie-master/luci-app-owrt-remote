"""Offline sanity checks for candidate WSS enrollment on a private snapshot."""
from pathlib import Path
import contextlib
import io
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import orchestrator_docker_wss_enrollment_preview as preview


class WssSnapshotPreviewTests(unittest.TestCase):
    def test_select_pair_never_uses_existing_ports(self):
        taken = {18443, 18444, 24000, 24001, 25002}
        admin, ssh = preview.select_pair(taken)
        self.assertEqual((admin, ssh), (24003, 25003))
        self.assertNotIn(admin, taken)
        self.assertNotIn(ssh, taken)
        self.assertEqual(ssh-admin, 1000)

    def test_select_pair_fails_without_room(self):
        with self.assertRaises(preview.Blocked):
            preview.select_pair(set(range(24000, 29000)))

    def test_refuses_unprivileged_execution_before_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(preview.os, "geteuid", return_value=1000), \
                 patch.object(preview.restored, "restore") as restore:
                with self.assertRaisesRegex(preview.Blocked, "root"):
                    preview.preview(root / "missing", "a" * 40, root)
                restore.assert_not_called()

    def test_restoration_blocker_identifies_stage_not_private_message(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "snapshot"
            archive.mkdir()
            with patch.object(preview.os, "geteuid", return_value=0), \
                 patch.object(preview.candidate, "verify_private_parent"), \
                 patch.object(preview.candidate, "verify_checkout"), \
                 patch.object(preview.gate, "inspect_archive",
                              return_value={"kind": "REHEARSAL_ONLY_NOT_CUTOVER"}), \
                 patch.object(preview.restored, "restore",
                              side_effect=preview.restored.Blocked("secret-token-123")):
                with self.assertRaises(preview.Blocked) as failed:
                    preview.preview(archive, "a" * 40, root)
            self.assertEqual(str(failed.exception), "snapshot.restore: Blocked")
            self.assertNotIn("secret-token", str(failed.exception))

    def test_cli_surfaces_only_reviewed_blocker_message(self):
        with patch.object(sys, "argv", ["preview", "--snapshot", "/tmp/snapshot",
                                        "--expect-commit", "a" * 40]), \
             patch.object(preview, "preview",
                          side_effect=preview.Blocked(
                              "candidate.baseline.generator: config mismatch")):
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                code = preview.main()
        self.assertEqual(code, 2)
        self.assertIn("candidate.baseline.generator: config mismatch",
                      errors.getvalue())

    def test_repo_tool_has_no_live_xray_mutations(self):
        source = (Path(__file__).resolve().parents[1] /
                  "tools/orchestrator_docker_wss_enrollment_preview.py").read_text()
        self.assertIn("adapter.validate_transition(xray, generated)", source)
        self.assertNotIn("adapter.xray_apply(", source)
        self.assertNotIn("systemctl restart", source)
        self.assertNotIn("docker compose up", source)


if __name__ == "__main__":
    unittest.main()
