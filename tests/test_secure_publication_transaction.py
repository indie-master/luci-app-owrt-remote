import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_publication_transaction import PublicationTransaction, sha
from transport_profiles import ProfileError


class SimulatedPowerLoss(BaseException):
    pass


class TransactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state = root / "state"
        state.mkdir(mode=0o700)
        self.xray = root / "xray.json"
        self.nginx = root / "managed.map"
        self.xray.write_bytes(b"original-xray")
        self.nginx.write_bytes(b"original-nginx")
        self.xray.chmod(0o640)
        self.nginx.chmod(0o644)
        self.tx = PublicationTransaction(state, self.xray, self.nginx)
        self.calls = []

    def apply(self, **kwargs):
        args = dict(candidate_xray=b"new-xray", candidate_nginx=b"new-nginx",
                    expected_xray_sha=sha(b"original-xray"),
                    expected_nginx_sha=sha(b"original-nginx"),
                    validate=lambda a,b:self.calls.append("validate"),
                    activate=lambda:self.calls.append("activate"),
                    accept=lambda:True, verify_baseline=lambda:True)
        args.update(kwargs)
        return self.tx.apply(**args)

    def originals(self):
        self.assertEqual(self.xray.read_bytes(), b"original-xray")
        self.assertEqual(self.nginx.read_bytes(), b"original-nginx")
        self.assertEqual(self.xray.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.nginx.stat().st_mode & 0o777, 0o644)

    def test_accepted_retains_snapshot_and_modes(self):
        result = self.apply()
        self.assertEqual(result["phase"],"ACCEPTED")
        self.assertEqual(self.calls,["validate","activate"])
        self.assertEqual(self.xray.read_bytes(), b"new-xray")
        self.assertEqual(self.nginx.read_bytes(), b"new-nginx")
        self.assertEqual(self.xray.stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.tx.root/"xray.backup").read_bytes(),b"original-xray")
        self.assertEqual((self.tx.root/"xray.backup").stat().st_mode & 0o777,0o600)
        self.assertEqual(self.tx.recover(activate=lambda:self.fail("unexpected activation"),
                                        verify_baseline=lambda:True)["phase"],"ACCEPTED")
        with self.assertRaises(ProfileError):
            self.apply()

    def test_failed_acceptance_restores_both(self):
        with self.assertRaisesRegex(ProfileError,"acceptance"):
            self.apply(accept=lambda:False)
        self.originals()
        self.assertEqual(self.calls,["validate","activate","activate"])
        self.assertEqual(json.loads(self.tx.journal.read_text())["phase"],"ROLLED_BACK")

    def test_power_loss_recovers_without_acceptance(self):
        def crash():
            raise SimulatedPowerLoss()
        with self.assertRaises(SimulatedPowerLoss):
            self.apply(activate=crash)
        self.assertEqual(self.xray.read_bytes(),b"new-xray")
        self.assertEqual(self.tx.recover(activate=lambda:None,
            verify_baseline=lambda:True)["phase"],"ROLLED_BACK")
        self.originals()

    def test_stale_baseline_and_failed_validation_do_not_write(self):
        with self.assertRaises(ProfileError):
            self.apply(expected_xray_sha="bad")
        def reject(a,b):
            raise ProfileError("validation failed")
        with self.assertRaises(ProfileError):
            self.apply(validate=reject)
        self.originals()
        self.assertFalse(self.tx.journal.exists())

    def test_external_edit_not_overwritten_by_recovery(self):
        def crash():
            raise SimulatedPowerLoss()
        with self.assertRaises(SimulatedPowerLoss):
            self.apply(activate=crash)
        self.nginx.write_bytes(b"external-edit")
        with self.assertRaisesRegex(ProfileError,"externally"):
            self.tx.recover(activate=lambda:None,verify_baseline=lambda:True)
        self.assertEqual(self.nginx.read_bytes(),b"external-edit")
        self.assertEqual(self.xray.read_bytes(),b"new-xray")

    def test_unverified_rollback_retains_recovery_journal(self):
        with self.assertRaisesRegex(ProfileError,"baseline runtime"):
            self.apply(accept=lambda:False,verify_baseline=lambda:False)
        self.originals()
        self.assertEqual(json.loads(self.tx.journal.read_text())["phase"],"RESTORING")
        self.assertEqual(self.tx.recover(activate=lambda:None,
            verify_baseline=lambda:True)["phase"],"ROLLED_BACK")


if __name__=="__main__":
    unittest.main()
