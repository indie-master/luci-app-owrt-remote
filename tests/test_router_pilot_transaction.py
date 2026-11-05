"""Fault-injection tests for opt-in OpenWrt client pilot transaction.

Uses synthetic ROOT only. NEVER runs against a live router or its services.
"""
from pathlib import Path
import hashlib
import importlib.util
import os
import subprocess
import tarfile
import tempfile
import time
import unittest

BASE = Path(__file__).resolve().parents[1]
BUILDER = BASE / "tools/build_router_bundle.py"
GUARD = BASE / "tools/router_bundle_guard.sh"
TRANSACTION = BASE / "tools/router_pilot_transaction.sh"
spec = importlib.util.spec_from_file_location("ro_builder", BUILDER)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

class RouterPilotTransactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ro-transaction-test-")
        self.addCleanup(tmp.cleanup)
        self.temp = Path(tmp.name)
        self.repo = self.temp/'source'
        self.router = self.temp/'router'
        self.router.mkdir()
        (self.router / ".ro-pilot-synthetic-root").write_text("TEST ONLY\n")
        self.old = {}
        for n,mode in builder.FILES:
            p=self.repo/'files'/n
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('NEW:'+n+'\n')
            x=self.router/n
            x.parent.mkdir(parents=True,exist_ok=True)
            x.write_text('OLD:'+n+'\n')
            self.old[n]=x.read_bytes()
        for n in ("etc/config/owrtremote","etc/xray/owrt-remote-client.json"):
            p=self.router/n
            p.parent.mkdir(parents=True,exist_ok=True)
            p.write_text('SECRET-OLD:'+n+'\n')
            self.old[n]=p.read_bytes()
        self.bundle=self.temp/'candidate.tar'
        self.digest=builder.build(self.bundle,self.repo)
        self.state=self.router/'etc/router-orchestrator/upgrade'
        self.env={**os.environ,"RO_TEST_MODE":"1",
                  "RO_TEST_ROOT":str(self.router),"RO_TEST_ROUTER_ID":"main",
                  "RO_TEST_TRANSPORT":"tcp","RO_TEST_PORT":"18443",
                  "RO_PILOT_TIMEOUT":"12"}

    def run_cmd(self,tool,*args,timeout=25,env=None):
        return subprocess.run([*os.environ.get("RO_TEST_SHELL", "sh").split(),str(tool),*map(str,args)],
                              capture_output=True,text=True,timeout=timeout,
                              env=env or self.env)

    def prepared(self):
        r=self.run_cmd(GUARD,"stage",self.bundle,self.digest)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        r=self.run_cmd(GUARD,"snapshot")
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def apply(self,timeout=12):
        r=self.run_cmd(TRANSACTION,"apply",self.digest,
                       env={**self.env,"RO_PILOT_TIMEOUT":str(timeout)})
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        return r

    def restore_assertions(self):
        for n,data in self.old.items():
            self.assertEqual((self.router/n).read_bytes(),data,n)

    def cleanup_watchdog(self):
        if (self.state/'pending').exists():
            self.run_cmd(TRANSACTION,"rollback")
        # Wait briefly for the watchdog to exit before tempfile teardown.
        time.sleep(0.2)

    def test_nested_symlink_escape_blocks_apply_without_modification(self):
        self.prepared()
        # Attempt to redirect a pre-existing target directory to a
        # different location after the verified snapshot was created.
        nested = self.router / "usr/lib/owrt-remote"
        saved = self.temp / "outside-original-client-dir"
        nested.rename(saved)
        nested.symlink_to(saved, target_is_directory=True)
        r = self.run_cmd(TRANSACTION, "apply", self.digest)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("symlink in router path", r.stderr)
        self.assertFalse((self.state / "pending").exists())
        self.assertFalse((self.state / "armed").exists())
        for name in ("wan.sh", "logs.sh"):
            expected = self.old["usr/lib/owrt-remote/" + name]
            self.assertEqual((saved / name).read_bytes(), expected)

    def test_success_requires_explicit_nonce_confirmation(self):
        self.prepared()
        self.apply()
        self.assertTrue((self.state/'pending').exists())
        self.assertTrue((self.router/'etc/rc.d/S08ro-pilot-recovery').is_symlink())
        for n,_ in builder.FILES:
            self.assertEqual((self.router/n).read_bytes(), (self.repo/'files'/n).read_bytes())
        nonce=(self.state/'confirm-nonce').read_text().strip()
        self.assertEqual(len(nonce),32)
        self.assertNotEqual(self.run_cmd(TRANSACTION,'confirm','bad-token').returncode,0)
        self.assertTrue((self.state/'pending').exists())
        r=self.run_cmd(TRANSACTION,'confirm',nonce)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse((self.state/'pending').exists())
        self.assertTrue((self.state/'confirmed').exists())
        self.assertTrue((self.state/'backup').exists())
        self.assertEqual((self.router/'etc/config/owrtremote').read_bytes(),
                         self.old['etc/config/owrtremote'])

    def test_watchdog_restores_when_not_confirmed(self):
        self.prepared()
        self.apply(timeout=2)
        for _ in range(14):
            if (self.state/'rolled-back').exists():
                break
            time.sleep(0.5)
        self.assertTrue((self.state/'rolled-back').exists(),'watchdog did not roll back')
        self.restore_assertions()
        self.assertFalse((self.state/'pending').exists())

    def test_respawn_reuses_persisted_deadline_instead_of_resetting(self):
        self.prepared()
        self.apply(timeout=12)
        original_deadline=int((self.state/'deadline.uptime').read_text().strip())
        self.assertGreater(original_deadline, 0)
        # A fresh watchdog process after a simulated procd respawn is already
        # beyond the original deadline. It must roll back immediately, not
        # grant another full twelve-second interval.
        env={**self.env, 'RO_TEST_NOW': str(original_deadline+1)}
        before=time.monotonic()
        r=self.run_cmd(TRANSACTION, 'watchdog', env=env, timeout=6)
        elapsed=time.monotonic()-before
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertLess(elapsed,3.0)
        self.assertTrue((self.state/'rolled-back').exists())
        self.restore_assertions()
        self.assertFalse((self.state/'pending').exists())

    def test_corrupt_deadline_fails_closed_and_restores(self):
        self.prepared()
        self.apply(timeout=12)
        (self.state/'deadline.uptime').write_text('invalid\n')
        r=self.run_cmd(TRANSACTION, 'watchdog', timeout=6)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('deadline missing or invalid',r.stderr)
        self.assertTrue((self.state/'rolled-back').exists())
        self.restore_assertions()

    def test_confirmed_candidate_is_not_rolled_back_after_relaunch(self):
        self.prepared()
        self.apply(timeout=12)
        nonce=(self.state/'confirm-nonce').read_text().strip()
        r=self.run_cmd(TRANSACTION,'confirm',nonce)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        deadline=int((self.state/'deadline.uptime').read_text().strip())
        r=self.run_cmd(TRANSACTION,'watchdog',timeout=6,
                       env={**self.env,'RO_TEST_NOW':str(deadline+100)})
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        for name,_ in builder.FILES:
            self.assertEqual((self.router/name).read_bytes(),
                             (self.repo/'files'/name).read_bytes())
        self.assertFalse((self.state/'pending').exists())

    def test_late_confirmation_forces_rollback_even_without_watchdog_wakeup(self):
        self.prepared()
        self.apply(timeout=12)
        nonce=(self.state/'confirm-nonce').read_text().strip()
        deadline=int((self.state/'deadline.uptime').read_text().strip())
        r=self.run_cmd(TRANSACTION,'confirm',nonce,
                       env={**self.env,'RO_TEST_NOW':str(deadline+1)}, timeout=8)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('confirmation deadline expired',r.stderr)
        self.assertTrue((self.state/'rolled-back').exists())
        self.assertFalse((self.state/'pending').exists())
        self.restore_assertions()
        self.assertFalse((self.state/'confirmed').exists())

    def test_invalid_deadline_disallows_confirmation_and_restores(self):
        self.prepared()
        self.apply(timeout=12)
        nonce=(self.state/'confirm-nonce').read_text().strip()
        (self.state/'deadline.uptime').write_text('invalid\n')
        r=self.run_cmd(TRANSACTION,'confirm',nonce,timeout=8)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('missing deadline',r.stderr)
        self.assertTrue((self.state/'rolled-back').exists())
        self.restore_assertions()

    def _assert_power_loss_during_confirm_recovers(self, point):
        self.prepared()
        self.apply(timeout=12)
        nonce=(self.state/'confirm-nonce').read_text().strip()
        r=self.run_cmd(TRANSACTION,'confirm',nonce,
                       env={**self.env,'RO_TEST_CONFIRM_CRASH_AT':point})
        self.assertNotEqual(r.returncode,0)
        self.assertIn('simulated interruption',r.stderr)
        self.assertTrue((self.state/'pending').exists())
        r=self.run_cmd(TRANSACTION,'boot-recover',timeout=8)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse((self.state/'confirmed').exists())
        self.assertTrue((self.state/'rolled-back').exists())
        self.restore_assertions()
        self.assertFalse((self.state/'pending').exists())

    def test_power_loss_after_confirm_marker_recovers(self):
        self._assert_power_loss_during_confirm_recovers('marker')

    def test_power_loss_after_disarming_recovers(self):
        self._assert_power_loss_during_confirm_recovers('disarmed')

    def test_rollback_restores_agent_if_live_private_config_was_edited(self):
        self.prepared()
        self.apply(timeout=12)
        modified=self.router/'etc/config/owrtremote'
        modified.write_text('EXTERNAL CHANGE\n')
        r=self.run_cmd(TRANSACTION,'rollback',timeout=8)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        for name,_ in builder.FILES:
            self.assertEqual((self.router/name).read_bytes(), self.old[name])
        # Snapshot is protected but the updater must NEVER rewrite UCI.
        self.assertEqual(modified.read_text(),'EXTERNAL CHANGE\n')
        self.assertFalse((self.state/'pending').exists())

    def test_boot_recovery_works_without_network_or_hub(self):
        self.prepared()
        self.apply(timeout=12)
        r=self.run_cmd(self.state/'transaction.sh','boot-recover')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertTrue((self.state/'rolled-back').exists())
        self.restore_assertions()

    def test_manual_rollback_restores_every_file(self):
        self.prepared()
        self.apply()
        r=self.run_cmd(TRANSACTION,'rollback')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.restore_assertions()
        self.assertFalse((self.state/'pending').exists())

    def test_no_live_transaction_is_permitted_in_development_branch(self):
        # This is a *fail-closed check*, not a production invocation.
        # The script must reject any live call before reading/writing /etc.
        env={**os.environ}
        for key in tuple(env):
            if key.startswith("RO_TEST_"):
                env.pop(key, None)
        for command in ('apply','confirm','rollback','watchdog','boot-recover'):
            with self.subTest(command=command):
                p=subprocess.run(['sh',str(TRANSACTION),command],
                                 capture_output=True,text=True,env=env,timeout=5)
                self.assertNotEqual(p.returncode,0)
                self.assertIn('live transactions disabled',p.stderr)

    def test_missing_snapshot_rejects_apply(self):
        r=self.run_cmd(GUARD,'stage',self.bundle,self.digest)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        r=self.run_cmd(TRANSACTION,'apply',self.digest)
        self.assertNotEqual(r.returncode,0)
        self.assertFalse((self.state/'pending').exists())
        self.restore_assertions()

    def test_changed_uci_or_xray_config_blocks_apply(self):
        for n in ('etc/config/owrtremote','etc/xray/owrt-remote-client.json'):
            with self.subTest(file=n):
                # Separate fixture per iteration by reverting changed content.
                self.prepared() if not (self.state/'backup').exists() else None
                p=self.router/n
                orig=p.read_bytes()
                p.write_bytes(b'MUTATED')
                r=self.run_cmd(TRANSACTION,'apply',self.digest)
                self.assertNotEqual(r.returncode,0)
                self.assertFalse((self.state/'pending').exists())
                p.write_bytes(orig)

    def test_invalid_manifest_or_bundle_blocks_apply(self):
        self.prepared()
        p=self.state/'stage/unpacked/payload/usr/sbin/owrt-remote'
        p.write_text('TAMPERED')
        r=self.run_cmd(TRANSACTION,'apply',self.digest)
        self.assertNotEqual(r.returncode,0)
        self.assertFalse((self.state/'pending').exists())
        self.restore_assertions()

    def test_changed_original_client_blocks_apply(self):
        self.prepared()
        path=self.router/'usr/sbin/owrt-remote'
        path.write_text('CONCURRENT-EDIT')
        r=self.run_cmd(TRANSACTION,'apply',self.digest)
        self.assertNotEqual(r.returncode,0)
        self.assertFalse((self.state/'pending').exists())
        self.assertIn('changed after snapshot',r.stderr)

    def test_corrupted_recovery_file_blocks_apply(self):
        self.prepared()
        recovery=self.state/'backup/files/usr/sbin/owrt-remote'
        recovery.write_text('CORRUPTED-BACKUP')
        r=self.run_cmd(TRANSACTION,'apply',self.digest)
        self.assertNotEqual(r.returncode,0)
        self.assertFalse((self.state/'pending').exists())
        self.assertIn('backup corrupted',r.stderr)
        self.restore_assertions()

    def test_wrong_router_and_transport_block_mutation(self):
        self.prepared()
        for name,val in [('RO_TEST_ROUTER_ID','PaxoBs'),('RO_TEST_TRANSPORT','ws'),
                         ('RO_TEST_PORT','443')]:
            with self.subTest(name=name):
                r=self.run_cmd(TRANSACTION,'apply',self.digest,
                               env={**self.env,name:val})
                self.assertNotEqual(r.returncode,0)
                self.assertFalse((self.state/'pending').exists())
        self.restore_assertions()

    def test_duplicate_apply_is_refused_while_pending(self):
        self.prepared()
        self.apply()
        try:
            r=self.run_cmd(TRANSACTION,'apply',self.digest)
            self.assertNotEqual(r.returncode,0)
            self.assertTrue((self.state/'pending').exists())
        finally:
            self.cleanup_watchdog()

    def test_new_files_remain_restorable_when_originals_were_absent(self):
        n='usr/lib/owrt-remote/wan.sh'
        (self.router/n).unlink()
        del self.old[n]
        self.prepared()
        self.apply()
        r=self.run_cmd(TRANSACTION,'rollback')
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse((self.router/n).exists())
        self.restore_assertions()

if __name__=='__main__':
    unittest.main()
