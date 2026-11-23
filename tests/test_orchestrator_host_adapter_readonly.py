"""Offline, unprivileged contract tests for the root-side read-only adapter."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "host_adapter_readonly_tests", ROOT / "docker/host_adapter.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class ReadonlyHostAdapterTests(unittest.TestCase):
    def exchange(self, adapter, request):
        """Exercise actual SO_PEERCRED over a Linux AF_UNIX socket pair."""
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with client, server:
            client.sendall(json.dumps(request).encode() + b"\n")
            self.assertTrue(adapter.serve_peer(server))
            data = client.recv(65536)
            self.assertTrue(data.endswith(b"\n"))
            return json.loads(data)

    def test_peer_uid_allowed_for_health_and_host_metrics(self):
        adapter = host.Adapter(uid=os.geteuid())
        health = self.exchange(adapter, {"op": "health"})
        self.assertEqual(health, {"ok": True, "adapter": "host-control-v1"})
        with patch.object(adapter, "metrics", return_value={
            "ok": True, "scope": "host-adapter"}):
            metric = self.exchange(adapter, {"op": "host.metrics"})
            self.assertEqual(metric["scope"], "host-adapter")

    def test_peer_uid_rejected_before_json_is_read(self):
        adapter = host.Adapter(uid=os.geteuid() + 1)
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with client, server:
            client.sendall(b'{"op":"health"}\n')
            with patch.object(adapter, "handle") as handler:
                self.assertFalse(adapter.serve_peer(server))
                handler.assert_not_called()

    def test_status_readonly_and_config_checksum(self):
        adapter = host.Adapter(uid=os.geteuid())
        with tempfile.TemporaryDirectory() as td:
            config = Path(td) / "owrt.json"
            config.write_text('{"inbounds":[],"routing":{"rules":[]}}\n')
            with patch.object(host, "CONFIG", config), \
                 patch.object(host, "active_xray", return_value=True):
                status = self.exchange(adapter, {"op": "xray.status"})
                current = self.exchange(adapter, {"op": "xray.current"})
            self.assertTrue(status["active"])
            self.assertEqual(current["sha256"], host.sha(config.read_bytes()))
            self.assertEqual(current["config"]["inbounds"], [])

    def test_write_capabilities_rejected_without_touching_host(self):
        adapter = host.Adapter(uid=os.geteuid())
        with patch.object(host, "systemctl") as service, \
             patch.object(host, "atomic_replace") as files:
            restart = self.exchange(adapter, {"op": "xray.restart"})
            apply = self.exchange(adapter, {"op": "xray.apply",
                                            "expected_sha256": "0",
                                            "config": {}})
            self.assertFalse(restart["ok"])
            self.assertFalse(apply["ok"])
            self.assertIn("not approved", restart["error"])
            self.assertIn("not approved", apply["error"])
            service.assert_not_called()
            files.assert_not_called()

    def test_unknown_operation_rejected(self):
        adapter = host.Adapter(uid=os.geteuid())
        response = self.exchange(adapter, {"op": "shell.exec", "command": "id"})
        self.assertFalse(response["ok"])
        self.assertIn("unsupported operation", response["error"])

    def test_empty_and_oversized_requests_are_bounded(self):
        adapter = host.Adapter(uid=os.geteuid())
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with client, server:
            client.sendall(b"\n")
            self.assertTrue(adapter.serve_peer(server))
            self.assertFalse(json.loads(client.recv(4096))["ok"])
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with client, server:
            # The request is larger than the socket buffer; consume it from
            # another thread to avoid deadlocking the test itself.
            results = []
            thread = threading.Thread(
                target=lambda: results.append(adapter.serve_peer(server)))
            thread.start()
            client.sendall(b"x" * (host.MAX_REQUEST + 10) + b"\n")
            self.assertFalse(json.loads(client.recv(4096))["ok"])
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [True])

    def test_crash_left_socket_can_be_recovered(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "control.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
            self.assertTrue(path.exists())
            self.assertTrue(host.clear_stale_socket(path))
            self.assertFalse(path.exists())
            self.assertFalse(host.clear_stale_socket(path))

    def test_active_socket_is_never_unlinked(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "control.sock"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(path))
                listener.listen(1)
                with self.assertRaisesRegex(host.Rejected, "already listening"):
                    host.clear_stale_socket(path)
                self.assertTrue(path.exists())

    def test_symlink_never_unlinked(self):
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            target = folder / "protected"
            target.write_text("do not touch")
            link = folder / "control.sock"
            link.symlink_to(target)
            with self.assertRaisesRegex(host.Rejected, "not an owned UNIX socket"):
                host.clear_stale_socket(link)
            self.assertEqual(target.read_text(), "do not touch")

    def test_privileged_daemon_executes_from_pinned_path(self):
        unit = (ROOT / "docker/host_adapter.service").read_text()
        self.assertIn("/opt/router-orchestrator/host-adapter/host_adapter.py", unit)
        self.assertNotIn("/opt/router-orchestrator/release/docker/host_adapter.py", unit)

    def test_unit_remains_readonly_without_host_xray_write_access(self):
        text = (ROOT / "docker/host_adapter.service").read_text()
        self.assertIn("ProtectSystem=strict", text)
        self.assertIn("ReadWritePaths=/run/router-orchestrator", text)
        self.assertNotIn("ReadWritePaths=/etc/xray", text)
        self.assertNotIn("--allow-xray-apply", text)
        self.assertNotIn("--allow-xray-restart", text)
        self.assertIn("NoNewPrivileges=true", text)
        self.assertIn("CapabilityBoundingSet=CAP_CHOWN CAP_FOWNER", text)

    def test_socket_path_cannot_be_configured_via_service(self):
        unit = (ROOT / "docker/host_adapter.service").read_text()
        self.assertIn("--peer-uid 23088", unit)
        self.assertNotIn("--socket", unit)
        with patch.object(host, "SOCKET", Path("/run/router-orchestrator/control.sock")):
            source = (ROOT / "docker/host_adapter.py").read_text()
            self.assertIn("custom socket paths are forbidden", source)


if __name__ == "__main__":
    unittest.main()
