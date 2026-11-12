"""Offline contract tests for Belt source-only OpenWrt export hotfix."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import belt_hub_host_export_hotfix as hotfix


class HostExportHotfixTests(unittest.TestCase):
    def test_exact_one_function_block_changed_and_idempotent(self):
        old = "before\n" + hotfix.OLD + "after\n"
        new, changed = hotfix.patch_source(old)
        self.assertTrue(changed)
        self.assertEqual(new, "before\n" + hotfix.NEW + "after\n")
        self.assertNotIn(hotfix.OLD, new)
        again, changed = hotfix.patch_source(new)
        self.assertFalse(changed)
        self.assertEqual(again, new)

    def test_unreviewed_installed_source_refused(self):
        for text in ("", "from datetime import date\n", hotfix.OLD * 2,
                     hotfix.NEW + hotfix.OLD):
            with self.subTest(text_length=len(text)):
                with self.assertRaises(hotfix.Blocked):
                    hotfix.patch_source(text)

    def test_repo_hub_uses_exact_same_new_export(self):
        # We must not accidentally hotfix a different source export
        # than the regression-tested development branch.
        text = (ROOT / "vps" / "owrt-remote-hub.py").read_text(encoding="utf-8")
        self.assertEqual(text.count(hotfix.NEW), 1)
        self.assertNotIn(hotfix.OLD, text)

    def test_hotfix_is_source_only_by_static_contract(self):
        text = (ROOT / "tools" / "belt_hub_host_export_hotfix.py").read_text()
        self.assertIn("health()", text)
        self.assertIn("service_pid(\"owrt-remote-xray.service\")", text)
        self.assertIn("XRAY: no changes", text)
        self.assertIn("atomic_source_copy(backup, args.installed)", text)
        self.assertNotIn('["systemctl", "restart", "owrt-remote-xray', text)


if __name__ == "__main__":
    unittest.main()
