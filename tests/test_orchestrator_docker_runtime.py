"""Runtime safety checks with no Docker daemon or VPS required."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docker_manager", REPO / "docker" / "manager.py")
mgr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mgr)

class DockerRuntimeContracts(unittest.TestCase):
    def test_hub_dockerfile_copy_sources_are_in_build_context(self):
        dockerfile = (REPO / "docker/hub/Dockerfile").read_text()
        allowlist = (REPO / ".dockerignore").read_text().splitlines()
        for line in dockerfile.splitlines():
            if line.startswith("COPY "):
                words = line.removeprefix("COPY ").split()
                source = next(word for word in words if not word.startswith("--"))
                self.assertIn("!" + source, allowlist)

    def test_compose_requires_explicit_readonly_xray(self):
        compose = (REPO / "docker-compose.yml").read_text()
        self.assertIn("network_mode: host", compose)
        self.assertIn("127.0.0.1", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("OWRT_REMOTE_RUNTIME: docker", compose)
        self.assertNotIn("docker.sock", compose)

    def test_source_disables_systemd_update_and_requires_pinned_host_terminal(self):
        source = (REPO / "vps" / "owrt-remote-hub.py").read_text()
        self.assertIn("if DOCKER_RUNTIME:", source)
        self.assertIn("StrictHostKeyChecking=yes", source)
        self.assertIn("UserKnownHostsFile=/opt/owrt-remote/.host-terminal-known", source)
        self.assertIn("roterm@127.0.0.1", source)
        self.assertIn("allowed = self.owner_ok()", source)
        self.assertIn("Docker HUB updates require a reviewed image", source)
        self.assertIn("external_readonly", (REPO / "docker-compose.yml").read_text())

    def test_env_rejects_unsafe_backend(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text("RO_IMAGE=router-orchestrator-hub:a\n"
                         "RO_STATE_DIR=/opt/router-orchestrator/data\n"
                         "RO_PORT=18089\n"
                         "RO_PUBLIC_URL=https://example.net\n"
                         "RO_XRAY_BACKEND=systemd\n")
            with patch.object(mgr, "ENV", p):
                with self.assertRaises(mgr.Blocked):
                    mgr.settings()

    def test_env_accepts_readonly_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / ".env"
            p.write_text("RO_IMAGE=router-orchestrator-hub:abcdef\n"
                         "RO_STATE_DIR=/opt/router-orchestrator/data\n"
                         "RO_PORT=18089\n"
                         "RO_PUBLIC_URL=https://example.net\n"
                         "RO_XRAY_BACKEND=external_readonly\n")
            with patch.object(mgr, "ENV", p):
                self.assertEqual(mgr.settings()["RO_PORT"], "18089")

    def test_compose_preflight_uses_explicit_model_and_validated_settings(self):
        argv = SimpleNamespace(non_interactive=True, public_url="https://hub.example.invalid",
                               port=18089, state_dir="/opt/router-orchestrator/local-fixture")
        with patch.dict(os.environ, {"COMPOSE_FILE": "other.yml", "COMPOSE_PROJECT_NAME": "other",
                                     "RO_PORT": "12345", "RO_XRAY_BACKEND": "other"}), \
             patch.multiple(mgr, require_root=lambda: None, status_service=lambda: False,
                            free_port=lambda _: True, require_new_destination=lambda _: None,
                            build_image=lambda: "router-orchestrator-hub:fixture"), \
             patch.object(mgr, "ENV", Path("/unused/.env")), \
             patch.object(mgr.subprocess, "run") as run, \
             patch.object(mgr, "save_env") as save, patch.object(Path, "mkdir") as mkdir:
            run.side_effect = [SimpleNamespace(returncode=0), OSError("invalid model")]
            with self.assertRaisesRegex(OSError, "invalid model"):
                mgr.install(argv)
            args, kwargs = run.call_args
            self.assertEqual(args[0][-2:], ["config", "--quiet"])
            self.assertIn(str(REPO / "docker-compose.yml"), args[0])
            self.assertEqual(kwargs["env"]["RO_PORT"], "18089")
            self.assertEqual(kwargs["env"]["RO_XRAY_BACKEND"], "external_readonly")
            self.assertFalse(any(k.startswith("COMPOSE_") for k in kwargs["env"]))
            mkdir.assert_not_called()
            save.assert_not_called()

    def test_start_uses_id_even_when_local_tag_is_reassigned(self):
        identifier = "sha256:" + "a" * 64
        config = {"RO_IMAGE": "router-orchestrator-hub:fixture"}
        with patch.object(mgr, "settings", return_value=config), \
             patch.object(mgr.subprocess, "run") as run:
            mgr.compose_up(identifier)
        args, kwargs = run.call_args
        self.assertIn("never", args[0])
        self.assertEqual(kwargs["env"]["RO_HUB_RUNTIME_IMAGE"], identifier)
        self.assertEqual(kwargs["env"]["RO_IMAGE"], config["RO_IMAGE"])

    def test_health_rejects_a_different_hub_revision(self):
        with patch.object(mgr.http.client, "HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"ok":true,"revision":"unexpected"}'
            self.assertFalse(mgr.health(18089, attempts=1, revision="expected"))
            connection.return_value.request.assert_called_once_with("GET", "/health?panel-update=1")
            response.read.return_value = b'{"ok":true,"revision":"expected"}'
            self.assertTrue(mgr.health(18089, attempts=1, revision="expected"))

    def test_host_service_errors_and_transition_fail_closed(self):
        for stdout, code, active in (
            ("", 0, False), ("owrt-remote.service loaded inactive dead", 0, False),
            ("owrt-remote.service loaded deactivating stop", 0, True),
            ("", 1, None),
        ):
            with self.subTest(stdout=stdout, code=code), patch.object(
                mgr, "call", return_value=SimpleNamespace(stdout=stdout, returncode=code)
            ):
                if active is None:
                    with self.assertRaises(mgr.Blocked):
                        mgr.status_service()
                else:
                    self.assertEqual(mgr.status_service(), active)

    def test_manager_operations_are_serialized(self):
        # No production paths are accessed by this test.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(mgr, "LOCK", root / ".deploy.lock"), \
                 patch.object(mgr, "require_root", lambda: None):
                with mgr.deployment_lock():
                    self.assertEqual((root / ".deploy.lock").stat().st_mode & 0o777, 0o600)
                    with self.assertRaisesRegex(mgr.Blocked, "Another Router Orchestrator"):
                        with mgr.deployment_lock():
                            pass
                with mgr.deployment_lock():
                    pass

    def test_manager_rejects_symlink_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = root / "other"
            target.write_text("do-not-touch")
            lock = root / ".deploy.lock"
            lock.symlink_to(target)
            with patch.object(mgr, "LOCK", lock), \
                 patch.object(mgr, "require_root", lambda: None):
                with self.assertRaises(OSError):
                    with mgr.deployment_lock():
                        pass
            self.assertEqual(target.read_text(), "do-not-touch")

    @staticmethod
    def sample_new_wss_config():
        legacy = {"tag": "owrt-remote-vless", "listen": "0.0.0.0",
                  "port": 18443, "protocol": "vless",
                  "settings": {"clients": [], "decryption": "none"},
                  "streamSettings": {"network": "tcp", "security": "none"}}
        wss = {"tag": "owrt-remote-wss", "listen": "127.0.0.1",
               "port": 18444, "protocol": "vless",
               "settings": {"clients": [
                   {"id": "old-admin", "email": "old@owrt-remote",
                    "reverse": {"tag": "reverse-old"}},
                   {"id": "old-ssh", "email": "old-ssh@owrt-remote",
                    "reverse": {"tag": "reverse-old-ssh"}}],
                            "decryption": "none"},
               "streamSettings": {"network": "ws", "security": "none",
                                  "wsSettings": {"path": "/owrt-ws/"}}}
        old_admin = {"tag": "entry-old", "listen": "127.0.0.1",
                     "port": 18090, "protocol": "tunnel",
                     "settings": {"allowedNetwork": "tcp",
                                  "rewriteAddress": "127.0.0.1", "rewritePort": 80}}
        old_ssh = {"tag": "ssh-old", "listen": "127.0.0.1",
                   "port": 19090, "protocol": "tunnel",
                   "settings": {"allowedNetwork": "tcp",
                                "rewriteAddress": "127.0.0.1", "rewritePort": 22}}
        old = {"log": {"loglevel": "warning"},
               "inbounds": [legacy, wss, old_admin, old_ssh],
               "outbounds": [{"tag": "direct", "protocol": "freedom"}],
               "routing": {"rules": [
                   {"type": "field", "inboundTag": ["entry-old"],
                    "outboundTag": "reverse-old"},
                   {"type": "field", "inboundTag": ["ssh-old"],
                    "outboundTag": "reverse-old-ssh"}]},
               "remarks": "OpenWrt Remote Hub server config"}
        import copy
        new = copy.deepcopy(old)
        new["inbounds"][1]["settings"]["clients"].extend([
            {"id": "new-admin", "email": "new@owrt-remote",
             "reverse": {"tag": "reverse-new"}},
            {"id": "new-ssh", "email": "new-ssh@owrt-remote",
             "reverse": {"tag": "reverse-new-ssh"}}])
        new["inbounds"].extend([
            {"tag": "entry-new", "listen": "127.0.0.1", "port": 18130,
             "protocol": "tunnel",
             "settings": {"allowedNetwork": "tcp",
                          "rewriteAddress": "127.0.0.1", "rewritePort": 80}},
            {"tag": "ssh-new", "listen": "127.0.0.1", "port": 19130,
             "protocol": "tunnel",
             "settings": {"allowedNetwork": "tcp",
                          "rewriteAddress": "127.0.0.1", "rewritePort": 22}}])
        new["routing"]["rules"].extend([
            {"type": "field", "inboundTag": ["entry-new"],
             "outboundTag": "reverse-new"},
            {"type": "field", "inboundTag": ["ssh-new"],
             "outboundTag": "reverse-new-ssh"}])
        return old, new

    def test_wss_addition_preserves_existing_identities(self):
        spec2 = importlib.util.spec_from_file_location("host_control", REPO / "docker" / "host_adapter.py")
        host = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(host)
        old, new = self.sample_new_wss_config()
        host.validate_transition(old, new)
        # A generated config may insert a new router between existing ones.
        new["routing"]["rules"].insert(0, new["routing"]["rules"].pop())
        host.validate_transition(old, new)

    def test_wss_mutations_and_partial_enrollment_are_blocked(self):
        import copy
        spec2 = importlib.util.spec_from_file_location("host_control", REPO / "docker" / "host_adapter.py")
        host = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(host)
        old, expected = self.sample_new_wss_config()
        for case in ("delete-old", "change-uuid", "change-reverse", "add-tcp",
                     "remove-ssh", "reroute-old", "change-dns", "change-outbound",
                     "duplicate-port", "public-listener", "duplicate-client",
                     "extra-client", "rule-reorder"):
            candidate = copy.deepcopy(expected)
            clients = candidate["inbounds"][1]["settings"]["clients"]
            if case == "delete-old":
                clients.pop(0)
            elif case == "change-uuid":
                clients[0]["id"] = "spoofed"
            elif case == "change-reverse":
                clients[-1]["reverse"]["tag"] = "wrong"
            elif case == "add-tcp":
                candidate["inbounds"][0]["settings"]["clients"].append(clients[-1])
            elif case == "remove-ssh":
                candidate["inbounds"] = [x for x in candidate["inbounds"]
                                         if x["tag"] != "ssh-new"]
            elif case == "reroute-old":
                candidate["routing"]["rules"][0]["outboundTag"] = "wrong"
            elif case == "change-dns":
                candidate["dns"] = {"servers": ["8.8.8.8"]}
            elif case == "change-outbound":
                candidate["outbounds"][0]["protocol"] = "blackhole"
            elif case == "duplicate-port":
                candidate["inbounds"][-1]["port"] = 18090
            elif case == "public-listener":
                candidate["inbounds"][-1]["listen"] = "0.0.0.0"
            elif case == "duplicate-client":
                clients[-1]["id"] = clients[0]["id"]
            elif case == "extra-client":
                clients.append({"id": "rogue", "email": "other@owrt-remote"})
            elif case == "rule-reorder":
                candidate["routing"]["rules"][:2] = list(reversed(candidate["routing"]["rules"][:2]))
            with self.subTest(case=case), self.assertRaises(host.Rejected):
                host.validate_transition(old, candidate)

    def test_adapter_never_accepts_shell(self):
        adapter_src = (REPO / "docker" / "host_adapter.py").read_text()
        self.assertNotIn("shell=True", adapter_src)
        self.assertIn("unsupported operation", adapter_src)
        self.assertIn("SO_PEERCRED", adapter_src)


    def test_host_adapter_requires_explicit_mutation(self):
        spec2 = importlib.util.spec_from_file_location("host_control", REPO / "docker" / "host_adapter.py")
        host = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(host)
        adapter = host.Adapter()
        with self.assertRaises(host.Rejected):
            adapter.handle({"op": "xray.restart"})
        with self.assertRaises(host.Rejected):
            adapter.handle({"op": "shell.exec", "command": "id"})

    def test_host_adapter_rejects_xray_transport_changes(self):
        spec2 = importlib.util.spec_from_file_location("host_control", REPO / "docker" / "host_adapter.py")
        host = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(host)
        old = {"inbounds": [
          {"tag":"owrt-remote-vless", "protocol":"vless", "port":18443, "listen":"0.0.0.0", "streamSettings":{}},
          {"tag":"owrt-remote-wss", "protocol":"vless", "port":18444, "listen":"127.0.0.1", "streamSettings":{}},
        ], "routing":{"rules":[]}}
        candidate = json.loads(json.dumps(old))
        candidate["inbounds"][1]["port"] = 18445
        with self.assertRaises(host.Rejected):
            host.validate_transition(old, candidate)

if __name__ == "__main__":
    unittest.main()
