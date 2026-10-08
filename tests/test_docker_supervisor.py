import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("docker_supervisor", ROOT / "docker/xray/supervisor.py")
sup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sup)

FAKE = """#!/usr/bin/env python3
import json,sys,time
file=sys.argv[sys.argv.index('-config')+1]
data=json.load(open(file))
if data.get('invalid'):
    sys.exit(1)
if '-test' in sys.argv:
    sys.exit(0)
if data.get('crash'):
    sys.exit(1)
time.sleep(50)
"""

class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.file = root / "owrt-remote.json"
        self.binary = root / "fake-xray"
        self.binary.write_text(FAKE)
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        self.file.write_text(json.dumps({"inbounds": [], "version": 1}))
        self.oldbin, self.oldconf = sup.BIN, sup.CONFIG
        sup.BIN, sup.CONFIG = str(self.binary), self.file
        self.addCleanup(self.reset)
        self.manager = sup.Manager()
        self.addCleanup(self.manager.stop_child)

    def reset(self):
        sup.BIN, sup.CONFIG = self.oldbin, self.oldconf

    def test_replace_and_noop(self):
        self.assertTrue(self.manager.start())
        self.assertEqual(self.manager.install({"inbounds": [], "version": 2}), {"changed": True})
        self.assertEqual(json.loads(self.file.read_text())["version"], 2)
        self.assertFalse(self.manager.install({"inbounds": [], "version": 2})["changed"])
        self.assertTrue(self.manager.status()["ok"])

    def test_invalid_config_keeps_running(self):
        self.manager.start()
        prior = self.manager.child.pid
        with self.assertRaises(RuntimeError):
            self.manager.install({"inbounds": [], "invalid": True})
        self.assertEqual(self.manager.child.pid, prior)
        self.assertFalse(json.loads(self.file.read_text()).get("invalid"))

    def test_failed_new_process_rolls_back(self):
        self.manager.start()
        with self.assertRaises(RuntimeError):
            self.manager.install({"inbounds": [], "crash": True})
        self.assertEqual(json.loads(self.file.read_text())["version"], 1)
        self.assertTrue(self.manager.status()["ok"])

if __name__ == "__main__":
    unittest.main()
