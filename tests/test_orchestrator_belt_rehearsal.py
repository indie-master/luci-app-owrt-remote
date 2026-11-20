"""Offline tests for the source-exact Belt shadow orchestrator."""
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import orchestrator_belt_rehearsal as ops


class RehearsalOps(unittest.TestCase):
    def test_safe_private_cleanup_never_follows_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            other = root / "must-stay"
            other.mkdir()
            probe = root / "orchestrator-shadow-state-1"
            probe.symlink_to(other, target_is_directory=True)
            with self.assertRaises(ops.RehearsalError):
                ops.private_cleanup(probe, root, "orchestrator-shadow-state-")
            self.assertTrue(other.exists())
            good = root / "orchestrator-shadow-state-2"
            good.mkdir()
            (good / "file").write_text("sample")
            ops.private_cleanup(good, root, "orchestrator-shadow-state-")
            self.assertFalse(good.exists())
            self.assertTrue(other.exists())

    def test_rehearsal_does_not_touch_prod_and_cleans_disposable_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "xray.json"
            source = root / "hub.py"
            static = root / "static"
            dockerfile = root / "Dockerfile"
            static.mkdir()
            config.write_text('{"inbounds":[]}')
            source.write_text("sample app")
            dockerfile.write_text("FROM scratch")
            snapshot = root / "orchestrator-docker-rehearsal-1"
            snapshot.mkdir()
            staged = root / "orchestrator-shadow-state-1"
            context = root / "orchestrator-shadow-image-1"
            digest = ops.file_digest(source)
            def restored(*args):
                staged.mkdir()
                return staged, {"source_sha256": digest}
            def ctx(*args):
                context.mkdir()
                (context / "Dockerfile").write_text("FROM scratch")
                return context, digest

            with patch.object(ops.os, "geteuid", return_value=0), \
                 patch.object(ops, "private_root"), \
                 patch.object(ops, "service_active", return_value=True), \
                 patch.object(ops.gate, "analyze"), \
                 patch.object(ops.gate, "snapshot", return_value=(snapshot, {})), \
                 patch.object(ops.gate, "inspect_archive"), \
                 patch.object(ops.shadow, "restore", side_effect=restored), \
                 patch.object(ops.image_context, "build_context", side_effect=ctx), \
                 patch.object(ops.shadow_runner, "run_rehearsal",
                              return_value={"health": "PASS", "routers": 6, "wss": 5,
                                            "tcp": 1, "logs": 5}), \
                 patch.object(ops.subprocess, "run") as cmd, \
                 patch.object(ops.shutil, "disk_usage",
                              return_value=type("Disk", (), {"free": 1024**3})()):
                cmd.return_value.returncode = 0
                result_snapshot, result = ops.rehearsal(
                    output_root=root, dockerfile=dockerfile, state=root,
                    xray=config, source=source, static=static)
            self.assertEqual(result_snapshot, snapshot)
            self.assertEqual(result["routers"], 6)
            self.assertTrue(snapshot.exists())
            self.assertFalse(staged.exists())
            self.assertFalse(context.exists())
            self.assertEqual(config.read_text(), '{"inbounds":[]}')
            self.assertEqual(source.read_text(), "sample app")
            self.assertEqual(cmd.call_count, 2)
            self.assertEqual(cmd.call_args.args[0][:3], (["docker", "image", "rm"]))

    def test_service_failure_blocks_before_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(ops.os, "geteuid", return_value=0), \
                 patch.object(ops, "service_active", return_value=False), \
                 patch.object(ops.gate, "snapshot") as snapshot:
                with self.assertRaises(ops.RehearsalError):
                    ops.rehearsal(output_root=root, dockerfile=root / "x")
                snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
