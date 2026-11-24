import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("audit", ROOT / "tools/inspect_secure_ingress.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditTests(unittest.TestCase):
    def test_summary_has_routes_not_private_directives(self):
        raw = b"""# configuration file /etc/nginx/nginx.conf:
stream {
 map $ssl_preread_server_name $backend {
   default 127.0.0.1:8443;
   remote.example.org 127.0.0.1:18550;
   include /etc/nginx/stream-map.d/*.conf;
   ~^regex 127.0.0.1:9999;
 }
 server {
   listen 443;
   ssl_preread on;
   proxy_pass $backend;
 }
}
# configuration file /etc/nginx/conf.d/site.conf:
server {
 server_name site.example.org;
 proxy_set_header Authorization secret-value;
 ssl_certificate_key /private/key.pem;
 proxy_pass https://user:password@external.example.org;
}
"""
        result = audit.summarize(raw)
        mapping = result["/etc/nginx/nginx.conf"]["sni_maps"][0]
        self.assertEqual(mapping["variable"], "$backend")
        self.assertEqual(mapping["entries"][0]["upstream"], "127.0.0.1:8443")
        self.assertEqual(mapping["includes"], ["/etc/nginx/stream-map.d/*.conf"])
        self.assertEqual(mapping["unparsed"], 1)
        self.assertNotIn("secret-value", str(result))
        self.assertNotIn("password", str(result))
        self.assertNotIn("/private/key.pem", str(result))


if __name__ == "__main__":
    unittest.main()
