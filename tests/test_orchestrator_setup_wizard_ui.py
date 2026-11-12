"""Offline proof that static setup wizard outputs match Python backend schemas.

Uses a tiny DOM stub with Node; never launches a browser, connects to
network, touches Docker, or accesses a real installation.
"""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
WIZARD = ROOT / "vps" / "static" / "orchestrator-setup-preview.html"
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_edge_plan as edge
import orchestrator_profile_catalog as catalog

NODE = shutil.which("node")


def script_text():
    text = WIZARD.read_text()
    hits = re.findall(r"<script>(.*?)</script>", text, re.S)
    if len(hits) != 1:
        raise AssertionError("expected exactly one fully local setup wizard script")
    return hits[0]


@unittest.skipUnless(NODE, "node required")
class WizardPreviewTests(unittest.TestCase):
    def result(self, *, mode="external_nginx", tls="external_tls", backup=False):
        defaults = {
            "domain": "hub.example.org", "backupDomain": "backup.example.org" if backup else "",
            "mode": mode, "tls": tls,
            "httpsPort": "443", "hubPort": "18088", "wssPort": "28444",
            "wsPath": "/owrt-ws/", "realityPort": "24443",
            "reality": False, "sni": "www.example.org",
            "target": "www.example.org:443", "fingerprint": "chrome",
            "hubXray": "26.10.21", "routerXray": "26.10.21",
            "email": "admin@example.org", "provider": "cloudflare",
            "cert": "/etc/letsencrypt", "key": "/etc/ssl/private/site.key",
        }
        # The HTML wizard itself is executed inside an isolated fake DOM;
        # the output is parsed by both real server-side validators below.
        harness = r"""
const initial = __DEFAULTS__;
const els = {};
function node(id) {
  if (!els[id]) {
    els[id] = {
      value: initial[id] || "", checked: initial[id] === true,
      hidden: false, disabled: false, textContent: "", className: "",
      children: [], handlers: {},
      replaceChildren() { this.children = []; this.value = ""; },
      appendChild(child) {
        this.children.push(child);
        if (!this.value) this.value = child.value || "";
      },
      addEventListener(name, cb) { this.handlers[name] = cb; }
    };
  }
  return els[id];
}
globalThis.document = {
  getElementById: node,
  createElement: function () { return {value:"",textContent:""}; }
};
__WIZARD_SCRIPT__
node("mode").value = "__MODE__";
node("mode").handlers.change();
node("tls").value = "__TLS__";
node("tls").handlers.change();
node("wizard").handlers.submit({preventDefault(){}});
if (node("results").hidden) {
  throw Error("wizard refused valid input: " + node("message").textContent);
}
process.stdout.write(JSON.stringify({
  edge:JSON.parse(node("edgePreview").textContent),
  catalog:JSON.parse(node("profilePreview").textContent)
}));
"""
        harness = (harness.replace("__DEFAULTS__", json.dumps(defaults))
                   .replace("__WIZARD_SCRIPT__", script_text())
                   .replace("__MODE__", mode)
                   .replace("__TLS__", tls))
        result = subprocess.run([NODE, "-e", harness], capture_output=True,
                                text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        edge.validate(data["edge"])
        catalog.validate(data["catalog"])
        return data

    def test_javascript_is_syntactically_valid_and_never_fetches_secrets(self):
        script = script_text()
        result = subprocess.run([NODE, "--check"], input=script,
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("fetch(", script)
        self.assertNotIn("localStorage", script)
        self.assertNotIn("hub_token", script)
        self.assertNotIn("privateKey", script)

    def test_existing_host_nginx_outputs_same_schema_as_python(self):
        data = self.result()
        self.assertEqual(data["edge"]["nginx_mode"], "external_nginx")
        self.assertEqual(data["edge"]["tls_mode"], "external_tls")
        self.assertEqual(data["catalog"]["profiles"][0]["endpoint_host"],
                         "hub.example.org")

    def test_bundled_nginx_certbot_http01_and_reality_options_validate(self):
        data = self.result(mode="bundled_nginx", tls="acme_http01", backup=True)
        self.assertEqual(data["edge"]["tls_mode"], "acme_http01")
        self.assertEqual(len(data["catalog"]["profiles"]), 2)
        self.assertEqual(data["catalog"]["profiles"][1]["id"], "wss-backup")
        self.assertIn("nginx:\n", edge.render_compose_plan(data["edge"]))

    def test_import_existing_certbot_directory(self):
        data = self.result(mode="bundled_nginx", tls="existing_letsencrypt")
        self.assertEqual(data["edge"]["cert_path"], "/etc/letsencrypt")
        compose = edge.render_compose_plan(data["edge"])
        self.assertIn("/etc/letsencrypt:/etc/letsencrypt:ro", compose)


if __name__ == "__main__":
    unittest.main()
