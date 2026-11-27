"""Offline ingress planning checks: no live nginx or certificates."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_ingress import plan_ingress, render_map_entries
from transport_profiles import ProfileError


def profile(transport="reality-xhttp"):
    return {
        "transport": transport, "name": "management", "port": 18550,
        "server_name": "remote.example.org",
        "server_names": ["remote.example.org", "second.example.org"],
        "target_host": "remote.example.org", "target_port": 443,
        "private_key": "test-key", "public_key": "A" * 43, "short_ids": ["ab12"],
        "path": "/reverse/", "service_name": "reverse",
        "certificate_file": "/etc/router-orchestrator/tls/fullchain.pem",
        "key_file": "/etc/router-orchestrator/tls/privkey.pem",
    }


class SecureIngressTests(unittest.TestCase):
    def test_all_secure_profiles_keep_encryption_at_xray(self):
        for name in ("tcp-tls", "wss", "xhttp", "grpc",
                     "reality-tcp", "reality-xhttp", "reality-grpc"):
            with self.subTest(name=name):
                plan = plan_ingress(profile(name))
                self.assertEqual(plan["tls_termination"], "xray")
                self.assertEqual(plan["listen"], "127.0.0.1")
                self.assertEqual(plan["requires_certificate"], not name.startswith("reality-"))
                self.assertFalse(plan["applied"])
                self.assertIn("remote.example.org 127.0.0.1:18550;", render_map_entries(plan))

    def test_collision_rejected_not_overwritten(self):
        for name in ("remote.example.org", "SECOND.example.org"):
            with self.assertRaises(ProfileError):
                plan_ingress(profile(), existing_routes=[{"public_port": 443, "sni": name}])
        with self.assertRaises(ProfileError):
            plan_ingress(profile(), occupied_ports=[18550])

    def test_invalid_names_and_plaintext_rejected(self):
        for name in ("tcp", "unknown"):
            with self.assertRaises(ProfileError):
                plan_ingress(profile(name))
        with self.assertRaises(ProfileError):
            plan_ingress(dict(profile(), server_name="a.example.org;include /etc/passwd"))
        with self.assertRaises(ProfileError):
            plan_ingress(dict(profile(), port=18444))


if __name__ == "__main__":
    unittest.main()
