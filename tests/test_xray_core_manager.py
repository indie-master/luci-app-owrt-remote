"""Offline security/contract tests for Xray version control."""
import importlib.util
from contextlib import ExitStack, nullcontext, redirect_stdout
import fcntl
import hashlib
import io
import tempfile
from types import SimpleNamespace as Result
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker"))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "docker" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jobs = load("core_job_runner", "core_job_runner.py")
lifecycle = load("xray_lifecycle", "xray_lifecycle.py")
releases = load("xray_releases", "xray_releases.py")
host = load("core_test_host_adapter", "host_adapter.py")


class CoreManagerTests(unittest.TestCase):
    def test_untrusted_actions_rejected(self):
        for action in ("shell", "delete", "prune", "pull", "../xray"):
            with self.assertRaises(ValueError):
                jobs.command_for(action, "")

    def test_version_has_strict_semver(self):
        for text in ("latest", "v26.12.1;id", "../core", "v26.1", "v26.1.1-alpha"):
            with self.assertRaises(ValueError):
                jobs.command_for("upgrade", text)

    def test_upgrade_image_is_fixed_repository(self):
        command = jobs.command_for("upgrade", "v26.11.9")
        self.assertEqual(command[-2:], ["--image", "router-orchestrator-xray:26.11.9"])

    def test_restart_cannot_select_version(self):
        with self.assertRaises(ValueError):
            jobs.command_for("restart", "v26.11.9")

    def test_compose_env_replacement_changes_only_xray(self):
        original = (b"RO_HUB_IMAGE=router-orchestrator-hub:good\n"
                    b"RO_XRAY_IMAGE=router-orchestrator-xray:old\n"
                    b"RO_PUBLIC_URL=https://example.org\n")
        result = lifecycle.replace_image(
            original, "router-orchestrator-xray:26.11.9")
        self.assertIn(b"RO_HUB_IMAGE=router-orchestrator-hub:good", result)
        self.assertIn(b"RO_XRAY_IMAGE=router-orchestrator-xray:26.11.9", result)
        self.assertNotIn(b"RO_XRAY_IMAGE=router-orchestrator-xray:old", result)

    def test_reject_image_other_repositories(self):
        with self.assertRaises(RuntimeError):
            lifecycle.replace_image(
                b"RO_XRAY_IMAGE=router-orchestrator-xray:old\n",
                "evil.example/xray:latest")

    def test_release_pages_are_bounded_and_prereleases_skipped(self):
        class Response:
            status=200
            def __init__(self,items): self.items=items
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,size): return json.dumps(self.items).encode()
        first=[{"tag_name":f"v26.11.{i}","draft":False,"prerelease":True} for i in range(10)]
        second=[{"tag_name":"v26.7.28","draft":False,"prerelease":False}]
        with patch.object(releases.urllib.request,"urlopen",side_effect=[Response(first),Response(second)]) as fetch:
            self.assertEqual(releases.stable_release_tags(),["v26.7.28"])
            self.assertEqual(fetch.call_count,2)
            self.assertIn("per_page=10&page=2",fetch.call_args.args[0].full_url)

    def test_rate_limit_returns_specific_error(self):
        from urllib.error import HTTPError
        with patch.object(releases.urllib.request,"urlopen",side_effect=HTTPError(
                releases.RELEASES,429,"too many requests",None,None)):
            with self.assertRaisesRegex(RuntimeError,"rate limit.*429"):
                releases.stable_release_tags()

    def test_stable_releases_exclude_prereleases(self):
        entries = [
            {"tag_name": "v26.11.9", "prerelease": True, "draft": False},
            {"tag_name": "v26.7.28", "prerelease": False, "draft": False},
            {"tag_name": "v26.6.27", "prerelease": False, "draft": True},
            {"tag_name": "bad-name", "prerelease": False, "draft": False},
        ]
        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return json.dumps(entries).encode()
        with patch.object(releases.urllib.request, "urlopen",
                          return_value=FakeResponse()):
            self.assertEqual(releases.stable_release_tags(), ["v26.7.28"])

    def test_core_mutation_rejects_untrusted_payload_before_process_launch(self):
        adapter = host.Adapter(allow_apply=True, allow_restart=True)
        with patch.object(host.subprocess, "run") as invoke:
            for request, error in (
                ({"action": "restart", "confirmed": "true"}, "confirmation"),
                ({"action": "upgrade", "confirmed": True, "url": "https://example.org"},
                 "request field"),
                ({"action": "shell", "confirmed": True}, "unsupported core action"),
            ):
                with self.assertRaisesRegex(host.Rejected, error):
                    adapter.handle({"op": "xray.core.start", **request})
            invoke.assert_not_called()

    def test_candidate_config_rejection_uses_runtime_bind_mounts(self):
        digest = "sha256:" + "a" * 64
        mounts = {"/etc/xray": "/tmp/isolated-config",
                  "/usr/local/share/xray": "/tmp/isolated-assets",
                  "/etc/router-orchestrator/tls": "/tmp/isolated-tls"}
        with patch.object(lifecycle, "active_config", return_value=mounts), \
             patch.object(lifecycle, "docker", side_effect=[
                 Result(returncode=0, stdout="23088:23088"),
                 Result(returncode=1),
             ]) as invoke:
            with self.assertRaisesRegex(RuntimeError, "config test failed"):
                lifecycle.candidate_test(digest)
        args = invoke.call_args.args
        for target, source in mounts.items():
            self.assertIn(f"type=bind,src={source},dst={target},readonly", args)
        self.assertIn("23088:23088", args)
        self.assertIn(digest, args)

    def test_failed_candidate_recreation_restores_previous_image(self):
        digest = "sha256:" + "a" * 64
        pinned = releases.REGISTRY + "@sha256:" + "b" * 64
        old = "router-orchestrator-xray:old"
        new = "router-orchestrator-xray:26.11.9"
        before = f"RO_XRAY_IMAGE={old}\n".encode()
        with tempfile.TemporaryDirectory() as work, ExitStack() as stack:
            root = Path(work)
            env, compose, journal = root / ".env", root / "compose.yml", root / "journal"
            env.write_bytes(before)
            compose.touch()
            config = root / "owrt-remote.json"
            config.write_text("{}")
            manifests = root / "data/xray-candidates"
            manifests.mkdir(parents=True)
            (manifests / "26.11.9.json").write_text(json.dumps({
                "image": new, "image_id": digest, "official_digest_ref": pinned,
                "validation": "config-only",
            }))
            stack.enter_context(patch.multiple(
                lifecycle, ROOT=root, ENV=env, COMPOSE=compose, JOURNAL=journal,
                TRANSPORT_HOLD=root / "hold"))
            stack.enter_context(patch.object(lifecycle, "run", return_value=Result(returncode=1)))
            stack.enter_context(patch.object(lifecycle, "active", return_value=True))
            stack.enter_context(patch.object(lifecycle, "active_config",
                                             return_value={"/etc/xray": str(root)}))
            stack.enter_context(patch.object(lifecycle, "candidate_test"))
            stack.enter_context(patch.object(lifecycle, "image_id",
                                             side_effect={old: "old-id", new: digest, pinned: digest}.get))
            stack.enter_context(patch.object(lifecycle, "online_router_baseline",
                                             return_value=(root / "db", {"router": 12345})))

            def swap(image, data, *, expected_id):
                env.write_bytes(lifecycle.replace_image(data, image))
                if image == new:
                    raise RuntimeError("candidate failed to start")
                self.assertEqual(expected_id, "old-id")

            stack.enter_context(patch.object(lifecycle, "swap", side_effect=swap))
            stack.enter_context(redirect_stdout(io.StringIO()))
            with self.assertRaisesRegex(RuntimeError, "candidate failed to start"):
                lifecycle.apply(new)
            self.assertEqual(env.read_bytes(), before)
            state = json.loads(journal.read_text())
            self.assertEqual(state["phase"], "rolled-back")
            self.assertEqual(state["known_good"], [{"image": old, "image_id": "old-id"}])

    def test_prepare_uses_verified_image_id_for_config_and_local_tag(self):
        digest = "sha256:" + "a" * 64
        pinned = releases.REGISTRY + "@sha256:" + "b" * 64
        metadata = [{"Architecture": "amd64", "Os": "linux",
                     "Id": digest, "RepoDigests": [pinned]}]
        for architecture, verified_id, version in (
            ("amd64", digest, "26.11.9"),
            ("arm64", digest, "26.11.9"),
            ("amd64", "sha256:" + "c" * 64, "26.11.9"),
            ("amd64", digest, "26.11.90"),
        ):
            with self.subTest(architecture=architecture, digest=verified_id, version=version):
                metadata[0]["Architecture"] = architecture
                results = [Result(returncode=0), Result(returncode=0, stdout=json.dumps(metadata)),
                           Result(returncode=0), Result(returncode=0, stdout=verified_id),
                           Result(returncode=0, stdout=f"Xray {version} (Xray)"),
                           Result(returncode=1), Result(returncode=0)]
                validator = Mock()
                with tempfile.TemporaryDirectory() as work, redirect_stdout(io.StringIO()), \
                     patch.object(releases, "STAGED", Path(work)), \
                     patch.object(releases, "stable_release_tags", return_value=["v26.11.9"]), \
                     patch.object(releases, "command", side_effect=results) as command:
                    if (architecture, verified_id, version) != ("amd64", digest, "26.11.9"):
                        with self.assertRaises(RuntimeError):
                            releases.prepare_candidate("v26.11.9", validator)
                        validator.candidate_test.assert_not_called()
                        self.assertFalse(list(Path(work).iterdir()))
                        continue
                    releases.prepare_candidate("v26.11.9", validator)
                    self.assertEqual(command.call_args_list[2].args[-1], pinned)
                    validator.candidate_test.assert_called_once_with(digest)
                    self.assertEqual(command.call_args.args[-2:],
                                     (digest, "router-orchestrator-xray:26.11.9"))

    def test_retains_three_distinct_verified_images(self):
        versions = [{"image": f"tag{i}", "image_id": f"id{i}"} for i in range(4)]
        self.assertEqual(lifecycle.retain_versions(versions, versions[2]),
                         [versions[2], versions[0], versions[1]])

    def test_recovery_survives_interruption_on_either_side_of_env_swap(self):
        old = "router-orchestrator-xray:old"
        new = "router-orchestrator-xray:new"
        before = b"RO_XRAY_IMAGE=router-orchestrator-xray:old\r\nRO_HUB_IMAGE=hub:test"
        after = lifecycle.replace_image(before, new)
        self.assertEqual(lifecycle.replace_image(after, old), before)
        transaction = {
            "old_image": old, "new_image": new,
            "old_image_id": "old-id", "new_image_id": "new-id",
            "old_env_sha256": hashlib.sha256(before).hexdigest(),
            "new_env_sha256": hashlib.sha256(after).hexdigest(),
        }
        for phase, current in (("switching", before), ("switching", after),
                               ("rolling-back", after), ("rolling-back", before)):
            with self.subTest(phase=phase, current=current), ExitStack() as stack:
                work = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                env, journal = work / ".env", work / "journal.json"
                stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(patch.multiple(lifecycle, ENV=env, JOURNAL=journal))
                stack.enter_context(patch.object(lifecycle, "run", return_value=Result(returncode=1)))
                stack.enter_context(patch.object(lifecycle, "image_id", return_value="old-id"))
                swap = stack.enter_context(patch.object(lifecycle, "swap"))
                env.write_bytes(current)
                lifecycle.write_journal(transaction, phase)
                env.write_bytes(current + b"\n# unrelated edit")
                with self.assertRaisesRegex(RuntimeError, "environment changed"):
                    lifecycle.rollback()
                swap.assert_not_called()
                env.write_bytes(current)
                lifecycle.rollback()
                swap.assert_called_once_with(old, current, expected_id="old-id")
                self.assertEqual(json.loads(journal.read_text())["phase"], "rolled-back")

    def test_watchdog_recovers_unfinished_switch_after_hold_clears(self):
        with patch.dict(sys.modules, {"xray_lifecycle": lifecycle}):
            watchdog = load("core_test_watchdog", "xray_watchdog.py")
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            lock, env, hold, journal = (root / name for name in ("lock", "env", "hold", "journal"))
            env.touch()
            hold.touch()
            journal.write_text('{"phase":"switching"}')
            env.chmod(0o600)
            journal.chmod(0o600)
            with patch.multiple(lifecycle, LOCK=lock, ENV=env, JOURNAL=journal,
                                TRANSPORT_HOLD=hold), \
                 patch.object(watchdog.m, "LOCK", root / ".deploy.lock"), \
                 patch.object(watchdog, "configured_stack_env", return_value=None), \
                 patch.object(watchdog, "legacy_exclusion", side_effect=nullcontext), \
                 patch("stack_runtime.protected", side_effect=lambda path: path), \
                 patch.object(watchdog.os, "geteuid", return_value=0), \
                 patch.object(lifecycle, "rollback") as rollback, redirect_stdout(io.StringIO()):
                watchdog.run()
                rollback.assert_not_called()
                hold.unlink()
                watchdog.run()
                rollback.assert_called_once_with()

    def test_duplicate_worker_cannot_change_record_or_reexecute(self):
        job_id = "28dc8245-deeb-4221-b843-8d26ac8ee883"
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            lock = root / "lock"
            record = root / (job_id + ".json")
            record.write_text(json.dumps({"id": job_id, "status": "pending",
                                          "action": "restart", "version": ""}))
            record.chmod(0o600)
            with patch.multiple(jobs, ROOT=root, JOBS=root, LOCK=lock), \
                 patch.object(jobs.os, "geteuid", return_value=0), \
                 patch.object(jobs.Path, "stat", return_value=Result(st_uid=0, st_mode=0o600)), \
                 patch.object(jobs.Path, "mkdir"), \
                 patch.object(jobs.Path, "is_file", return_value=True), \
                 patch.object(jobs.Path, "is_symlink", return_value=False), \
                 patch.object(jobs.subprocess, "run", return_value=Result(
                     returncode=0, stdout="", stderr="")) as invoke:
                with lock.open("a+") as held:
                    fcntl.flock(held.fileno(), fcntl.LOCK_EX)
                    with self.assertRaises(BlockingIOError):
                        jobs.run_job(job_id)
                self.assertEqual(json.loads(record.read_text())["status"], "pending")
                self.assertEqual(jobs.run_job(job_id), 0)
                with self.assertRaisesRegex(ValueError, "already executed"):
                    jobs.run_job(job_id)
                self.assertEqual(json.loads(record.read_text())["status"], "success")
                invoke.assert_called_once()


if __name__ == "__main__":
    unittest.main()
