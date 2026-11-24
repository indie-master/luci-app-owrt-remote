"""Offline tests: no Xray process, Docker daemon or production sockets."""
import unittest

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

module_path = Path(__file__).resolve().parents[1] / "docker" / "transport_profiles.py"
spec = spec_from_file_location("transport_profiles", module_path)
module = module_from_spec(spec)
spec.loader.exec_module(module)
ProfileError = module.ProfileError
SUPPORTED = module.SUPPORTED
render_stream_settings = module.render_stream_settings
validate_profiles = module.validate_profiles
render_router_outbound = module.render_router_outbound


def tls_profile(transport):
    data = {
        "transport": transport,
        "server_name": "router.example.org",
        "certificate_file": "/etc/router-orchestrator/tls/fullchain.pem",
        "key_file": "/etc/router-orchestrator/tls/privkey.pem",
    }
    if transport in ("wss", "xhttp"):
        data["path"] = "/owrt-remote"
    if transport == "grpc":
        data["service_name"] = "owrt_remote"
    return data


class TransportProfilesTest(unittest.TestCase):
    def test_supported(self):
        self.assertEqual(set(SUPPORTED),
                         {"tcp", "tcp-tls", "wss", "reality-tcp", "xhttp", "grpc", "reality-xhttp", "reality-grpc"})

    def test_legacy_tcp(self):
        self.assertEqual(render_stream_settings({"transport": "tcp"}),
                         {"network": "tcp", "security": "none"})

    def test_tls_transports(self):
        for transport, network in (("tcp-tls", "tcp"), ("wss", "ws"), ("xhttp", "xhttp"),
                                   ("grpc", "grpc")):
            with self.subTest(transport=transport):
                actual = render_stream_settings(tls_profile(transport))
                self.assertEqual(actual["network"], network)
                self.assertEqual(actual["security"], "tls")

    def test_reality_tcp(self):
        settings = render_stream_settings({
            "transport": "reality-tcp",
            "server_name": "www.example.org",
            "server_names": ["www.example.org"],
            "target_host": "www.example.org",
            "target_port": 443,
            "private_key": "example-test-only-not-a-real-key",
            "short_ids": ["aabb"],
        })
        self.assertEqual(settings["security"], "reality")
        self.assertEqual(settings["network"], "tcp")

    def test_new_profiles_never_allow_plaintext(self):
        self.assertEqual(set(module.SECURE_PROFILES), {"tcp-tls", "wss", "reality-tcp", "xhttp", "grpc", "reality-xhttp", "reality-grpc"})
        for name in module.SECURE_PROFILES:
            module.require_secure_profile({"transport": name})
        for name in ("tcp", "none", "unknown"):
            with self.assertRaises(ProfileError):
                module.require_secure_profile({"transport": name})

    def test_tcp_tls_and_wss_validate_certificate(self):
        for name in ("tcp-tls", "wss"):
            p = dict(tls_profile(name), name="candidate", port=18550)
            outbound = render_router_outbound(p, router_uuid="550e8400-e29b-41d4-a716-446655440000",
                                              remote_host="router.example.org")
            stream = outbound["streamSettings"]
            self.assertEqual(stream["security"], "tls")
            self.assertIs(stream["tlsSettings"]["allowInsecure"], False)
            self.assertEqual(stream["tlsSettings"]["serverName"], "router.example.org")
            if name == "wss":
                self.assertEqual(stream["tlsSettings"]["alpn"], ["http/1.1"])
                self.assertEqual(stream["wsSettings"]["host"], "router.example.org")
        with self.assertRaises(ProfileError):
            render_stream_settings({"transport": "tcp-tls"})

    def test_reality_http_transports_and_multi_sni(self):
        for transport, network, field in (
            ("reality-xhttp", "xhttp", "xhttpSettings"),
            ("reality-grpc", "grpc", "grpcSettings"),
        ):
            p = {"name": "candidate", "port": 18550, "transport": transport,
                 "server_name": "www.example.org",
                 "server_names": ["www.example.org", "other.example.org"],
                 "target_host": "www.example.org", "target_port": 443,
                 "private_key": "PRIVATE_TEST_VALUE_NEVER_IN_CLIENT",
                 "public_key": "A" * 43, "short_ids": ["aabb"],
                 "path": "/reverse/", "service_name": "reverse"}
            server = render_stream_settings(p)
            self.assertEqual(server["network"], network)
            self.assertEqual(server["security"], "reality")
            self.assertEqual(server["realitySettings"]["serverNames"], p["server_names"])
            client = render_router_outbound(p, router_uuid="550e8400-e29b-41d4-a716-446655440000",
                                            remote_host="router.example.org")
            stream = client["streamSettings"]
            self.assertEqual(stream["network"], network)
            self.assertEqual(stream["security"], "reality")
            self.assertEqual(stream[field], server[field])
            self.assertNotIn("PRIVATE_TEST_VALUE_NEVER_IN_CLIENT", str(client))
            self.assertNotIn("tlsSettings", stream)
            with self.assertRaises(ProfileError):
                render_stream_settings(dict(p, server_names=["other.example.org"]))

    def test_reject_unknown(self):
        with self.assertRaises(ProfileError):
            render_stream_settings({"transport": "httpupgrade"})

    def test_reject_bad_tls_path(self):
        data = tls_profile("wss")
        data["key_file"] = "/etc/shadow"
        with self.assertRaises(ProfileError):
            render_stream_settings(data)

    def test_reject_bad_ws_path(self):
        data = tls_profile("wss")
        data["path"] = "relative"
        with self.assertRaises(ProfileError):
            render_stream_settings(data)

    def test_reject_duplicate_ports(self):
        one = {"name": "first", "port": 18443, "transport": "tcp"}
        two = {"name": "second", "port": 18443, "transport": "tcp"}
        with self.assertRaises(ProfileError):
            validate_profiles([one, two])

    def test_client_outbound_for_each_transport(self):
        uid = "550e8400-e29b-41d4-a716-446655440000"
        profiles = [{"transport": "tcp"}]
        for transport in ("tcp-tls", "wss", "xhttp", "grpc"):
            profiles.append(tls_profile(transport))
        reality = {
            "transport": "reality-tcp",
            "server_name": "www.example.org",
            "server_names": ["www.example.org"],
            "target_host": "www.example.org", "target_port": 443,
            "private_key": "PRIVATE_TEST_VALUE_NEVER_IN_CLIENT",
            "public_key": "A" * 43,
            "short_ids": ["aabb"]
        }
        profiles.append(reality)
        for i, item in enumerate(profiles):
            with self.subTest(transport=item["transport"]):
                item["name"] = "test" + str(i)
                item["port"] = 18450 + i
                outbound = render_router_outbound(
                    item, router_uuid=uid, remote_host="router.example.org"
                )
                self.assertEqual(outbound["settings"]["id"], uid)
                self.assertEqual(outbound["settings"]["port"], 18450 + i)
                self.assertEqual(outbound["settings"]["reverse"]["tag"], "reverse-in")
                self.assertNotIn("PRIVATE_TEST_VALUE_NEVER_IN_CLIENT", str(outbound))

    def test_client_rejects_malformed_identity(self):
        profile = {"name": "probe", "port": 18455, "transport": "tcp"}
        with self.assertRaises(ProfileError):
            render_router_outbound(
                profile, router_uuid="not-a-uuid", remote_host="router.example.org"
            )

    def test_no_mutation_of_input(self):
        data = tls_profile("grpc")
        before = dict(data)
        render_stream_settings(data)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
