"""Compatibility probe must inventory missing watchdog launcher without aborting.

The test executes only the self-cleaning /tmp synthetic-fixture probe. It
overrides command discovery in its own shell process, without editing PATH,
router services or any host utility.
"""
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tools/openwrt_hardware_compat_probe.sh"
TRANSACTION = ROOT / "tools/router_pilot_transaction.sh"

class OpenWrtProbeInventoryTests(unittest.TestCase):
    def test_missing_nohup_is_reported_not_required_for_filesystem_probe(self):
        wrapper = r'''command() {
  if [ "$1" = "-v" ] && [ "$2" = "nohup" ]; then return 1; fi
  type "$2" >/dev/null 2>&1
}
. "$1"
'''
        result = subprocess.run(
            ["sh", "-c", wrapper, "sh", str(PROBE)],
            capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PROBE: ABSENT - nohup", result.stdout)
        self.assertIn("PROBE: WATCHDOG BLOCKED", result.stdout)
        self.assertIn("PROBE: PASS - isolated temporary filesystem checks complete", result.stdout)

    def test_transaction_requires_launcher_before_creating_boot_hook(self):
        source = TRANSACTION.read_text(encoding="utf-8")
        section = source[source.index("apply() {"):source.index("\nwatchdog() {")]
        self.assertIn("command -v nohup", section)
        self.assertLess(section.index("command -v nohup"), section.index("install_boot_recovery"))
        # Production hard-lock remains mandatory even if later diagnostics pass.
        self.assertIn("'live transactions disabled:", source)

if __name__ == "__main__":
    unittest.main()
