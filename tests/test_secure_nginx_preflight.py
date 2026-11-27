import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"docker"))
import secure_nginx_preflight as preflight
from transport_profiles import ProfileError


class NginxPreflightTests(unittest.TestCase):
    def test_isolated_candidate_digest_and_live_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"managed.map"
            target.write_bytes(b"# empty\n")
            old=hashlib.sha256(target.read_bytes()).hexdigest()
            candidate=b"test.example.org 127.0.0.1:24001;\n"
            def run(command,**kwargs):
                self.assertEqual(command[:5],["/usr/bin/unshare","--mount","--propagation","private","--"])
                self.assertEqual(Path(command[-2]).read_bytes(),candidate)
                self.assertEqual(Path(command[-1]),target)
                return subprocess.CompletedProcess(command,0,b"candidate dump",b"")
            with patch.object(preflight,"MANAGED",target),patch.object(preflight.subprocess,"run",side_effect=run):
                result=preflight.candidate_digest(candidate,expected_live_sha="a"*64,
                    expected_fragment_sha=old,nginx_digest=lambda mode:"a"*64,target=target)
                self.assertEqual(result,hashlib.sha256(b"candidate dump").hexdigest())
                self.assertEqual(target.read_bytes(),b"# empty\n")

    def test_stale_baseline_refuses_namespace_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/"managed.map"
            target.write_bytes(b"# empty\n")
            with patch.object(preflight,"MANAGED",target),patch.object(preflight.subprocess,"run") as run:
                with self.assertRaisesRegex(ProfileError,"include changed"):
                    preflight.candidate_digest(b"candidate",expected_live_sha="a"*64,
                        expected_fragment_sha="bad",nginx_digest=lambda mode:"a"*64,target=target)
                run.assert_not_called()


if __name__=="__main__":
    unittest.main()
