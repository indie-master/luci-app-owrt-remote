"""Offline security/contract tests for Xray version control."""
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "docker" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


jobs = load("core_job_runner", "core_job_runner.py")
lifecycle = load("xray_lifecycle", "xray_lifecycle.py")
releases = load("xray_releases", "xray_releases.py")


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


if __name__ == "__main__":
    unittest.main()
