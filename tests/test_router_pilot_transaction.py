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
