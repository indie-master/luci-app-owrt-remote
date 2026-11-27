"""Offline installation tests using private fixture directories."""

from pathlib import Path
import os
import shutil
import stat
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
import manager as m
import stack_host_install as installer
from stack_runtime import run
from test_orchestrator_stack_config import fixture


class HostInstallTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        project = self.base / "project"
        project.mkdir(mode=0o700)
        self.env = project / "stack.env"
        self.config = fixture(project, "external-only")
        self.config["RO_XRAY_BACKEND"] = "unified_adapter"
        self.write_env()
        for key in ("RO_XRAY_CONFIG_DIR", "RO_XRAY_ASSET_DIR", "RO_TLS_DIR"):
            Path(self.config[key]).mkdir(mode=0o700)
        self.units = self.base / "units"
        self.units.mkdir(mode=0o700)
        src = self.base / "src"
        (src / "deploy").mkdir(mode=0o700, parents=True)
        src.chmod(0o700)
        shutil.copytree(m.ROOT / "deploy" / "systemd", src / "deploy" / "systemd")
        (src / "deploy" / "systemd").chmod(0o700)
        (src / "docker").mkdir(mode=0o700)
        for name in ("host_adapter.py", "xray_watchdog.py"):
            (src / "docker" / name).touch(mode=0o600)
        for path in (src / "deploy" / "systemd").iterdir():
            path.chmod(0o600)
        self.selector = self.base / "managed-stack.conf"
        for patcher in (
            patch.object(m, "HUB_DEPLOY_ROOT", self.base),
            patch.object(m, "LOCK", self.base / ".deploy.lock"),
            patch.object(installer, "UNIT_DIRECTORY", self.units),
            patch.object(installer, "OTHER_UNIT_DIRECTORIES", (self.base / "runtime",)),
            patch.dict(os.environ, {installer.HOST_STACK_ENV: str(self.env)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_env(self):
        m.durable_write(self.env, "".join(f"{key}={value}\n" for key, value in self.config.items()))

    def install(self):
        with m.deployment_lock(), patch.object(m, "call") as call:
            run(SimpleNamespace(env_file=self.env, operation="install-host-services"))
            call.assert_not_called()

    def test_installs_watchdog_timer_and_adapter_idempotently(self):
        self.install()
        files = (self.selector, *(self.units / name for name in (
            installer.ADAPTER, installer.WATCHDOG, installer.TIMER)))
        self.assertEqual(self.selector.read_text(), f"{installer.HOST_STACK_ENV}={self.env}\n")
        self.assertEqual(stat.S_IMODE(self.selector.stat().st_mode), 0o600)
        self.assertTrue(all(path.stat().st_uid == 0 for path in files))
        for path, entry in ((files[1], "host_adapter.py"), (files[2], "xray_watchdog.py")):
            text = path.read_text()
            self.assertIn(f"Environment={installer.HOST_STACK_ENV}=\n", text)
            self.assertIn(f"EnvironmentFile={self.selector}\n", text)
            self.assertIn(f"ExecStart=/usr/bin/python3 {self.base}/src/docker/{entry}\n", text)
            self.assertNotIn("--stack-env", text)
            self.assertNotIn("--allow-xray-", text)
        self.assertIn(f"Unit={installer.WATCHDOG}\n", files[3].read_text())
        before = [path.stat().st_ino for path in files]
        self.install()
        self.assertEqual(before, [path.stat().st_ino for path in files])

    def test_conflicting_unit_or_selector_prevents_writes(self):
        drop_in = self.units / (installer.ADAPTER + ".d")
        drop_in.mkdir()
        with self.assertRaisesRegex(m.Blocked, "Conflicting systemd"):
            self.install()
        self.assertFalse(self.selector.exists())
        drop_in.rmdir()
        m.durable_write(self.selector, f"{installer.HOST_STACK_ENV}=/other/stack.env\n")
        with self.assertRaisesRegex(m.Blocked, "Conflicting managed Stack"):
            self.install()
        self.assertEqual(list(self.units.iterdir()), [])

    def test_rejects_aliased_paths(self):
        directory = Path(self.config["RO_XRAY_CONFIG_DIR"])
        alias = directory.with_name("alias")
        alias.symlink_to(directory)
        self.config["RO_XRAY_CONFIG_DIR"] = str(alias)
        self.write_env()
        with self.assertRaisesRegex((m.Blocked, ValueError), "path"):
            self.install()
        self.assertFalse(self.selector.exists())
