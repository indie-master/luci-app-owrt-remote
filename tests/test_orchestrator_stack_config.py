"""Source and renderer regressions for the single root Compose entrypoint."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_stack_config as stack


def fixture(root, mode):
    output = root / "generated"
    return {"RO_IMAGE": "router-orchestrator-hub:test", "RO_STATE_DIR": str(root / "data"),
            "RO_PORT": "18088", "RO_WSS_PORT": "28444",
            "RO_PUBLIC_URL": "https://hub.example.invalid:8443",
            "RO_XRAY_BACKEND": "external_readonly", "RO_STACK_MODE": mode,
            "RO_STACK_NAME": "ro-ci-stack", "RO_HTTP_PORT": "8080",
            "RO_XRAY_IMAGE": "example.invalid/xray@sha256:" + "a" * 64,
            "RO_NGINX_IMAGE": "example.invalid/nginx@sha256:" + "b" * 64,
            "RO_XRAY_CONFIG_DIR": str(root / "xray"), "RO_XRAY_ASSET_DIR": str(root / "assets"),
            "RO_TLS_DIR": str(root / "tls"), "RO_NGINX_CONF": str(output / "nginx.conf"),
            "RO_STREAM_ROUTES": "reality.example.invalid:24443"}


class UnifiedStackTests(unittest.TestCase):
    def test_standalone_routes_reality_before_tls_and_preserves_hub_and_wss(self):
        config = fixture(Path("/tmp/fixture"), "standalone")
        config["RO_NGINX_STREAM_MODULE"] = stack.STREAM_MODULE
        plan = stack.stack_plan(config)
        rendered = stack.render_internal(plan)
        self.assertTrue(rendered.startswith("load_module " + stack.STREAM_MODULE))
        self.assertIn("listen 8443;", rendered)
        self.assertIn("ssl_preread on;", rendered)
        self.assertIn("listen 127.0.0.1:18445 ssl;", rendered)
        self.assertIn("reality.example.invalid 127.0.0.1:24443;", rendered)
        self.assertIn("ssl_reject_handshake on;", rendered)
        self.assertIn("return 301 https://hub.example.invalid:8443$request_uri;", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:28444;", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:18088;", rendered)
        self.assertEqual(stack.render_external(plan), {})

    def test_external_stream_has_loopback_tls_and_no_public_listener(self):
        plan = stack.stack_plan(fixture(Path("/tmp/fixture"), "external-stream"))
        self.assertNotIn("listen 8443;", stack.render_internal(plan))
        self.assertNotIn("listen 8080;", stack.render_internal(plan))
        self.assertIn("listen 127.0.0.1:18445 ssl;", stack.render_internal(plan))
        stream = stack.render_external(plan)["external-stream.snippet"]
        self.assertIn("hub.example.invalid 127.0.0.1:18445;", stream)
        self.assertIn("reality.example.invalid 127.0.0.1:24443;", stream)

    def test_external_only_has_external_http_paths_and_optional_stream_map(self):
        plan = stack.stack_plan(fixture(Path("/tmp/fixture"), "external-only"))
        self.assertIsNone(stack.render_internal(plan))
        snippets = stack.render_external(plan)
        http = snippets["external-http.snippet"]
        self.assertIn("location ^~ /owrt-ws/", http)
        self.assertIn("proxy_pass http://127.0.0.1:28444;", http)
        self.assertIn("proxy_pass http://127.0.0.1:18088;", http)
        self.assertIn("proxy_set_header Upgrade $http_upgrade;", http)
        self.assertNotIn("hub.example.invalid", snippets["external-stream.snippet"])

    def test_invalid_ingress_config_does_not_render(self):
        for key, value in (
            ("RO_STACK_MODE", "unsafe"), ("RO_XRAY_IMAGE", "xray:latest"),
            ("RO_NGINX_IMAGE", "nginx:latest"), ("RO_WSS_PORT", "8443"),
            ("RO_NGINX_HEALTH_PORT", "18088"), ("RO_PUBLIC_URL", "https://hub.example.invalid:0"),
            ("RO_STREAM_ROUTES", "hub.example.invalid:24443"),
            ("RO_STREAM_ROUTES", "reality.example.invalid:8443"),
            ("RO_STREAM_ROUTES", "reality.example.invalid:24443,reality.example.invalid:34443"),
            ("RO_XRAY_CONFIG_DIR", "/tmp/fixture/data/nested"),
            ("RO_WSS_PATH", "/;\n"), ("RO_NGINX_STREAM_MODULE", "/tmp/example.so"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(stack.InvalidPlan):
                config = fixture(Path("/tmp/fixture"), "standalone")
                config[key] = value
                stack.stack_plan(config)

    def test_cli_uses_same_env_as_compose_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = root / "fixture.env"
            env.write_text("".join(f"{key}={value}\n" for key, value in fixture(root, "external-stream").items()))
            output = root / "generated"
            args = [sys.executable, str(ROOT / "tools/orchestrator_stack_config.py"),
                    "--env-file", str(env), "--output-dir", str(output)]
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"deployment_status": "REVIEW_ONLY"', result.stdout)
            self.assertTrue((output / "nginx.conf").is_file())
            self.assertTrue((output / "external-stream.snippet").is_file())
            result = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 2)

    def test_bind_alias_cannot_reuse_hub_state_as_xray_configuration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = fixture(root, "external-only")
            (root / "data").mkdir()
            (root / "xray").symlink_to(root / "data", target_is_directory=True)
            with self.assertRaisesRegex(stack.InvalidPlan, "without aliases"):
                stack.stack_plan(config)
