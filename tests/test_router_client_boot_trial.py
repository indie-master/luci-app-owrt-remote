"""Boot-trial state machine regression tests, disposable filesystem only.

No real init service, reboot, Xray, UCI or network calls. The existing
router_client_transaction suite separately exercises ten-file restoration.
"""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/router_client_boot_trial.sh"


@unittest.skipUnless(shutil.which("flock"), "flock required")
class BootTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ro-boot-trial-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "etc/router-orchestrator/upgrade"
        (self.state / "stage").mkdir(parents=True)
        for rel, seal in (("etc/config/owrtremote", "uci"),
                          ("etc/xray/owrt-remote-client.json", "xray")):
            live = self.root / rel
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_text("old " + seal)
            backup = self.state / "backup/files" / rel
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, backup)
            (self.state / "stage" / (seal + ".sha256")).write_text(
                hashlib.sha256(live.read_bytes()).hexdigest())
        # Root guard and absolute paths are replaced explicitly in the test copy.
        script = SOURCE.read_text()
        script = script.replace('[ "$(id -u)" = 0 ] && [ -r /etc/openwrt_release ] || fail', ': || fail')
        for var in ("S", "INIT", "BOOT", "UCI", "XRAY"):
            script = script.replace(f"\n{var}=/", f"\n{var}=$FIXTURE/")
        # Backup uses a live absolute path in production. Strip fixture prefix.
        script = script.replace('$S/backup/files$dest', '$S/backup/files${dest#"$FIXTURE"}')
        script = script.replace('$S/backup/files$XRAY', '$S/backup/files${XRAY#"$FIXTURE"}')
        script = script.replace('"/$rel"', '"$FIXTURE/$rel"')
        script = script.replace('request_reboot() { /sbin/reboot; }',
                                'request_reboot() { echo reboot >"$S/fake-reboot"; exit 0; }')
        marker = '\ncase "${1:-}" in\narm)'
        self.assertIn(marker, script)
        script = script.replace(marker, '''
boot_id() { echo "$TEST_BOOT"; }
now() { echo "$TEST_NOW"; }
uci() { :; }
sync() { :; }
runtime_ok() { [ "$TEST_RUNTIME" = yes ]; }
workers_ready() { [ "$TEST_WORKERS" = yes ]; }
verify_installed() { :; }
''' + marker)
        self.runner = self.state / "client-boot-trial.sh"
        self.runner.write_text(script)
        (self.state / "client-transaction.sh").write_text(
            '#!/bin/sh\n[ "$1" = boot-recover ] || exit 1\n'
            'S="$(dirname "$0")"\nprintf ROLLED_BACK >"$S/client.result"\n'
            'rm -f "$S/client.pending"\n')
        self.env = {**os.environ, "FIXTURE": str(self.root), "TEST_BOOT": "new",
                    "TEST_NOW": "100", "TEST_RUNTIME": "yes", "TEST_WORKERS": "yes"}
        self.mark("boot-trial.before", "old")
        self.mark("boot-trial.phase", "ARMED")

    def mark(self, name, value):
        (self.state / name).write_text(value + "\n")

    def call(self, *args, ok=True, **env):
        result = subprocess.run(["sh", str(self.runner), *args],
                                env={**self.env, **env}, capture_output=True,
                                text=True, timeout=15)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def current(self):
        return (self.state / "boot-trial.phase").read_text().strip()

    def test_new_boot_starts_fixed_window(self):
        self.call("boot-enter", ok=False, TEST_BOOT="old")
        self.assertEqual(self.current(), "ARMED")
        self.call("boot-enter")
        self.assertEqual(self.current(), "TRIAL")
        self.assertEqual((self.state / "boot-trial.deadline").read_text().strip(), "400")

    def test_next_boot_restores_configs_before_agent(self):
        self.call("boot-enter")
        (self.root / "etc/config/owrtremote").write_text("changed")
        (self.root / "etc/xray/owrt-remote-client.json").write_text("changed")
        self.call("boot-enter", TEST_BOOT="next")
        self.assertEqual(self.current(), "ROLLED_BACK")
        self.assertEqual((self.root / "etc/config/owrtremote").read_text(), "old uci")
        self.assertEqual((self.root / "etc/xray/owrt-remote-client.json").read_text(), "old xray")
        self.assertFalse((self.state / "fake-reboot").exists())

    def test_expired_worker_restores_then_requests_one_reboot(self):
        self.call("boot-enter")
        self.call("worker", "primary", TEST_NOW="401")
        self.assertEqual(self.current(), "ROLLED_BACK")
        self.assertTrue((self.state / "fake-reboot").exists())
        (self.state / "fake-reboot").unlink()
        self.call("boot-enter", TEST_BOOT="next")
        self.assertFalse((self.state / "fake-reboot").exists())

    def test_confirmation_requires_runtime_and_monitors_within_deadline(self):
        self.call("boot-enter")
        self.call("confirm", ok=False, TEST_RUNTIME="no")
        self.call("confirm", ok=False, TEST_WORKERS="no")
        self.call("confirm", ok=False, TEST_NOW="400")
        self.assertEqual(self.current(), "TRIAL")
        self.call("confirm")
        self.assertEqual(self.current(), "ACCEPTED")
        self.call("boot-enter", TEST_BOOT="next")
        self.assertEqual(self.current(), "ACCEPTED")


if __name__ == "__main__":
    unittest.main()
