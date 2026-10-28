"""Offline fixed-file bundle + snapshot tests; never touches production OpenWrt."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools/build_router_bundle.py"
GUARD = ROOT / "tools/router_bundle_guard.sh"
spec = importlib.util.spec_from_file_location("bundle_builder", BUILDER)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

class GuardedBundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ro-guard-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'repo'
        self.router = self.root / 'router'
        self.router.mkdir()
        for name, _ in builder.FILES:
            p = self.source / 'files' / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('NEW-CANDIDATE ' + name + '\n')
            old = self.router / name
            old.parent.mkdir(parents=True, exist_ok=True)
            old.write_text('ORIGINAL ' + name + '\n')
        for name in ('etc/config/owrtremote', 'etc/xray/owrt-remote-client.json'):
            f = self.router / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text('PRIVATE-ORIGINAL ' + name + '\n')
        self.bundle = self.root / 'candidate.tar'
        self.digest = builder.build(self.bundle, self.source)
        self.env = {**os.environ, 'RO_TEST_MODE':'1',
                    'RO_TEST_ROOT':str(self.router),
                    'RO_TEST_ROUTER_ID':'main',
                    'RO_TEST_TRANSPORT':'tcp',
                    'RO_TEST_PORT':'18443'}

    def guard(self, *args, env=None):
        return subprocess.run(['sh', str(GUARD), *map(str,args)],
                              env=self.env if env is None else env,
                              text=True, capture_output=True, timeout=20)

    def state(self):
        return self.router / 'etc/router-orchestrator/upgrade'

    def test_build_is_bit_for_bit_reproducible(self):
        other = self.root/'other.tar'
        self.assertEqual(self.digest, builder.build(other, self.source))
        self.assertEqual(self.bundle.read_bytes(), other.read_bytes())

    def test_verified_stage_and_snapshot_leave_originals_untouched(self):
        original = {name:(self.router/name).read_bytes() for name,_ in builder.FILES}
        p = self.guard('stage', self.bundle, self.digest)
        self.assertEqual(p.returncode, 0, p.stdout+p.stderr)
        self.assertIn('STAGED', p.stdout)
        q = self.guard('snapshot')
        self.assertEqual(q.returncode, 0, q.stdout+q.stderr)
        self.assertIn('SNAPSHOT READY', q.stdout)
        for name,_ in builder.FILES:
            self.assertEqual((self.router/name).read_bytes(),original[name])
            self.assertEqual((self.state()/'backup/files'/name).read_bytes(), original[name])
        self.assertEqual((self.router/'etc/config/owrtremote').read_text(),
                         'PRIVATE-ORIGINAL etc/config/owrtremote\n')
        self.assertEqual((self.state()/'backup/critical.sha256').read_text().count('\n'),2)
        self.assertFalse((self.state()/'pending').exists())

    def test_wrong_checksum_does_not_change_router(self):
        p = self.guard('stage', self.bundle, '0'*64)
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse(self.state().exists())

    def test_wrong_router_transport_or_port_fails_closed(self):
        for var,value in [('RO_TEST_ROUTER_ID','PaxoBs'),
                          ('RO_TEST_TRANSPORT','ws'), ('RO_TEST_PORT','443')]:
            with self.subTest(var=var):
                p = self.guard('stage', self.bundle, self.digest,
                               env={**self.env,var:value})
                self.assertNotEqual(p.returncode,0)
                self.assertFalse((self.state()/'stage').exists())

    def test_unknown_tar_entry_rejected_before_extraction(self):
        malicious = self.root/'malicious.tar'
        with tarfile.open(self.bundle) as original, tarfile.open(malicious,'w') as out:
            for m in original:
                out.addfile(m, original.extractfile(m))
            extra = tarfile.TarInfo('payload/../../escape')
            data=b'bad'
            extra.size=len(data)
            out.addfile(extra, io.BytesIO(data))
        digest=hashlib.sha256(malicious.read_bytes()).hexdigest()
        p=self.guard('stage',malicious,digest)
        self.assertNotEqual(p.returncode,0)
        self.assertFalse((self.root/'escape').exists())
        self.assertFalse((self.state()/'stage').exists())

    def test_symlink_payload_rejected_even_with_valid_archive_checksum(self):
        malicious=self.root/'symlink.tar'
        with tarfile.open(self.bundle) as original, tarfile.open(malicious,'w') as out:
            for member in original:
                if member.name=='payload/usr/sbin/owrt-remote':
                    member.type=tarfile.SYMTYPE
                    member.linkname='/etc/shadow'
                    member.size=0
                    out.addfile(member)
                else:
                    out.addfile(member,original.extractfile(member))
        p=self.guard('stage',malicious,hashlib.sha256(malicious.read_bytes()).hexdigest())
        self.assertNotEqual(p.returncode,0)
        self.assertFalse((self.state()/'stage').exists())

    def test_failed_or_repeated_stage_snapshot_cannot_overwrite_recovery(self):
        self.assertEqual(self.guard('stage',self.bundle,self.digest).returncode,0)
        self.assertNotEqual(self.guard('stage',self.bundle,self.digest).returncode,0)
        self.assertEqual(self.guard('snapshot').returncode,0)
        critical=(self.state()/'backup/critical.sha256').read_bytes()
        self.assertNotEqual(self.guard('snapshot').returncode,0)
        self.assertEqual((self.state()/'backup/critical.sha256').read_bytes(), critical)

    def test_apply_is_disabled_in_first_milestone(self):
        self.assertEqual(self.guard('stage',self.bundle,self.digest).returncode,0)
        self.assertEqual(self.guard('snapshot').returncode,0)
        p=self.guard('apply')
        self.assertNotEqual(p.returncode,0)
        self.assertIn('not yet enabled',p.stderr)
        self.assertTrue((self.router/'usr/sbin/owrt-remote').read_text().startswith('ORIGINAL'))

if __name__ == '__main__':
    unittest.main()
