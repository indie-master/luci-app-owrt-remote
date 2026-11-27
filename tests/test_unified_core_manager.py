"""Offline fault injection for the unified Core Manager."""

import fcntl
import json
import os
import signal
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
import cleanup
import core_job_runner as jobs
import docker_stack_integration_ci as integration
import manager as m
from stack_runtime import MODES, OWNER, REVISION, Stack
import unified_core_manager as core
import xray_watchdog as watchdog
from unified_xray_adapter import Adapter, XRAY_TMPFS
from test_orchestrator_stack_config import fixture


class CoreManagerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = fixture(self.root, "standalone")
        self.config.update(RO_XRAY_BACKEND="unified_adapter",
                           RO_XRAY_IMAGE="ghcr.io/xtls/xray-core@sha256:" + "a" * 64)
        self.stack = Stack(self.root / "stack.env", self.config)
        self.adapter = Mock(spec=Adapter)
        self.manager = core.CoreManager(self.adapter)
        self.job_id = str(uuid.uuid4())
        for patcher in (patch.object(m, "LOCK", self.root / ".deploy.lock"),
                        patch.object(core.lifecycle, "LOCK", self.root / "legacy.lock"),
                        patch.object(core.lifecycle, "TRANSPORT_HOLD", self.root / "legacy-hold"),
                        patch.object(m, "LEGACY_HUB_STATE", self.root / "legacy-data"),
                        patch.object(m, "RELEASE_STATE", self.root / "hub-release.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.source = {"images": {"xray": "sha256:" + "1" * 64},
                       "config": self.config, "revision": "2" * 64}
        self.target = {"images": {"xray": "sha256:" + "3" * 64},
                       "config": dict(self.config, RO_XRAY_IMAGE="ghcr.io/xtls/xray-core@sha256:" + "b" * 64),
                       "revision": "4" * 64}

    def item(self, release):
        options = f"{m.HUB_UID}:{m.HUB_GID}"
        labels = {OWNER: self.stack.owner, REVISION: release["revision"],
                  "com.docker.compose.project": self.stack.name, "com.docker.compose.service": "xray"}
        targets = {"/etc/xray": "RO_XRAY_CONFIG_DIR", "/usr/local/share/xray": "RO_XRAY_ASSET_DIR",
                   "/etc/router-orchestrator/tls": "RO_TLS_DIR"}
        return {"Name": "/" + self.stack.name + "-xray", "Id": "5" * 64,
                "Image": release["images"]["xray"], "State": {"Status": "running"},
                "HostConfig": {"NetworkMode": "host", "ReadonlyRootfs": True, "Privileged": False,
                               "Tmpfs": XRAY_TMPFS},
                "Config": {"Labels": labels, "User": options,
                           "Cmd": ["run", "-config", "/etc/xray/owrt-remote.json"]},
                "Mounts": [{"Type": "bind", "RW": False, "Destination": target,
                            "Source": self.config[key]} for target, key in targets.items()]}

    def test_managed_ownership_in_all_modes(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.config = fixture(self.root, mode)
                self.stack = Stack(self.root / "stack.env", self.config)
                item = self.item(self.source)
                self.assertEqual(Adapter.owned_xray(self.stack, self.source, item), item["Id"])
                for label in (OWNER, REVISION, "com.docker.compose.project", "com.docker.compose.service"):
                    broken = self.item(self.source)
                    broken["Config"]["Labels"][label] = "other"
                    with self.assertRaisesRegex(m.Blocked, "identity"):
                        Adapter.owned_xray(self.stack, self.source, broken)
                for key in ("Name", "Id", "Image"):
                    broken = self.item(self.source)
                    broken[key] = "other"
                    with self.assertRaisesRegex(m.Blocked, "identity"):
                        Adapter.owned_xray(self.stack, self.source, broken)
                broken = self.item(self.source)
                broken["Mounts"][0]["Source"] += "-other"
                with self.assertRaisesRegex(m.Blocked, "bind-mounts"):
                    Adapter.owned_xray(self.stack, self.source, broken)

    def test_switch_rejects_changed_container_before_compose(self):
        stack = Mock()
        stack.inspect.return_value = {"xray": self.item(self.source)}
        self.adapter.owned_xray.return_value = "unexpected"
        with self.assertRaisesRegex(m.Blocked, "container changed"):
            self.manager.switch(stack, self.source, self.target, expected_container="expected")
        stack.compose.assert_not_called()

    def test_hold_and_pending_lifecycle_transactions(self):
        with patch.object(self.stack, "xray_hold", return_value=True), \
             patch.object(core.XrayState, "load") as load:
            with self.assertRaisesRegex(m.Blocked, "HOLD"):
                self.manager.guard(self.stack)
            load.assert_not_called()
        with patch.object(self.stack, "guard"), patch.object(m, "pending_transaction", return_value=True):
            with self.assertRaisesRegex(m.Blocked, "HUB-only"):
                self.manager.guard(self.stack)
        with patch.object(self.stack, "guard"), patch.object(m, "pending_transaction", return_value=False):
            for state in ({"pending": {"token": "x"}}, {"pending": None, "profile_pending": {"token": "x"}}):
                with patch.object(core.XrayState, "load", return_value=state):
                    with self.assertRaisesRegex(m.Blocked, "transport"):
                        self.manager.guard(self.stack)

    def test_core_worker_lock_excludes_transport_and_docker_lifecycle(self):
        with patch.object(m, "LOCK", self.root / ".deploy.lock"), patch.object(m, "require_root"):
            adapter = Adapter(self.stack.path)
            records = {"hub": {}}
            channel = Mock()
            channel.getsockopt.return_value = struct.pack("3i", 1, m.HUB_UID, m.HUB_GID)
            with patch.object(adapter, "open_project", return_value=(self.stack, self.source, records)),                  patch("unified_xray_adapter.peer_is_hub", return_value=True),                  patch("unified_xray_adapter.read_request", return_value={"op": "transport.recover"}), \
                 patch.object(adapter, "handle") as handle, m.deployment_lock():
                adapter.serve(channel)
                handle.assert_not_called()
                self.assertFalse(json.loads(channel.sendall.call_args.args[0])["ok"])
                for command in ("backup", "restore", "upgrade", "restart", "rollback"):
                    with self.subTest(command=command), self.assertRaisesRegex(m.Blocked, "operation is running"):
                        with m.deployment_lock():
                            self.fail("Lifecycle operation acquired the held lock")

    def test_legacy_lock_conflict_is_nonblocking_and_ordered(self):
        path = self.root / "xray-lifecycle.lock"
        with patch.object(m, "LOCK", self.root / ".deploy.lock"), patch.object(m, "require_root"), \
             patch.object(core.lifecycle, "LOCK", path):
            with self.assertRaisesRegex(m.Blocked, "deployment lock first"):
                with core.legacy_exclusion():
                    self.fail("Legacy lock acquired before deployment lock")
            with path.open("a+") as legacy:
                fcntl.flock(legacy, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with m.deployment_lock(), self.assertRaisesRegex(m.Blocked, "legacy lifecycle"):
                    with core.legacy_exclusion():
                        self.fail("Core operation entered under a conflicting legacy lock")

    def test_baseline_records_profile_portals_and_keeps_heartbeat_only_routers(self):
        self.stack.state.mkdir()
        (self.stack.state / "hub.db").touch()
        config = {"inbounds": [{"listen": "127.0.0.1", "protocol": "tunnel", "tag": tag, "port": port}
                               for tag, port in (("entry-r1", 20001), ("ssh-r1", 20002),
                                                 ("profile-luci", 20003), ("profile-ssh", 20004))]}
        transport = {"profiles": [{"assignments": {"r2": {"ports": [20003, 20004]}}}]}
        rows = [("r1", 999, 20001, 20002, "{}"), ("r2", 0, 0, 0, "{}"),
                ("r3", 500, 0, 0, '{"heartbeat_interval": 200}'), ("offline", 0, 0, 0, "{}")]
        with patch.object(core, "read_private", return_value=json.dumps(config)), \
             patch.object(core, "router_rows", return_value=rows), patch.object(core.time, "time", return_value=1000), \
             patch.object(core, "probe_channels", side_effect=lambda channels: {c: True for c in channels}):
            result = core.baseline(self.stack, transport)
        self.assertEqual(result, {"r1": {"channels": [("luci", 20001), ("ssh", 20002)]},
                                  "r2": {"channels": [("luci", 20003), ("ssh", 20004)]},
                                  "r3": {"channels": []}})

    def test_fresh_heartbeat_never_masks_channel_loss(self):
        expected = {"r1": {"channels": [("luci", 20001), ("ssh", 20002)]},
                    "r2": {"channels": [("luci", 20003), ("ssh", 20004)]}}
        with patch.object(core, "router_rows", return_value=[("r1", 11), ("r2", 12)]), \
             patch.object(core.time, "sleep"):
            for missing in (None, ("luci", 20003), ("ssh", 20004)):
                with self.subTest(missing=missing), \
                     patch.object(core.time, "monotonic", side_effect=[0, 0, 0 if missing is None else 2]), \
                     patch.object(core, "probe_channels",
                                  side_effect=lambda channels: {c: c != missing for c in channels}):
                    self.assertEqual(core.wait_routers(self.stack, expected, 10, timeout=1),
                                     missing is None)

    def test_recovery_switches_only_if_old_runtime_was_displaced(self):
        for observed, published in ((self.source, False), (self.target, False), (self.target, True),
                                    (None, False)):
            with self.subTest(observed=observed, published=published):
                config = self.target["config"] if published else self.source["config"]
                stack = SimpleNamespace(path=self.stack.path, config=config)
                before = Mock(path=self.stack.path, config=self.config)
                before.name = self.stack.name
                before.inspect.return_value = {"xray": self.item(observed)} if observed else {}
                record = {"current": self.source, "pending": {
                    "kind": "core", "action": "upgrade", "target": self.target, "id": self.job_id,
                    "config_sha256": "a" * 64, "baseline": {"r": {"channels": [["luci", 20001], ["ssh", 20002]]}}, "started": 10}}
                with patch.object(core, "Stack", return_value=before), \
                     patch.object(self.manager, "guard", return_value={"routers": {}}), \
                     patch.object(self.manager, "ready"), patch.object(m, "file_hash", return_value="a" * 64), \
                     patch.object(self.manager, "switch") as switch,                      patch.object(core, "wait_routers", return_value=True):
                    with m.deployment_lock(), core.legacy_exclusion():
                        self.manager.recover(stack, record)
                    self.assertEqual(switch.call_count, int(observed != self.source))
                    if observed is None:
                        switch.assert_called_once_with(before, self.target, self.source, expected_container=None)
                    self.assertEqual(before.publish.call_count, int(published))
                    persisted = before.save.call_args.args[0]
                    self.assertIsNone(persisted["pending"])
                    self.assertEqual(persisted["core_receipt"], {"id": self.job_id, "status": "failed"})

    def test_restart_is_never_replayed_during_recovery(self):
        stack = Mock()
        stack.path, stack.name = self.stack.path, self.stack.name
        stack.config = self.config
        stack.inspect.return_value = {"xray": self.item(self.source)}
        record = {"current": self.source, "pending": {
            "kind": "core", "action": "restart", "target": self.source, "id": self.job_id,
            "config_sha256": "a" * 64, "baseline": {"r": {"channels": [["luci", 20001], ["ssh", 20002]]}}, "started": 10}}
        with patch.object(core, "Stack", return_value=stack), \
             patch.object(self.manager, "guard", return_value={"routers": {}}), \
             patch.object(self.manager, "ready"), patch.object(m, "file_hash", return_value="a" * 64), \
             patch.object(self.manager, "switch") as switch, patch.object(core, "wait_routers", return_value=True):
            with m.deployment_lock(), core.legacy_exclusion():
                self.manager.recover(stack, record)
        switch.assert_not_called()
        self.adapter.restart.assert_not_called()

    def test_stale_job_reconciles_commit_receipt_without_reexecution(self):
        directory = jobs.unified_directory(self.stack)
        directory.mkdir(mode=0o700)
        for original, committed in (("running", True), ("running", False), ("pending", False)):
            with self.subTest(original=original, committed=committed):
                path = directory / (self.job_id + ".json")
                jobs.update(path, {"id": self.job_id, "status": original, "action": "upgrade"})
                record = {"current": self.source, "pending": None}
                if committed:
                    record["core_receipt"] = {"id": self.job_id, "status": "success"}
                with patch.object(core, "load_record", return_value=record),                      patch("unified_xray_state.read_private", side_effect=lambda path, _: path.read_bytes()):
                    jobs.settle_unified_job(self.stack, self.job_id)
                result = json.loads(path.read_text())
                self.assertEqual(result["status"], "success" if committed else "failed")
                self.assertGreater(result["finished_unix"], 0)

    def record_pending(self, action="upgrade"):
        return {"version": 1, "current": self.source, "pending": {
            "kind": "core", "id": self.job_id, "action": action,
            "target": self.source if action == "restart" else self.target,
            "config_sha256": "a" * 64, "baseline": {"r": {"channels": [["luci", 20001], ["ssh", 20002]]}},
            "started": 10,
        }}

    def test_watchdog_hold_and_lock_conflict_never_run_legacy_or_recover(self):
        self.stack.state.mkdir()
        (self.stack.state / ".xray-maintenance-hold").touch()
        self.stack.save({"version": 1, "current": self.source, "pending": None})
        with patch.dict(os.environ, {core.HOST_STACK_ENV: str(self.stack.path)}), \
             patch.object(Adapter, "open_project", return_value=(self.stack, self.source, {})) as opening, \
             patch.object(core.CoreManager, "recover") as recover, patch.object(watchdog, "run_legacy") as legacy:
            with self.assertRaisesRegex(m.Blocked, "HOLD"):
                watchdog.run()
            opening.reset_mock()
            with m.deployment_lock(), self.assertRaisesRegex(m.Blocked, "operation is running"):
                watchdog.run()
            opening.assert_not_called()
            recover.assert_not_called()
            legacy.assert_not_called()

    def test_missing_xray_recovery_respects_hold_and_retries_after_failed_create(self):
        self.stack.save(self.record_pending())
        self.stack.state.mkdir()
        hold = self.stack.state / ".xray-maintenance-hold"
        hold.touch()
        remaining = {"hub": {}, "nginx": {}}
        restored = dict(remaining, xray=self.item(self.source))
        with patch.dict(os.environ, {core.HOST_STACK_ENV: str(self.stack.path)}), \
             patch.object(Adapter, "open_project", return_value=(self.stack, self.source, remaining)), \
             patch.object(Stack, "state_permissions"), patch.object(m, "status_service", return_value=False), \
             patch.object(m, "ENV", self.root / "absent-legacy.env"), \
             patch.object(core.XrayState, "load", return_value={"routers": {}, "pending": None}), \
             patch.object(Stack, "verify_release") as verify, \
             patch.object(Stack, "inspect", side_effect=[remaining] * 4 + [restored]), \
             patch.object(m, "file_hash", return_value="a" * 64), \
             patch.object(Stack, "compose", side_effect=[OSError("create interrupted"), ""]) as compose, \
             patch.object(Adapter, "probe", return_value=True), \
             patch.object(core, "wait_routers", return_value=True) as proof:
            with self.assertRaisesRegex(m.Blocked, "HOLD"):
                watchdog.run()
            compose.assert_not_called()
            hold.unlink()
            with self.assertRaisesRegex(OSError, "create interrupted"):
                watchdog.run()
            self.assertEqual(core.load_record(self.stack)["pending"]["id"], self.job_id)
            proof.assert_not_called()
            watchdog.run()
        self.assertEqual(compose.call_count, 2)
        compose.assert_called_with("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                                   "--no-recreate", "xray", release=self.source)
        self.assertTrue(all(call.args == (self.source,) for call in verify.call_args_list))
        proof.assert_called_once()
        self.assertIsNone(core.load_record(self.stack)["pending"])

    def test_recovery_create_rejects_foreign_reserved_name(self):
        foreign = self.item(self.source)
        foreign["Config"]["Labels"]["com.docker.compose.project"] = "foreign"

        def inspect_docker(*args):
            if args[:2] == ("container", "inspect"):
                return json.dumps([foreign])
            return foreign["Id"] if args[-1] == f"name=^/{self.stack.name}-xray$" else ""

        with patch.object(self.stack, "guard"), patch.object(self.stack, "verify_release"), \
             patch.object(self.stack, "inspect_docker", side_effect=inspect_docker), \
             patch.object(self.stack, "compose") as compose:
            with self.assertRaisesRegex(m.Blocked, "Unexpected container"):
                self.manager.switch(self.stack, self.target, self.source, expected_container=None)
        compose.assert_not_called()

    def test_watchdog_reconciles_after_killed_worker_and_defers_terminal_status_until_receipt(self):
        self.stack.save(self.record_pending("restart"))
        directory = jobs.unified_directory(self.stack)
        directory.mkdir(mode=0o700)
        path = directory / (self.job_id + ".json")
        # Simulate an older worker that wrote failed before recovery succeeded.
        jobs.update(path, {"id": self.job_id, "action": "restart", "status": "failed", "finished_unix": 5})
        with m.deployment_lock():
            jobs.settle_unified_job(self.stack, self.job_id)
        self.assertEqual(jobs.unified_job_status(self.stack, self.job_id)["job"]["status"], "running")

        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(reader)
            try:
                with m.deployment_lock(), core.legacy_exclusion():
                    os.write(writer, b"1")
                    signal.pause()
            finally:
                os._exit(1)
        os.close(writer)
        try:
            self.assertEqual(os.read(reader, 1), b"1")
        finally:
            os.close(reader)
            os.kill(pid, signal.SIGKILL)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)

        with patch.dict(os.environ, {core.HOST_STACK_ENV: str(self.stack.path)}), \
             patch.object(Adapter, "open_project", return_value=(self.stack, self.source, {})), \
             patch.object(core.CoreManager, "guard", return_value={"routers": {}}), \
             patch.object(Stack, "verify_release"), patch.object(Stack, "inspect", return_value={
                 "xray": self.item(self.source)}), patch.object(m, "file_hash", return_value="a" * 64), \
             patch.object(core.CoreManager, "ready"), patch.object(core, "wait_routers", return_value=True), \
             patch.object(Adapter, "restart") as restart, patch.object(Stack, "compose") as compose, \
             patch.object(watchdog, "run_legacy") as legacy:
            watchdog.run()
            watchdog.run()
        restart.assert_not_called()
        compose.assert_not_called()
        legacy.assert_not_called()
        record = core.load_record(self.stack)
        self.assertIsNone(record["pending"])
        self.assertEqual(record["core_receipt"], {"id": self.job_id, "status": "failed"})
        self.assertEqual(jobs.unified_job_status(self.stack, self.job_id)["job"]["status"], "failed")

    def test_watchdog_blocks_foreign_runtime(self):
        self.stack.save(self.record_pending())
        item = self.item(self.target)
        item["Config"]["Labels"][OWNER] = "foreign"
        with patch.dict(os.environ, {core.HOST_STACK_ENV: str(self.stack.path)}), \
             patch.object(Adapter, "open_project", return_value=(self.stack, self.source, {})), \
             patch.object(core.CoreManager, "guard", return_value={"routers": {}}), \
             patch.object(Stack, "verify_release"), patch.object(Stack, "inspect", return_value={"xray": item}), \
             patch.object(m, "file_hash", return_value="a" * 64), patch.object(Stack, "compose") as compose:
            with self.assertRaisesRegex(m.Blocked, "ownership"):
                watchdog.run()
        compose.assert_not_called()
        self.assertEqual(core.load_record(self.stack)["pending"]["id"], self.job_id)

    def test_cleanup_protects_known_good_aliases_and_pending_images(self):
        def release(core_release):
            fields = {"config": core_release["config"], "material": "test",
                      "images": dict(core_release["images"], hub="sha256:" + "4" * 64,
                                     nginx="sha256:" + "5" * 64)}
            return dict(fields, revision=cleanup.digest(fields), model="test")

        source, target = release(self.source), release(self.target)
        history = [{"image": "ghcr.io/xtls/xray-core@sha256:" + key * 64,
                    "image_id": "sha256:" + key * 64} for key in ("6", "7")]
        record = dict(self.record_pending(), current=source, core_known_good=history)
        record["pending"]["target"] = target
        self.stack.save(record)
        xray = self.item(source)
        xray["State"]["Running"] = True
        containers = {"xray": xray, "hub": {}, "nginx": {}}
        live_images = {source["config"][cleanup.IMAGES[service]]: image_id
                       for service, image_id in source["images"].items()}
        live_images.update({target["config"]["RO_XRAY_IMAGE"]: target["images"]["xray"]})
        live_images.update({entry["image"]: entry["image_id"] for entry in history})
        aliases = {f"router-orchestrator-xray:1.0.{index}": image_id
                   for index, image_id in enumerate(live_images.values())}
        other = "router-orchestrator-hub:unowned"
        removable = "router-orchestrator-hub:unused"

        def images(tag):
            if tag in {other, removable}:
                labels = {m.SOURCE_LABEL: "8" * 64} if tag == removable else {}
                return {"Id": "sha256:" + "8" * 64, "Config": {"Labels": labels}}
            return {"Id": (live_images | aliases)[tag]}

        with patch.object(m, "pending_transaction", return_value=None), \
             patch.object(cleanup.life, "ROOT", self.root), patch.object(cleanup.life, "JOURNAL", self.root / "legacy.json"), \
             patch.object(cleanup, "open_settings", return_value=self.config), \
             patch.object(core.CoreManager, "guard"), patch.object(Stack, "verify_release"), \
             patch.object(Stack, "inspect", return_value=containers), patch.object(cleanup, "inventory", return_value={}), \
             patch.object(cleanup, "image", side_effect=images), patch.object(cleanup, "docker", return_value=
                 "\n".join([*aliases, other, removable])) as docker:
            self.assertEqual(cleanup.plan(self.stack.path), ({}, {removable: "sha256:" + "8" * 64}))
            with self.assertRaisesRegex(m.Blocked, "Pending Stack"):
                cleanup.plan(self.stack.path, applying=True)
            self.stack.journal.write_text("{corrupt")
            docker.reset_mock()
            with self.assertRaises(ValueError):
                cleanup.plan(self.stack.path)
            docker.assert_not_called()

    def test_missing_or_unverified_second_image_blocks_integration(self):
        pinned = "ghcr.io/xtls/xray-core@sha256:" + "a" * 64
        for candidate in ({}, {"RO_CI_CORE_CANDIDATE_VERSION": "1.2.3"},
                          {"RO_CI_CORE_CANDIDATE_IMAGE": pinned, "RO_CI_XRAY_IMAGE": pinned,
                           "RO_CI_CORE_CANDIDATE_VERSION": "1.2.3"}):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(SystemExit, "Core acceptance blocked"):
                integration.core_candidate(candidate)


if __name__ == "__main__":
    unittest.main()
