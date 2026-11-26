import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_certificate_inventory import inspect_pair, find_certificate, mounted_certificate
from transport_profiles import ProfileError
from unified_transport_catalog import tls_binding


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL required")
class CertificateInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cert = self.root / "cert.pem"
        self.key = self.root / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-nodes", "-days", "3", "-subj", "/CN=router.example.org",
                        "-addext", "subjectAltName=DNS:router.example.org,DNS:*.lab.example.org",
                        "-keyout", str(self.key), "-out", str(self.cert)],
                       check=True, capture_output=True, timeout=15)

    def test_exact_and_wildcard_and_private_key_match(self):
        for name in ("router.example.org", "node.lab.example.org"):
            result = find_certificate(name, [(self.cert, self.key)], roots=[self.root])
            self.assertTrue(result["valid"])
            self.assertNotIn("private_key", result)
        for name in ("other.example.org", "deep.node.lab.example.org"):
            with self.assertRaises(ProfileError):
                inspect_pair(name, self.cert, self.key, roots=[self.root])

    def test_mounted_inventory_auto_selection(self):
        selected = mounted_certificate("node.lab.example.org", root=self.root)
        self.assertEqual(selected["certificate_file"], "/etc/router-orchestrator/tls/cert.pem")
        self.assertEqual(selected["key_file"], "/etc/router-orchestrator/tls/key.pem")
        with self.assertRaises(ProfileError):
            mounted_certificate("uncovered.example.org", root=self.root)

    def test_stage_tls_binding_checks_the_exact_mounted_pair(self):
        name = "node.lab.example.org"
        stack = SimpleNamespace(config={"RO_TLS_DIR": str(self.root)})
        profile = dict(mounted_certificate(name, root=self.root), transport="grpc", server_name=name)
        original = tls_binding(stack, profile)
        with self.cert.open("ab") as stream:
            stream.write(b"\n")
        renewed = tls_binding(stack, profile)
        self.assertNotEqual(original["certificate_file"], renewed["certificate_file"])
        self.assertEqual(original["key_file"], renewed["key_file"])
        with self.assertRaises(ProfileError):
            tls_binding(stack, dict(profile, server_name="uncovered.example.org"))

    def test_expiry_root_and_mismatched_key(self):
        with self.assertRaises(ProfileError):
            inspect_pair("router.example.org", self.cert, self.key, roots=[self.root],
                         minimum_seconds=86400*7)
        outside = self.root / "other"
        outside.mkdir()
        with self.assertRaises(ProfileError):
            inspect_pair("router.example.org", self.cert, self.key, roots=[outside])
        other = self.root / "other.key"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-out", str(other)],
                       check=True, capture_output=True, timeout=15)
        with self.assertRaises(ProfileError):
            inspect_pair("router.example.org", self.cert, other, roots=[self.root])


if __name__ == "__main__":
    unittest.main()
