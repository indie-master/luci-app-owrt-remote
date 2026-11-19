"""Runtime safety checks with no Docker daemon or VPS required."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docker_manager", REPO / "docker" / "manager.py")
mgr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mgr)

class DockerRuntimeContracts(unittest.TestCase):
    def test_compose_requires_explicit_readonly_xray(self):
        compose = (REPO / "docker-compose.yml").read_text()
        self.assertIn("network_mode: host", compose)
        self.assertIn("127.0.0.1", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("OWRT_REMOTE_RUNTIME: docker", compose)
        self.assertNotIn("docker.sock", compose)

    def test_source_disables_systemd_update_and_host_terminal(self):
        source = (REPO / "vps" / "owrt-remote-hub.py").read_text()
        self.assertIn("if DOCKER_RUNTIME:", source)
        self.assertIn("Host VPS terminal adapter required", source)
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
