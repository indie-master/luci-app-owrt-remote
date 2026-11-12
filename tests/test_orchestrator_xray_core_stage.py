"""Offline version-pinning tests: no installed Xray or network usage."""
from pathlib import Path
import hashlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_xray_core_stage as gate


class XrayCoreStageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.candidate = root / "new-xray"
        self.candidate.write_bytes(b"\x7fELF" + b"candidate-fixture")
        self.candidate.chmod(0o700)
        self.active = root / "running-xray"
        self.active.write_bytes(b"\x7fELF" + b"existing-fixture")
        self.config = root / "reverse.json"
        self.config.write_text('{"inbounds": [], "outbounds": []}')
        self.expected = hashlib.sha256(self.candidate.read_bytes()).hexdigest()

    def test_verified_version_and_syntax_are_read_only(self):
        old = self.active.read_bytes()
        cfg = self.config.read_bytes()
        with mock.patch.object(gate, "version_of",
                               side_effect=["26.3.27", "26.10.21"]) as versions, \
             mock.patch.object(gate, "test_config") as check:
            report = gate.preview(self.candidate, self.expected, "26.10.21",
                                  self.config, self.active)
        self.assertEqual(report["current_version"], "26.3.27")
        self.assertEqual(report["candidate_version"], "26.10.21")
        self.assertFalse(report["installed"])
        self.assertFalse(report["restart"])
        self.assertEqual(self.active.read_bytes(), old)
        self.assertEqual(self.config.read_bytes(), cfg)
        check.assert_called_once_with(self.candidate, self.config)
        self.assertEqual(versions.call_count, 2)

    def test_wrong_sha_blocks_before_executing_binary(self):
        with mock.patch.object(gate, "version_of") as run:
            with self.assertRaises(gate.GateError):
                gate.preview(self.candidate, "a" * 64, "26.10.21",
                             self.config, self.active)
            run.assert_not_called()

    def test_wrong_version_is_rejected(self):
        with mock.patch.object(gate, "version_of", return_value="26.11.9"), \
             mock.patch.object(gate, "test_config") as test:
            with self.assertRaises(gate.GateError):
                gate.preview(self.candidate, self.expected, "26.10.21", self.config)
            test.assert_not_called()

    def test_candidate_is_extracted_elf_not_download_archive(self):
        self.candidate.write_bytes(b"PK\x03\x04not-an-elf")
        new_sha = hashlib.sha256(self.candidate.read_bytes()).hexdigest()
        with self.assertRaises(gate.GateError):
            gate.preview(self.candidate, new_sha, "26.10.21", self.config)

    def test_rejects_unpinned_and_active_executable(self):
        with self.assertRaises(gate.GateError):
            gate.preview(self.candidate, "unverified", "26.10.21", self.config)
        with self.assertRaises(gate.GateError):
            gate.preview(self.active, hashlib.sha256(self.active.read_bytes()).hexdigest(),
                         "26.10.21", self.config, self.active)

    def test_rejects_config_syntax_failure(self):
        with mock.patch.object(gate, "version_of", return_value="26.10.21"), \
             mock.patch.object(gate, "test_config", side_effect=gate.GateError("bad config")):
            with self.assertRaises(gate.GateError):
                gate.preview(self.candidate, self.expected, "26.10.21", self.config)


if __name__ == "__main__":
    unittest.main()
