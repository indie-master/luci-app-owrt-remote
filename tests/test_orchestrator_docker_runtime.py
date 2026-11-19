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

if __name__ == "__main__":
    unittest.main()
