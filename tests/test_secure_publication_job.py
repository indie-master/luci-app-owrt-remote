import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"docker"))
import secure_publication_job as job
from transport_profiles import ProfileError


class JobTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        root=Path(t.name)
        for name,path in (("JOB",root/"job.json"),("LOCK",root/"lock")):
            patcher=patch.object(job,name,path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.digest="a"*64

    def enqueue(self):
        return job.enqueue("test",1,self.digest,True)

    def test_disabled_or_unconfirmed_never_creates_job(self):
        with patch.object(job,"enabled",return_value=False):
            with self.assertRaises(ProfileError):
                self.enqueue()
        with patch.object(job,"enabled",return_value=True):
            with self.assertRaises(ProfileError):
                job.enqueue("test",1,self.digest,False)
        self.assertFalse(job.JOB.exists())

    def test_enqueue_uses_fixed_unit_and_hides_digest(self):
        with patch.object(job,"enabled",return_value=True), \
             patch.object(job,"connection",return_value=MagicMock()), \
             patch.object(job,"load_artifact",return_value=({},self.digest,False)), \
             patch.object(job.subprocess,"run",return_value=subprocess.CompletedProcess([],0)) as run:
            status=self.enqueue()
            self.assertEqual(status["status"],"pending")
            self.assertNotIn("digest",status)
            run.assert_called_once_with(["systemctl","start","--no-block",job.UNIT],
                                       capture_output=True,timeout=10)
            with self.assertRaises(ProfileError):
                self.enqueue()

    def test_start_timeout_keeps_recovery_gate(self):
        with patch.object(job,"enabled",return_value=True), \
             patch.object(job,"connection",return_value=MagicMock()), \
             patch.object(job,"load_artifact",return_value=({},self.digest,False)), \
             patch.object(job.subprocess,"run",side_effect=subprocess.TimeoutExpired("systemctl",10)):
            with self.assertRaises(ProfileError):
                self.enqueue()
        self.assertEqual(job.status()["status"],"recovery_required")

    def test_stale_digest_never_starts_service(self):
        with patch.object(job,"enabled",return_value=True), \
             patch.object(job,"connection",return_value=MagicMock()), \
             patch.object(job,"load_artifact",return_value=({},"b"*64,False)), \
             patch.object(job.subprocess,"run") as run:
            with self.assertRaises(ProfileError):
                self.enqueue()
            run.assert_not_called()
        self.assertFalse(job.JOB.exists())


if __name__=="__main__":
    unittest.main()
