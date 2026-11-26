"""Syntax-check inline JavaScript shipped to owner-only management pages."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PAGES = (
    ROOT / "vps/static/transport-configurator.html",
    ROOT / "vps/static/secure-profiles.html",
    ROOT / "vps/static/transport-manager.html",
    ROOT / "vps/static/xray-core-manager.html",
)


@unittest.skipUnless(shutil.which("node"), "Node.js is needed for JS syntax checks")
class WebInterfaceJavaScriptTests(unittest.TestCase):
    def test_management_pages_have_valid_javascript(self):
        for page in PAGES:
            with self.subTest(page=page.name):
                html = page.read_text(encoding="utf-8")
                scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html,
                                     flags=re.S | re.I)
                self.assertTrue(scripts, "management page contains no script")
                for script in scripts:
                    with tempfile.TemporaryDirectory() as folder:
                        path = Path(folder) / "page.js"
                        path.write_text(script, encoding="utf-8")
                        result = subprocess.run(
                            ["node", "--check", str(path)],
                            capture_output=True, text=True, timeout=20
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)


class ManagementPageContractTests(unittest.TestCase):
    def test_owner_navigation_uses_catalog_and_mobile_hides_desktop_links(self):
        source=(ROOT/"vps/owrt-remote-hub.py").read_text(encoding="utf-8")
        self.assertIn('id="transportCatalogLink" href="/secure-profiles/"',source)
        self.assertIn('id="xrayCoreLink" href="/xray-core/"',source)
        self.assertIn("['vpsTerminalLink', 'transportCatalogLink', 'xrayCoreLink', 'xrayReload', 'xrayRestart']",source)
        self.assertIn("node.hidden = !owner || mobileLayoutMq.matches",source)

    def test_transport_form_prioritizes_draft_and_keeps_diagnostics_collapsed(self):
        source=(ROOT/"vps/static/transport-configurator.html").read_text(encoding="utf-8")
        self.assertIn('id="transportFamily"',source)
        self.assertIn('id="transportSecurity"',source)
        self.assertNotIn('TCP Legacy</option>',source)
        self.assertLess(source.index('id="saveDraft"'),source.index('<details class="advanced">'))
        self.assertLess(source.index('<details class="advanced">'),source.index('name="router_uuid"'))
        self.assertIn("pilotPanel').hidden=!stageAvailable",source)

    def test_core_release_error_does_not_replace_running_status(self):
        source=(ROOT/"vps/static/xray-core-manager.html").read_text(encoding="utf-8")
        start=source.index("async function refreshVersions()")
        end=source.index("async function refresh()",start)
        self.assertNotIn("info.textContent",source[start:end])
        self.assertIn('releaseStatus',source[start:end])
        self.assertIn("held=s.maintenance_hold!==false",source)
        self.assertIn('id="maintenance"',source)


if __name__ == "__main__":
    unittest.main()
