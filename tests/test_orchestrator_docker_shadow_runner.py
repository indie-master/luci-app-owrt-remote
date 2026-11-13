"""Docker rehearsal context and dry-run isolation tests; no Docker daemon."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

import orchestrator_belt_docker_migration_gate as gate
import orchestrator_docker_rehearsal_restore as restore
import orchestrator_docker_image_context as context
import orchestrator_docker_shadow_runner as runner
from test_orchestrator_belt_docker_migration_gate import build_fleet


class DockerShadowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state, self.xray, self.hub = build_fleet(self.root)
        original, _ = gate.snapshot(self.state, self.xray, self.hub, self.root)
        self.stage, self.report = restore.restore(original, self.root)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "favicon.ico").write_bytes(b"fake-ico")
        (self.static / "image.png").write_bytes(b"fake-png")

    def test_image_context_contains_only_hub_source_and_static_assets(self):
        folder, sha = context.build_context(
            self.stage, self.static,
            ROOT / "docker" / "hub" / "Dockerfile.rehearsal", self.root)
        self.assertEqual(sha, self.report["source_sha256"])
        self.assertEqual(folder.stat().st_mode & 0o077, 0)
        self.assertEqual((folder / "hub-source.py").read_bytes(), self.hub.read_bytes())
        self.assertEqual((folder / "static" / "image.png").read_bytes(), b"fake-png")
        self.assertFalse((folder / "state").exists())
        self.assertFalse((folder / "hub-auth.json").exists())
        self.assertFalse((folder / "agent.token").exists())
        self.assertFalse((folder / "xray.json").exists())
        self.assertEqual(
            json.loads((folder / "context-report.json").read_text())
            ["hub_source_sha256"], sha)

    def test_unsafe_static_asset_blocks_without_new_context(self):
        (self.static / "secret.pem").write_text("key material")
        with self.assertRaises(restore.Blocked):
            context.build_context(
                self.stage, self.static,
                ROOT / "docker" / "hub" / "Dockerfile.rehearsal", self.root)

    def test_symlink_static_asset_blocks(self):
        (self.static / "external.png").symlink_to(self.xray)
        with self.assertRaises(restore.Blocked):
            context.build_context(
                self.stage, self.static,
                ROOT / "docker" / "hub" / "Dockerfile.rehearsal", self.root)

    def test_shadow_docker_flags_never_publish_or_mount_live_files(self):
        calls = []
        cid = "a" * 64
        expected_source = self.report["source_sha256"]

        def fake_docker(*args, timeout=35):
            calls.append(args)
            if args[:2] == ("image", "inspect"):
                label_query = args[3]
                if runner.PURPOSE_LABEL in label_query:
                    return subprocess.CompletedProcess(args, 0, "isolated-hub-rehearsal\n", "")
                return subprocess.CompletedProcess(args, 0, expected_source + "\n", "")
            if args[0] == "run":
                self.assertIn("--network", args)
                self.assertIn("none", args)
                self.assertIn("--log-driver", args)
                self.assertIn("--read-only", args)
                self.assertIn("--user", args)
                import os
                self.assertIn(str(os.geteuid()) + ":" + str(os.getegid()), args)
                self.assertIn("--cap-drop", args)
                self.assertIn("ALL", args)
                self.assertNotIn("--publish", args)
                self.assertNotIn("-p", args)
                self.assertNotIn("--privileged", args)
                self.assertNotIn("/var/run/docker.sock", " ".join(args))
                self.assertNotIn("src=/var/lib/owrt-remote,", " ".join(args))
                self.assertIn("src=" + str(self.stage / "state"),
                              " ".join(args))
                return subprocess.CompletedProcess(args, 0, cid + "\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(runner, "docker", side_effect=fake_docker):
            result = runner.run_rehearsal(self.stage, "router-rehearsal:test")
        self.assertEqual(result["health"], "PASS")
        self.assertEqual(result["routers"], 6)
        self.assertEqual(result["wss"], 5)
        self.assertTrue(any(call[0] == "exec" for call in calls))
        self.assertTrue(any(call[:2] == ("rm", "--force") for call in calls))
        self.assertEqual(restore.verify_staged(self.stage), self.report)

    def test_wrong_source_digest_prevents_any_container_run(self):
        def fake_docker(*args, timeout=35):
            return subprocess.CompletedProcess(args, 0, "wrong-label\n", "")
        with mock.patch.object(runner, "docker", side_effect=fake_docker) as call:
            with self.assertRaises(runner.PilotError):
                runner.run_rehearsal(self.stage, "not-approved:test")
        self.assertFalse(any(x.args[0][0] == "run" for x in call.call_args_list))


if __name__ == "__main__":
    unittest.main()
