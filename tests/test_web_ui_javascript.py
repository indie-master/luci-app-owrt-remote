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


if __name__ == "__main__":
    unittest.main()
