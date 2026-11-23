"""Read-only Belt acceptance tool tests: no real host changes."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import orchestrator_host_adapter_readonly_acceptance as check


class HostAdapterAcceptanceTests(unittest.TestCase):
    def test_preflight_is_readonly_and_reports_service_state(self):
        with tempfile.TemporaryDirectory() as td:
            file = Path(td) / "xray.json"
            file.write_text("{}")
            output = io.StringIO()
            with patch.object(check.os, "geteuid", return_value=0), \
                 patch.object(check, "check_checkout") as checkout, \
                 patch.object(check, "check_source_service") as verify_unit, \
                 patch.object(check, "check_systemd") as status, \
                 patch.object(check, "XRAY", file), \
                 contextlib.redirect_stdout(output):
                check.preflight("f" * 40)
                checkout.assert_called_once()
                verify_unit.assert_called_once()
                self.assertEqual(status.call_count, 2)
            self.assertIn("NO CHANGE", output.getvalue())

    def test_read_operations_allowlist_excludes_all_writes(self):
        self.assertEqual(check.OPS,
                         ("health", "xray.status", "host.metrics", "xray.current"))
        with self.assertRaisesRegex(check.Blocked, "unsupported read-only"):
            check.read_one("xray.apply")
        with self.assertRaisesRegex(check.Blocked, "unsupported read-only"):
            check.read_one("xray.restart")
        with self.assertRaisesRegex(check.Blocked, "unsupported read-only"):
            check.read_one("shell.exec")

    def test_peer_summaries_never_reveal_raw_config(self):
        payloads = {
            "health": {"ok": True, "adapter": "host-control-v1"},
            "xray.status": {"ok": True, "active": True,
                            "service": "owrt-remote-xray"},
            "host.metrics": {"ok": True, "scope": "host-adapter",
                             "memory": {"total_bytes": 1024}},
            "xray.current": {"ok": True, "sha256": "f" * 64,
                             "config": {"uuid": "secret-uuid-not-for-stdout"}},
        }
        seen = []
        def fake(op):
            seen.append(op)
            return payloads[op]
        with patch.object(check, "read_one", side_effect=fake):
            result = check.peer_summaries()
        self.assertEqual(tuple(seen), check.OPS)
        self.assertEqual(result["xray_sha256"], "f" * 64)
        self.assertNotIn("secret", json.dumps(result))

    def test_peer_requires_live_xray(self):
        payloads = {
            "health": {"ok": True, "adapter": "host-control-v1"},
            "xray.status": {"ok": True, "active": False,
                            "service": "owrt-remote-xray"}
        }
        with patch.object(check, "read_one", side_effect=lambda op: payloads[op]):
            with self.assertRaisesRegex(check.Blocked, "not healthy"):
                check.peer_summaries()

    def test_probe_reports_only_status_and_sha_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            file = root / "xray.json"
            unit = root / "adapter.service"
            pinned = root / "host_adapter.py"
            sock_path = root / "control.sock"
            file.write_text('{"secret": "not for stdout"}')
            unit.write_text("unit")
            pinned.write_text("code")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(sock_path))
                fake_show = type("Completed", (), {
                    "returncode": 0,
                    "stdout": "DropInPaths=\nFragmentPath=" + str(unit) + "\n",
                })()
                output = io.StringIO()
                with patch.object(check, "preflight"), \
                     patch.object(check, "check_systemd"), \
                     patch.object(check, "run_command", return_value=fake_show), \
                     patch.object(check, "UNIT", unit), \
                     patch.object(check, "PINNED", pinned), \
                     patch.object(check, "XRAY", file), \
                     patch.object(check, "SOCKET", sock_path), \
                     patch.object(check, "root_peer_rejected", return_value=True), \
                     patch.object(check, "restricted_probe",
                                  return_value={"xray_sha256": check.sha256(file),
                                                "metrics": "PASS"}), \
                     contextlib.redirect_stdout(output):
                    check.probe("f" * 40)
            self.assertIn("READONLY PROBE: PASS", output.getvalue())
            self.assertNotIn("not for stdout", output.getvalue())

    def test_probe_blocks_unexpected_xray_sha(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            file = root / "xray.json"
            unit = root / "adapter.service"
            pinned = root / "host_adapter.py"
            sock_path = root / "control.sock"
            file.write_text("{}")
            unit.write_text("unit")
            pinned.write_text("code")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(sock_path))
                fake_show = type("Completed", (), {
                    "returncode": 0,
                    "stdout": "DropInPaths=\nFragmentPath=" + str(unit) + "\n",
                })()
                with patch.object(check, "preflight"), \
                     patch.object(check, "check_systemd"), \
                     patch.object(check, "run_command", return_value=fake_show), \
                     patch.object(check, "UNIT", unit), \
                     patch.object(check, "PINNED", pinned), \
                     patch.object(check, "XRAY", file), \
                     patch.object(check, "SOCKET", sock_path), \
                     patch.object(check, "root_peer_rejected", return_value=True), \
                     patch.object(check, "restricted_probe",
                                  return_value={"xray_sha256": "0"*64}):
                    with self.assertRaisesRegex(check.Blocked, "differs"):
                        check.probe("f"*40)

    def test_source_unit_is_reviewed_without_privileged_exec_flags(self):
        with patch.object(check, "UNIT", Path("/tmp/nonexistent-unit")):
            check.check_source_service()


if __name__ == "__main__":
    unittest.main()
