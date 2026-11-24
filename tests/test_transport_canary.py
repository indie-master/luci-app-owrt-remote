"""Offline tests: staged reverse tunnels never modify existing ports/identities."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker"))
from transport_canary import build_shadow_canary, assert_baseline_retained
from transport_profiles import ProfileError
import host_adapter as adapter_module


class CanaryContract(unittest.TestCase):
    def setUp(self):
        self.original = {
            "inbounds": [
                {"tag": "owrt-remote-vless", "listen": "0.0.0.0",
                 "port": 18443, "protocol": "vless",
                 "settings": {"decryption": "none", "clients": []}},
                {"tag": "entry-main", "listen": "127.0.0.1",
                 "port": 18080, "protocol": "tunnel",
                 "settings": {"allowedNetwork": "tcp",
                              "rewriteAddress": "127.0.0.1", "rewritePort": 80}}],
            "routing": {"rules": [{"type": "field",
                                   "inboundTag": ["entry-main"],
                                   "outboundTag": "reverse-main"}]}
        }
        self.profile = {"transport": "tcp", "name": "pilot01", "port": 18446}
        self.router = {
            "id": "main", "vless_uuid": "550e8400-e29b-41d4-a716-446655440000",
            "ssh_vless_uuid": "550e8400-e29b-41d4-a716-446655440001",
            "admin_host": "127.0.0.1", "ssh_host": "127.0.0.1",
            "admin_port": 80, "ssh_port": 22
        }

    def test_manual_rollback_unit_is_fixed_and_never_user_supplied(self):
        source = (ROOT / "docker" / "host_adapter.py").read_text()
        unit = (ROOT / "deploy" / "systemd" /
                "router-orchestrator-transport-rollback.service").read_text()
        apply_script = (ROOT / "deploy" / "apply_belt.py").read_text()
        hub = (ROOT / "vps" / "owrt-remote-hub.py").read_text()
        self.assertIn('router-orchestrator-transport-rollback.service', source)
        self.assertIn('transport_stage_rollback', source)
        self.assertIn('transport_stage_status', source)
        self.assertIn('transport_stage.py rollback', unit)
        self.assertIn('TRANSPORT_ROLLBACK_UNIT', apply_script)
        self.assertIn('X-Transport-Action-CSRF', hub)
        self.assertIn('/api/transports/stage/rollback', hub)

    def test_shadow_rollback_requires_completed_stage_and_confirmation(self):
        import json
        import tempfile
        from unittest.mock import patch
        adapter = adapter_module.Adapter(allow_apply=True)
        with patch.object(adapter, "can_mutate") as mutate, \\
             patch.object(adapter, "transport_stage_status",
                          return_value={"phase": "STAGING", "router_id": "main"}), \\
             patch.object(adapter_module.subprocess, "run") as launch:
            with self.assertRaises(adapter_module.Rejected):
                adapter.transport_stage_rollback({"confirmed": True})
            launch.assert_not_called()
        with patch.object(adapter, "can_mutate") as mutate, \\
             patch.object(adapter, "transport_stage_status",
                          return_value={"phase": "STAGED", "router_id": "main"}), \\
             patch.object(adapter_module.subprocess, "run") as launch:
            launch.return_value.returncode = 0
            with self.assertRaises(adapter_module.Rejected):
                adapter.transport_stage_rollback({"confirmed": False})
            launch.assert_not_called()
            result = adapter.transport_stage_rollback({"confirmed": True})
            self.assertEqual(result["status"], "rollback_requested")
            self.assertEqual(launch.call_args.args[0],
                             ["systemctl", "start", "--no-block",
                              "router-orchestrator-transport-rollback.service"])

    def test_server_addition_keeps_existing_bytes(self):
        before = repr(self.original)
        candidate, pilot = build_shadow_canary(
            self.original, self.profile, self.router,
            admin_test_port=21080, ssh_test_port=22080
        )
        self.assertEqual(repr(self.original), before)
        assert_baseline_retained(self.original, candidate)
        self.assertEqual(len(candidate["inbounds"]), len(self.original["inbounds"]) + 3)
        self.assertEqual(len(candidate["routing"]["rules"]),
                         len(self.original["routing"]["rules"]) + 2)
        self.assertEqual(pilot["status"], "STAGED_ONLY")
        self.assertEqual(pilot["vless_uuid"], self.router["vless_uuid"])
        self.assertNotEqual(pilot["reverse_tag"], "reverse-main")

    def test_duplicate_port_fails(self):
        for conflict in (18443, 18080, 443, 18445):
            with self.subTest(conflict=conflict):
                self.profile["port"] = conflict
                with self.assertRaises(ProfileError):
                    build_shadow_canary(self.original, self.profile, self.router,
                                        admin_test_port=21080,
                                        ssh_test_port=22080)

    def test_same_admin_ssh_uuid_fails(self):
        self.router["ssh_vless_uuid"] = self.router["vless_uuid"]
        with self.assertRaises(ProfileError):
            build_shadow_canary(self.original, self.profile, self.router,
                                admin_test_port=21080, ssh_test_port=22080)

    def test_private_identity_retained_on_invalid_candidate(self):
        before = repr(self.original)
        with self.assertRaises(ProfileError):
            build_shadow_canary(self.original, self.profile, self.router,
                                admin_test_port=18443, ssh_test_port=18443)
        self.assertEqual(repr(self.original), before)

    def test_explicit_unsafe_bind_rejected(self):
        with self.assertRaises(ProfileError):
            build_shadow_canary(self.original, self.profile, self.router,
                                admin_test_port=21080, ssh_test_port=22080,
                                listen="8.8.8.8")

    def test_mutated_existing_port_detected(self):
        candidate, _ = build_shadow_canary(
            self.original, self.profile, self.router,
            admin_test_port=21080, ssh_test_port=22080
        )
        candidate["inbounds"][0]["port"] = 9999
        with self.assertRaises(ProfileError):
            assert_baseline_retained(self.original, candidate)


if __name__ == "__main__":
    unittest.main()
