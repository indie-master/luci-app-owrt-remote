"""Fresh OpenWrt bootstrap tests: disposable root only, no network or procd.

Never runs the installer with ROOT=/, and never updates a real router.
"""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

BASE=Path(__file__).resolve().parents[1]
INSTALL=BASE/"tools/router_fresh_install.sh"
PIN="a"*40

class FreshInstallTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix="ro-fresh-install-test-",dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.tmp=Path(temp.name)
        self.router=self.tmp/"router"
        self.router.mkdir()
        (self.router/".ro-fresh-install-test-root").touch()
        self.env={**os.environ,
                  "RO_FRESH_TEST_MODE":"1",
                  "RO_TEST_ROOT":str(self.router),
                  "RO_FRESH_TEST_SOURCE":str(BASE)}
    def script(self,*args,env=None):
        return subprocess.run(["sh",str(INSTALL),*args],
                              env=env or self.env,text=True,
                              capture_output=True,timeout=35)
    def test_check_is_read_only_and_no_files_installed(self):
        x=self.script("--check")
        self.assertEqual(x.returncode,0,x.stdout+x.stderr)
        self.assertIn("PASS - no existing",x.stdout)
        self.assertFalse((self.router/"usr").exists())
        self.assertFalse((self.router/"etc").exists())

    def test_fresh_install_preloads_and_writes_exact_source_bundle(self):
        x=self.script("--install",PIN)
        self.assertEqual(x.returncode,0,x.stdout+x.stderr)
        self.assertIn("INSTALLED - initial files written",x.stdout)
        for rel in (
            "usr/sbin/owrt-remote",
            "usr/lib/owrt-remote/wan.sh",
            "usr/lib/owrt-remote/logs.sh",
            "etc/init.d/owrt-remote",
            "etc/init.d/owrt-remote-logs",
            "www/cgi-bin/owrt-remote",
            "www/luci-static/resources/view/owrt_remote.js",
            "etc/config/owrtremote"
        ):
            actual=(self.router/rel).read_bytes()
            expected=(BASE/"files"/rel).read_bytes()
            self.assertEqual(actual,expected,rel)
        key=(self.router/"etc/owrt-remote/web.key").read_text().strip()
        self.assertGreater(len(key),16)
        self.assertNotIn(key,x.stdout+x.stderr)
        # The same script is strictly fresh-only, never repeats an installation.
        second=self.script("--install",PIN)
        self.assertNotEqual(second.returncode,0)
        self.assertIn("existing state",second.stderr)

    def test_existing_agent_or_uci_blocks_fresh_install_before_stage(self):
        p=self.router/"etc/config/owrtremote"
        p.parent.mkdir(parents=True)
        p.write_text("DO-NOT-REPLACE\n")
        x=self.script("--install",PIN)
        self.assertNotEqual(x.returncode,0)
        self.assertIn("existing state",x.stderr)
        self.assertEqual(p.read_text(),"DO-NOT-REPLACE\n")
        self.assertFalse((self.router/"usr").exists())

    def test_nested_symlink_escape_fails_before_install(self):
        outside=self.tmp/"outside"
        outside.mkdir()
        (self.router/"usr").symlink_to(outside,target_is_directory=True)
        x=self.script("--install",PIN)
        self.assertNotEqual(x.returncode,0)
        self.assertIn("symlink in target path",x.stderr)
        self.assertEqual(list(outside.iterdir()),[])

    def test_invalid_revision_and_unguarded_usage_blocked(self):
        for argv in ((),("--install",),("--install","main"),
                     ("--install","abcdef; touch /tmp/oops"),):
            with self.subTest(argv=argv):
                x=self.script(*argv)
                self.assertNotEqual(x.returncode,0)
        self.assertFalse((self.router/"usr").exists())

    def test_missing_file_refuses_before_modifying_router(self):
        outside=self.tmp/"missing-source"
        outside.mkdir()
        x=self.script("--install",PIN,
                      env={**self.env,"RO_FRESH_TEST_SOURCE":str(outside)})
        self.assertNotEqual(x.returncode,0)
        self.assertIn("missing test payload",x.stderr)
        self.assertFalse((self.router/"usr").exists())

if __name__=="__main__":
    unittest.main()
