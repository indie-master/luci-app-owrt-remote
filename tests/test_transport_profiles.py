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
                         {"tcp", "wss", "reality-tcp", "xhttp", "grpc"})

    def test_legacy_tcp(self):
        self.assertEqual(render_stream_settings({"transport": "tcp"}),
                         {"network": "tcp", "security": "none"})

    def test_tls_transports(self):
        for transport, network in (("wss", "ws"), ("xhttp", "xhttp"),
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

    def test_no_mutation_of_input(self):
        data = tls_profile("grpc")
        before = dict(data)
        render_stream_settings(data)
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
