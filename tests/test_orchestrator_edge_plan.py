"""Offline Docker/nginx topology planning tests; no privileged actions."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_edge_plan as edge


class DockerEdgePlanTests(unittest.TestCase):
    def test_external_nginx_reuses_existing_vhost_certs_and_ports(self):
        plan = edge.new_edge_plan("sprt-wrt.cdnvc.xyz", mode="external_nginx")
        self.assertEqual(plan["tls_mode"], "external_tls")
        conf = edge.render_external_nginx_snippet(plan)
        self.assertIn("location ^~ /owrt-ws/", conf)
        self.assertIn("proxy_pass http://127.0.0.1:28444;", conf)
        self.assertIn("proxy_pass http://127.0.0.1:18088;", conf)
        compose = edge.render_compose_plan(plan)
        self.assertIn('127.0.0.1:18088:8088', compose)
        self.assertIn('127.0.0.1:28444:18444', compose)
        self.assertIn("network_mode: service:ro-core", compose)
        self.assertNotIn('      - "80:80"', compose)
        self.assertNotIn("  nginx:\n", compose)
        self.assertNotIn("private_key", json.dumps(plan))
        self.assertFalse(plan["automatic_nginx_overwrite"])

    def test_bundled_nginx_http01_initial_cert_is_two_stage(self):
        plan = edge.new_edge_plan("new.example.org", mode="bundled_nginx",
                                  tls_mode="acme_http01",
                                  acme_email="admin@example.org")
        compose = edge.render_compose_plan(plan)
        conf = edge.render_bundled_nginx(plan)
        self.assertIn('      - "80:80"', compose)
        self.assertIn('      - "443:443"', compose)
        self.assertIn("PINNED_NGINX_IMAGE_REQUIRED", compose)
        self.assertIn("listen 443 ssl;", conf)
        self.assertIn("letsencrypt/live/new.example.org/fullchain.pem", conf)
        self.assertIn("location ^~ /.well-known/acme-challenge/", conf)
        self.assertIn("REVIEW ONLY", conf)
        self.assertIn("Certificate bootstrap/renewal: REQUIRED", edge.preview(plan))

    def test_bundled_reuses_existing_pem_without_copying_private_key(self):
        plan = edge.new_edge_plan("my.example.net", mode="bundled_nginx",
                                  tls_mode="existing_pem",
                                  cert_path="/etc/ssl/mychain.pem",
                                  key_path="/etc/ssl/private/mykey.pem")
        compose = edge.render_compose_plan(plan)
        self.assertIn("/etc/ssl/mychain.pem:/run/ro-tls/fullchain.pem:ro", compose)
        self.assertIn("/etc/ssl/private/mykey.pem:/run/ro-tls/privkey.pem:ro", compose)
        conf = edge.render_bundled_nginx(plan)
        self.assertIn("ssl_certificate /run/ro-tls/fullchain.pem;", conf)
        self.assertNotIn("BEGIN PRIVATE KEY", compose)

    def test_certbot_tree_is_mounted_as_directory_to_preserve_renewed_symlinks(self):
        plan = edge.new_edge_plan("hub.example.org", mode="bundled_nginx",
                                  tls_mode="existing_letsencrypt",
                                  cert_path="/etc/letsencrypt")
        compose = edge.render_compose_plan(plan)
        self.assertIn("/etc/letsencrypt/live/hub.example.org:/etc/letsencrypt/live/hub.example.org:ro", compose)
        self.assertIn("/etc/letsencrypt/archive/hub.example.org:/etc/letsencrypt/archive/hub.example.org:ro", compose)
        self.assertNotIn("/etc/letsencrypt:/etc/letsencrypt:ro", compose)
        conf = edge.render_bundled_nginx(plan)
        self.assertIn("/etc/letsencrypt/live/hub.example.org/fullchain.pem", conf)
        self.assertNotIn("/run/ro-tls/fullchain.pem", conf)

    def test_existing_wildcard_cert_can_use_different_certbot_lineage(self):
        plan = edge.new_edge_plan("hub.cdnvc.xyz", mode="bundled_nginx",
                                  tls_mode="existing_letsencrypt",
                                  cert_path="/etc/letsencrypt",
                                  cert_name="cdnvc.xyz")
        compose = edge.render_compose_plan(plan)
        nginx = edge.render_bundled_nginx(plan)
        self.assertIn("/etc/letsencrypt/live/cdnvc.xyz:/etc/letsencrypt/live/cdnvc.xyz:ro", compose)
        self.assertIn("/etc/letsencrypt/archive/cdnvc.xyz:/etc/letsencrypt/archive/cdnvc.xyz:ro", compose)
        self.assertIn("ssl_certificate /etc/letsencrypt/live/cdnvc.xyz/fullchain.pem;", nginx)
        self.assertIn("server_name hub.cdnvc.xyz;", nginx)

    def test_custom_https_public_port_is_consistent_with_nginx_and_redirect(self):
        plan = edge.new_edge_plan("hub.example.org", mode="bundled_nginx",
                                  tls_mode="acme_dns01",
                                  acme_email="admin@example.org",
                                  dns_provider="cloudflare",
                                  https_port=8443)
        compose = edge.render_compose_plan(plan)
        conf = edge.render_bundled_nginx(plan)
        self.assertIn('      - "8443:8443"', compose)
        self.assertIn("listen 8443 ssl;", conf)
        self.assertIn("https://$host:8443$request_uri", conf)
        self.assertEqual(plan["public_url"], "https://hub.example.org:8443")

    def test_dns01_needs_explicit_provider_and_safe_domain(self):
        good = edge.new_edge_plan("*.example.org".replace("*.", "wildcard."),
                                  mode="bundled_nginx", tls_mode="acme_dns01",
                                  acme_email="admin@example.org",
                                  dns_provider="cloudflare")
        self.assertEqual(good["dns_provider"], "cloudflare")
        for kwargs in (
            {"mode": "external_nginx", "tls_mode": "acme_http01"},
            {"mode": "bundled_nginx", "tls_mode": "acme_dns01",
             "acme_email": "admin@example.org"},
            {"mode": "bundled_nginx", "tls_mode": "acme_http01",
             "acme_email": ""},
            {"mode": "bundled_nginx", "tls_mode": "existing_pem",
             "cert_path": "../../cert.pem", "key_path": "/private/key"},
            {"mode": "bundled_nginx", "tls_mode": "existing_pem",
             "cert_path": "/cert.pem", "key_path": "/cert.pem"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(edge.InvalidPlan):
                edge.new_edge_plan("hub.example.net", **kwargs)

    def test_no_unsafe_path_or_colliding_ports(self):
        plan = edge.new_edge_plan("own.example.net")
        for field, value in (("hub_host_port", 443), ("wss_host_port", 18088),
                             ("wss_path", "/bad path/"),
                             ("public_url", "http://own.example.net"),
                             ("automatic_xray_restart", True),
                             ("deployment_status", "DEPLOY")):
            candidate = dict(plan)
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(edge.InvalidPlan):
                edge.validate(candidate)

    def test_cli_writes_staged_files_only_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            plan = Path(td) / "edge.json"
            stage = Path(td) / "output"
            script = ROOT / "tools" / "orchestrator_edge_plan.py"
            result = subprocess.run(
                [sys.executable, str(script), "init",
                 "--domain", "hub.example.org", "--output", str(plan)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(plan.stat().st_mode & 0o077, 0)
            self.assertIn("NO changes", result.stdout)
            result = subprocess.run(
                [sys.executable, str(script), "stage",
                 str(plan), "--output-dir", str(stage)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PREVIEW ONLY", result.stdout)
            self.assertTrue((stage / "compose.review.yml").exists())
            self.assertTrue((stage / "nginx-external.snippet").exists())
            result = subprocess.run(
                [sys.executable, str(script), "stage",
                 str(plan), "--output-dir", str(stage)],
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("BLOCKED", result.stderr)


if __name__ == "__main__":
    unittest.main()
