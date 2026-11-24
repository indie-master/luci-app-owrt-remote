import importlib.util
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "tools/bootstrap_external_ingress.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)
from transport_profiles import ProfileError

BASE = b"""events { worker_connections 1024; }
http {
    server {
        listen 127.0.0.1:8443 ssl proxy_protocol;
        server_name existing.example.org;
    }
}
stream {
    map $ssl_preread_server_name $backend {
        node.example.org 127.0.0.1:1111;
        default 127.0.0.1:8443;
    }
    server {
        listen 443 reuseport;
        proxy_pass $backend;
        ssl_preread on;
        proxy_protocol on;
    }
}
"""


class BootstrapTests(unittest.TestCase):
    def test_exact_additive_include_and_proxy_detection(self):
        candidate, routes, proxy = bootstrap.prepare(BASE)
        addition = b"\n        include /etc/nginx/router-orchestrator-managed.map;"
        self.assertEqual(candidate.replace(addition,b"",1),BASE)
        self.assertEqual(routes,{"node.example.org":"127.0.0.1:1111","default":"127.0.0.1:8443"})
        self.assertTrue(proxy)
        self.assertFalse(bootstrap.prepare(BASE.replace(b"proxy_protocol on;",b"proxy_protocol off;"))[2])

    def test_inherited_proxy_protocol_and_server_override(self):
        config = BASE.replace(b"stream {",b"stream {\n    proxy_protocol on;").replace(b"        proxy_protocol on;\n",b"")
        self.assertTrue(bootstrap.prepare(config)[2])
        self.assertFalse(bootstrap.prepare(config.replace(b"ssl_preread on;",b"ssl_preread on;\n        proxy_protocol off;"))[2])

    def test_reject_existing_managed_include_regex_or_default_change(self):
        for old,new in (
            (b"node.example.org",b"*.example.org"),
            (b"default 127.0.0.1:8443",b"default 127.0.0.1:9443"),
            (b"node.example.org 127.0.0.1:1111;",b"include /etc/nginx/other.map;"),
            (b"ssl_preread on;",b"ssl_preread off;"),
        ):
            with self.subTest(new=new),self.assertRaises(ProfileError):
                bootstrap.prepare(BASE.replace(old,new))

    def test_http_vhost_names_are_reserved(self):
        _,routes,_ = bootstrap.prepare(BASE)
        names,occupied = bootstrap.approved_names(["fresh.example.org"],BASE,routes)
        self.assertEqual(names,["fresh.example.org"])
        self.assertIn("existing.example.org",occupied)
        for name in ("existing.example.org","node.example.org"):
            with self.assertRaises(ProfileError):
                bootstrap.approved_names([name],BASE,routes)

    def test_empty_ownership_is_valid_and_no_new_routes(self):
        candidate,routes,proxy = bootstrap.prepare(BASE)
        names,occupied=bootstrap.approved_names([],BASE,routes)
        bootstrap.validate_inventory({"mode":"external-stream","public_port":443,"allowed_names":names,
             "existing_routes":[{"public_port":443,"sni":n} for n in occupied],
             "occupied_ports":[443],"managed_routes":[],"nginx_config_sha256":"a"*64,
             "proxy_protocol":proxy})


if __name__ == "__main__":
    unittest.main()
