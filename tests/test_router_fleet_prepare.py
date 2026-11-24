"""Belt coordinator tests. No SSH, no real router, no persistent writes."""
import argparse
import hashlib
import importlib.util
import io
from pathlib import Path
import shlex
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare", ROOT / "tools/router_fleet_prepare.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PrepareTests(unittest.TestCase):
    def test_inspect_payload_only_contains_guard(self):
        with tarfile.open(fileobj=io.BytesIO(prepare.make_payload())) as tar:
            self.assertEqual(tar.getnames(), ["guard.sh"])
            self.assertEqual(tar.extractfile("guard.sh").read(), prepare.GUARD.read_bytes())

    def test_stage_payload_requires_exact_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle.tar"
            bundle.write_bytes(b"fixture")
            with self.assertRaises(ValueError):
                prepare.make_payload(bundle, "0" * 64)
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            with tarfile.open(fileobj=io.BytesIO(prepare.make_payload(bundle, digest))) as tar:
                self.assertEqual(tar.getnames(), ["guard.sh", "bundle.tar"])
                self.assertEqual(tar.extractfile("bundle.tar").read(), b"fixture")

    def test_reject_invalid_target(self):
        for value in ("", "bad;cmd", "a b", "../x", "x"*129):
            with self.assertRaises(argparse.ArgumentTypeError):
                prepare.valid_id(value)
        for value in ("0", "65536", "-1", "x"):
            with self.assertRaises(argparse.ArgumentTypeError):
                prepare.port(value)

    def test_command_pins_loopback_ssh_and_only_prepare_actions(self):
        args = argparse.Namespace(router="WR3000S_2", transport="ws", vps_port=443,
                                  ssh_port=19001, stage=False, sha256=None)
        cmd = prepare.ssh_command(args)
        self.assertEqual(cmd[-2], "root@127.0.0.1")
        self.assertIn("StrictHostKeyChecking=ask", cmd)
        self.assertIn("ClearAllForwardings=yes", cmd)
        remote = shlex.split(cmd[-1])
        self.assertEqual(remote[-5:], ["WR3000S_2", "ws", "443", "inspect", "-"])
        self.assertNotIn("guard.sh apply", remote[2])
        self.assertNotIn("uci commit", remote[2])
        self.assertNotIn("restart", remote[2])


if __name__ == "__main__":
    unittest.main()
