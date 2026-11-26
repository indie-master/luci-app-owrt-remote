"""Offline regression coverage for the unified Docker Manager."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from test_orchestrator_stack_config import fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker"))
import manager
import stack_runtime as stack
from docker_stack_integration_ci import diagnose_gate


def config_document():
    return {"inbounds": [
        {"listen": "127.0.0.1", "port": 28444, "protocol": "vless",
         "streamSettings": {"network": "ws", "wsSettings": {"path": "/owrt-ws/"}}},
        {"listen": "127.0.0.1", "port": 24443, "protocol": "vless",
         "streamSettings": {"network": "raw", "security": "tls"}},
    ]}


class StackRuntimeTests(unittest.TestCase):
    def setup_stack(self, mode="standalone"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        return stack.Stack(self.root / "stack.env", fixture(self.root, mode))

    def test_ci_lock_does_not_reserve_isolated_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(manager, "LOCK", root / ".deploy.lock"):
                self.assertEqual(manager.journal_path(),
                                 Path("/opt/router-orchestrator/.transaction.json"))
                for mode in stack.MODES:
                    with self.subTest(mode=mode):
                        runtime = stack.Stack(root / "stack.env", fixture(root, mode))
                        self.assertEqual(runtime.state, root / "data")
                        runtime.state_permissions(initializing=True)

    def test_ci_lock_does_not_unreserve_legacy_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(manager, "LOCK", root / ".deploy.lock"), \
                 patch.object(Path, "stat", autospec=True,
                              return_value=Mock(st_mode=0o700)), \
                 patch.object(Path, "resolve", autospec=True, side_effect=lambda path: path):
                legacy = Path("/opt/router-orchestrator/data")
                for parent in (legacy.parent, legacy, legacy / "nested"):
                    with self.subTest(parent=parent), self.assertRaisesRegex(
                        manager.Blocked, "The existing HUB state is reserved"
                    ):
                        stack.Stack(parent / "stack.env", fixture(parent, "standalone"))

    def test_backup_blocks_running_stack_and_catalog_symlink(self):
        runtime = self.setup_stack()
        runtime.path.touch()
        runtime.journal.touch()
        runtime.plan["nginx_conf"].parent.mkdir(exist_ok=True)
        runtime.plan["nginx_conf"].write_text("test configuration")
        with patch.object(runtime, "verify_release"), \
             patch.object(runtime, "inspect") as inspect, \
             patch.object(manager, "backup_state") as archive:
            inspect.return_value = {"hub": {"State": {"Status": "running"}}}
            with self.assertRaisesRegex(manager.Blocked, "Stop the stack"):
                runtime.backup({})
            archive.assert_not_called()
            inspect.return_value = {"hub": {"State": {"Status": "exited"}}}
            runtime.transport_store.symlink_to(self.root / "missing")
            with self.assertRaisesRegex(manager.Blocked, "Unsafe path alias"):
                runtime.backup({})
            archive.assert_not_called()

    def test_ci_gate_diagnostics_hide_raw_errors(self):
        output = io.StringIO()
        runtime = self.setup_stack()

        def preflight(_stack, _release):
            try:
                raise subprocess.CalledProcessError(1, ["secret-argument"], stderr="secret-output")
            except subprocess.CalledProcessError as error:
                raise manager.Blocked("xray configuration test failed") from error

        def await_ready(_stack, _release):
            raise manager.Blocked("Readiness failed: hub, HTTPS routing")

        def unexpected(_stack, _release):
            raise manager.Blocked("Readiness failed: hub, secret-value")

        with patch.object(sys, "stderr", output):
            gates = [diagnose_gate(gate) for gate in (preflight, await_ready, unexpected)]
        for gate in gates:
            with self.assertRaises(manager.Blocked):
                gate(runtime, {})
        result = output.getvalue()
        self.assertIn("preflight, mode=standalone", result)
        self.assertIn("xray configuration test failed", result)
        self.assertIn("Gate command exit status: 1", result)
        self.assertIn("Failed readiness checks: hub, HTTPS routing", result)
        self.assertNotIn("secret", result)

    def test_xray_checks_loopback_protocol_path_and_port_topology(self):
        runtime = self.setup_stack()
        stack.check_xray(config_document(), runtime.plan)
        for index, change in (
            (0, {"listen": "0.0.0.0"}), (0, {"port": 24443}),
            (0, {"port": "28444"}), (0, {"protocol": "http"}),
            (0, {"streamSettings": []}),
            (0, {"streamSettings": {"network": "ws", "wsSettings": {"path": "/wrong/"}}}),
            (1, {"streamSettings": {"network": "raw", "security": "none"}}),
        ):
            with self.subTest(change=change), self.assertRaises(manager.Blocked):
                config = config_document()
                config["inbounds"][index].update(change)
                stack.check_xray(config, runtime.plan)

    def test_managed_ports_respect_ingress_ownership(self):
        for mode in stack.MODES:
            with self.subTest(mode=mode):
                runtime = self.setup_stack(mode)
                ports = runtime.ports()
                self.assertIn(("127.0.0.1", 28444), ports["xray"])
                self.assertIn(("127.0.0.1", 24443), ports["xray"])
                self.assertEqual("nginx" in ports, mode != "external-only")
                self.assertEqual(("0.0.0.0", 8443) in ports.get("nginx", []), mode == "standalone")

    def test_compose_pins_profile_project_and_images_and_ignores_overrides(self):
        runtime = self.setup_stack("external-only")
        images = {name: "sha256:" + char * 64 for name, char in (("hub", "1"), ("xray", "2"))}
        release = {"revision": "fixture", "images": images}
        with patch.dict(os.environ, COMPOSE_PROFILES="standalone", DOCKER_HOST="tcp://invalid",
                        DOCKER_CONTEXT="foreign", RO_STACK_OWNER="foreign"), \
             patch.object(manager.subprocess, "run", return_value=Mock(stdout="")) as execute:
            runtime.compose("config", "--quiet", release=release)
        command = execute.call_args.args[0]
        env = execute.call_args.kwargs["env"]
        self.assertIn("unix:///var/run/docker.sock", command)
        self.assertEqual(command[command.index("--profile") + 1], "external-only")
        self.assertEqual(command[command.index("--project-name") + 1], runtime.name)
        self.assertEqual(env["RO_XRAY_IMAGE"], images["xray"])
        self.assertEqual(env["RO_HUB_RUNTIME_IMAGE"], images["hub"])
        self.assertEqual(env["RO_STACK_OWNER"], runtime.owner)
        self.assertNotIn("COMPOSE_PROFILES", env)
        self.assertNotIn("DOCKER_HOST", env)
        self.assertNotIn("DOCKER_CONTEXT", env)

    def test_preflight_runs_real_binary_commands_in_pinned_compose_services(self):
        for mode in stack.MODES:
            with self.subTest(mode=mode):
                runtime = self.setup_stack(mode)
                release = {"images": {}, "revision": "fixture"}
                with patch.object(runtime, "verify_release") as verify, \
                     patch.object(stack, "verify_certificate") as cert, \
                     patch.object(runtime, "compose") as compose:
                    runtime.preflight(release)
                commands = [entry.args[6:] for entry in compose.call_args_list]
                self.assertEqual(commands[0], ("xray", "run", "-test", "-config",
                                                "/etc/xray/owrt-remote.json"))
                if mode == "external-only":
                    self.assertEqual(len(commands), 1)
                    cert.assert_not_called()
                else:
                    self.assertEqual(commands[1], ("nginx", "nginx", "-t"))
                    cert.assert_called_once()
                self.assertEqual(verify.call_count, 2)

    def test_hold_blocks_before_other_host_checks(self):
        runtime = self.setup_stack()
        runtime.state.mkdir()
        (runtime.state / ".xray-maintenance-hold").symlink_to("missing")
        with patch.object(manager, "status_service") as check:
            with self.assertRaisesRegex(manager.Blocked, "HOLD"):
                runtime.guard()
            check.assert_not_called()

    def test_legacy_hold_and_configured_overlap_are_checked_with_isolated_lock(self):
        runtime = self.setup_stack()
        legacy = self.root / "legacy"
        legacy.mkdir()
        hold = legacy / ".xray-maintenance-hold"
        hold.symlink_to("missing")
        legacy_env = self.root / "legacy.env"
        config = {key: runtime.config[key] for key in manager.ALLOWED}
        config["RO_STATE_DIR"] = str(runtime.state / "nested")
        with patch.multiple(manager, LOCK=self.root / ".deploy.lock", LEGACY_HUB_STATE=legacy,
                            ENV=legacy_env), patch.object(manager, "status_service", return_value=False):
            manager.save_env(config)
            with self.assertRaisesRegex(manager.Blocked, "HOLD"):
                runtime.guard()
            hold.unlink()
            with self.assertRaisesRegex(manager.Blocked, "overlaps the existing HUB-only installation"):
                runtime.guard()

    def test_foreign_container_cannot_be_adopted(self):
        runtime = self.setup_stack()
        foreign = {"Image": "image", "Name": "/" + runtime.name + "-xray",
                   "Config": {"Labels": {
                       "com.docker.compose.project": runtime.name,
                       "com.docker.compose.service": "xray",
                       stack.REVISION: "revision", stack.OWNER: "wrong-owner",
                   }}}
        with patch.object(stack, "docker", side_effect=lambda *args, **_:
                          json.dumps([foreign]) if args[0] == "container" else "foreign-id"):
            with self.assertRaisesRegex(manager.Blocked, "Unexpected container"):
                runtime.inspect({"images": {"xray": "image"}, "revision": "revision"})

    def test_failed_binary_test_does_not_emit_configuration(self):
        runtime = self.setup_stack("external-only")
        error = subprocess.CalledProcessError(1, ("docker",), stderr="sensitive-content")
        with patch.object(runtime, "verify_release"), patch.object(runtime, "compose", side_effect=error):
            with self.assertRaisesRegex(manager.Blocked, "^xray configuration test failed$"):
                runtime.preflight({})

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required")
    def test_certificate_requires_correct_hostname_key_and_permissions(self):
        runtime = self.setup_stack()
        tls = self.root / "tls"
        tls.mkdir()
        host = runtime.plan["host"]
        key = tls / "privkey.pem"
        manager.call("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                     "-subj", "/CN=" + host, "-addext", "subjectAltName=DNS:" + host,
                     "-keyout", str(key), "-out", str(tls / "fullchain.pem"))
        stack.verify_certificate(tls, host)
        with self.assertRaises(subprocess.CalledProcessError):
            stack.verify_certificate(tls, "different.example.invalid")
        manager.call("openssl", "genpkey", "-algorithm", "RSA", "-out", str(key))
        with self.assertRaisesRegex(manager.Blocked, "does not match"):
            stack.verify_certificate(tls, host)
        key.chmod(0o644)
        with self.assertRaisesRegex(manager.Blocked, "private privkey"):
            stack.verify_certificate(tls, host)

    def test_occupied_port_keeps_other_starts_blocked(self):
        runtime = self.setup_stack("external-only")
        candidate = Mock()
        candidate.bind.side_effect = OSError()
        with patch.object(stack.socket, "socket", return_value=MagicMock(__enter__=Mock(return_value=candidate))):
            with self.assertRaisesRegex(manager.Blocked, "xray TCP port .* occupied"):
                runtime.ports_free({"hub"})

    def test_recovery_stops_candidate_and_blocks_old_writer_on_changed_schema(self):
        runtime = self.setup_stack()
        source = {"config": runtime.config}
        target = {"config": dict(runtime.config, RO_IMAGE="router-orchestrator-hub:next")}
        tx = {"current": source, "pending": {"target": target, "schema": "old-schema"}}
        with patch.object(runtime, "guard"), patch.object(runtime, "stop") as stop, \
             patch.object(runtime, "preflight") as preflight, patch.object(runtime, "up") as up, \
             patch.object(manager, "schema_fingerprint", return_value="new-schema"):
            with self.assertRaisesRegex(manager.Blocked, "automatic rollback blocked"):
                stack.recovered(runtime, tx)
            stop.assert_called_once_with(source, target)
            preflight.assert_not_called()
            up.assert_not_called()

    def test_recovery_converges_using_env_commit_boundary(self):
        runtime = self.setup_stack()
        source = {"config": runtime.config}
        moved = dict(runtime.config, RO_IMAGE="router-orchestrator-hub:next")
        target = {"config": moved}
        tx = {"current": source, "pending": {"target": target, "schema": "old", "target_schema": "new"}}
        for config, expected, previous, schema in ((runtime.config, source, target, "old"),
                                                   (moved, target, source, "new")):
            with self.subTest(schema=schema):
                chosen = stack.Stack(runtime.path, config)
                with patch.object(chosen, "guard"), patch.object(chosen, "stop") as stop, \
                     patch.object(chosen, "preflight"), patch.object(chosen, "up") as up, \
                     patch.object(chosen, "save") as save, \
                     patch.object(manager, "schema_fingerprint", return_value=schema):
                    stack.recovered(chosen, tx)
                up.assert_called_once_with(expected, previous)
                self.assertEqual(stop.called, schema == "old")
                self.assertIsNone(save.call_args.args[0]["pending"])
                self.assertEqual(save.call_args.args[0]["current"], expected)

    def test_update_refuses_port_or_mode_changes_before_docker(self):
        runtime = self.setup_stack()
        for change in ({"RO_PORT": "25000"}, {"RO_STACK_MODE": "external-only"}):
            candidate = stack.Stack(runtime.path, dict(runtime.config, **change))
            with self.assertRaisesRegex(manager.Blocked, "cannot change"):
                stack.update(runtime, {"current": {"config": runtime.config}}, candidate)

    def test_default_project_is_reserved_for_hub_only(self):
        runtime = self.setup_stack()
        config = dict(runtime.config, RO_STACK_NAME="router-orchestrator")
        with self.assertRaisesRegex(manager.Blocked, "reserved"):
            stack.Stack(runtime.path, config)

    def test_repair_does_not_restart_healthy_components(self):
        runtime = self.setup_stack()
        release = {"images": {}}
        containers = {service: {"State": {"Status": "running"}, "Id": service}
                      for service in runtime.services}
        with patch.object(runtime, "readiness", return_value={"hub": True, "xray": False, "nginx": True}), \
             patch.object(runtime, "guard"), patch.object(runtime, "inspect", return_value=containers), \
             patch.object(runtime, "up") as up, patch.object(stack, "docker") as docker:
            runtime.repair(release)
        docker.assert_called_once_with("stop", "xray")
        up.assert_called_once_with(release)


if __name__ == "__main__":
    unittest.main()
