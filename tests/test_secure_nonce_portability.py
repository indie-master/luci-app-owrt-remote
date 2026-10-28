"""Exercise the production nonce function when OpenWrt applets are absent.

Uses a synthetic shell process and never touches router files or services.
"""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = (
    ROOT / "tools/openwrt_hardware_compat_probe.sh",
    ROOT / "tools/router_pilot_transaction.sh",
)

class SecureNonceFallbackTests(unittest.TestCase):
    @staticmethod
    def run_function(source: Path, blocked: tuple[str, ...]):
        script_text = source.read_text(encoding="utf-8")
        start = script_text.index("secure_nonce() {")
        end = script_text.index("\n}\n", start) + len("\n}")
        function = script_text[start:end]
        # Intercept shell's command -v without changing any host binaries.
        blocked_case = "|".join(blocked)
        command_stub = (
            'command() {\n'
            '  [ "$1" = "-v" ] || return 1\n'
            f'  case "$2" in {blocked_case}) return 1 ;; esac\n'
            '  type "$2" >/dev/null 2>&1\n'
            '}\n'
        )
        command = command_stub + function + "\n" + (
            'value="$(secure_nonce)" || exit 17\n'
            '[ "${#value}" -eq 32 ] || exit 18\n'
            'case "$value" in *[!0-9a-f]*) exit 19 ;; esac\n'
            'printf "%s\\n" "$value"\n'
        )
        return subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=8)

    def test_no_od_needed_when_alternative_present(self):
        if not (shutil.which("hexdump") or shutil.which("openssl")):
            self.skipTest("CI runner has no hexdump/openssl")
        for source in SOURCES:
            with self.subTest(source=source.name):
                r = self.run_function(source, ("od",))
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(len(r.stdout.strip()), 32)

    def test_od_branch_still_supported(self):
        if not shutil.which("od"):
            self.skipTest("runner lacks od")
        for source in SOURCES:
            with self.subTest(source=source.name):
                r = self.run_function(source, ("hexdump", "openssl"))
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_all_secure_encoding_methods_missing_fails_closed(self):
        for source in SOURCES:
            with self.subTest(source=source.name):
                r = self.run_function(source, ("hexdump", "od", "openssl"))
                self.assertEqual(r.returncode, 17)
                self.assertEqual(r.stdout, "")

if __name__ == "__main__":
    unittest.main()
