import importlib.util
from pathlib import Path
import ssl
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("rehearse", ROOT / "tools/rehearse_secure_reverse.py")
rehearse = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rehearse)
from transport_profiles import ProfileError


class TargetTests(unittest.TestCase):
    def test_requires_verified_tls13_h2(self):
        with patch.object(rehearse.ssl,"create_default_context") as factory, \
             patch.object(rehearse.socket,"create_connection") as connect:
            ctx=factory.return_value
            tls=ctx.wrap_socket.return_value.__enter__.return_value
            tls.version.return_value="TLSv1.3"
            tls.selected_alpn_protocol.return_value="h2"
            result=rehearse.check_reality_target("target.example.org","sni.example.org")
            self.assertEqual(result["tls"],"TLSv1.3")
            self.assertEqual(ctx.minimum_version,ssl.TLSVersion.TLSv1_3)
            ctx.set_alpn_protocols.assert_called_once_with(["h2"])
            connect.assert_called_once_with(("target.example.org",443),timeout=8)
            self.assertEqual(ctx.wrap_socket.call_args.kwargs["server_hostname"],"sni.example.org")
            tls.selected_alpn_protocol.return_value="http/1.1"
            with self.assertRaises(ProfileError):
                rehearse.check_reality_target("target.example.org","sni.example.org")

    def test_certificate_failure_blocks_trial(self):
        with patch.object(rehearse.socket,"create_connection",side_effect=ssl.SSLCertVerificationError()):
            with self.assertRaisesRegex(ProfileError,"certificate verification failed"):
                rehearse.check_reality_target("target.example.org","sni.example.org")


if __name__=="__main__":
    unittest.main()
