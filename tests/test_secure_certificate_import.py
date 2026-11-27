import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_certificate_import import import_certificate, nginx_pairs
from transport_profiles import ProfileError


class ParseTests(unittest.TestCase):
    def test_literal_pairs_only(self):
        raw = b"""# configuration file /etc/nginx/nginx.conf:
server {
    ssl_certificate /etc/nginx/fullchain.pem;
    ssl_certificate_key /etc/nginx/key.pem;
}
server {
    ssl_certificate /etc/nginx/$ssl_server_name.pem;
    ssl_certificate_key /etc/nginx/ignored.pem;
}
"""
        self.assertEqual(nginx_pairs(raw),[(Path("/etc/nginx/fullchain.pem"),Path("/etc/nginx/key.pem"))])


@unittest.skipUnless(shutil.which("openssl"),"OpenSSL required")
class ImportTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.source=self.root/"source"
        self.dest=self.root/"tls"
        self.source.mkdir(mode=0o700)
        self.dest.mkdir(mode=0o700)
        self.cert=self.source/"fullchain.pem"
        self.key=self.source/"privkey.pem"
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes",
            "-days","3","-subj","/CN=router.example.org",
            "-addext","subjectAltName=DNS:*.example.org",
            "-keyout",str(self.key),"-out",str(self.cert)],check=True,capture_output=True)
        self.key.chmod(0o600)
        # The real trust-chain checks run in production. CI may be an
        # unprivileged runner whose /tmp parent belongs to root.
        self.reader=patch("secure_certificate_import.read_pem",side_effect=self.fixture_read)
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def fixture_read(self,path,roots):
        path=Path(path).resolve(strict=True)
        if not any(path.is_relative_to(Path(r).resolve()) for r in roots):
            raise ProfileError("source outside roots")
        return path.read_bytes()

    def do_import(self,name="router.example.org"):
        return import_certificate(name,root=self.dest,pairs=[(self.cert,self.key)],roots=[self.source])

    def test_import_is_idempotent_and_keeps_key_private(self):
        result=self.do_import()
        self.assertEqual(result,self.do_import())
        self.assertFalse(result["applied"])
        entries=list(self.dest.iterdir())
        self.assertEqual(len(entries),1)
        self.assertEqual((entries[0]/"privkey.pem").stat().st_mode & 0o777,0o600)
        self.assertEqual((entries[0]/"privkey.pem").read_bytes(),self.key.read_bytes())
        self.assertNotIn("PRIVATE KEY",str(result))

    def test_wrong_name_does_not_publish_slot(self):
        with self.assertRaises(ProfileError):
            self.do_import("deep.router.example.org")
        self.assertEqual(list(self.dest.iterdir()),[])


if __name__=="__main__":
    unittest.main()
