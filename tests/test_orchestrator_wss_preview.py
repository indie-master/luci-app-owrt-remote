"""Offline sanity checks for candidate WSS enrollment on a private snapshot."""
from pathlib import Path
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

    def test_repo_tool_has_no_live_xray_mutations(self):
        source = (Path(__file__).resolve().parents[1] /
                  "tools/orchestrator_docker_wss_enrollment_preview.py").read_text()
        self.assertIn("adapter.validate_transition(xray, generated)", source)
        self.assertNotIn("adapter.xray_apply(", source)
        self.assertNotIn("systemctl restart", source)
        self.assertNotIn("docker compose up", source)


if __name__ == "__main__":
    unittest.main()
