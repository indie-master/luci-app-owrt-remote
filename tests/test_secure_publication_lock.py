import fcntl
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"docker"))
import host_adapter as host


class MutationLockTests(unittest.TestCase):
    def test_direct_mutations_share_lifecycle_lock_and_respect_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock=Path(tmp)/"xray-lifecycle.lock"
            hold=Path(tmp)/"hold"
            adapter=host.Adapter(allow_restart=True,allow_apply=True)
            adapter.last_restart=-1000
            with patch.object(host,"LIFECYCLE_LOCK",lock),patch.object(host,"HOLD",hold), \
                 patch.object(adapter,"_xray_restart_locked",return_value={"ok":True}) as restart, \
                 patch.object(adapter,"_xray_apply_locked",return_value={"ok":True}) as apply:
                with lock.open("a+") as fd:
                    fcntl.flock(fd.fileno(),fcntl.LOCK_EX)
                    with self.assertRaisesRegex(host.Rejected,"already running"):
                        adapter.xray_restart()
                    with self.assertRaisesRegex(host.Rejected,"already running"):
                        adapter.xray_apply({})
                    restart.assert_not_called()
                    apply.assert_not_called()
                hold.touch()
                with self.assertRaisesRegex(host.Rejected,"maintenance hold"):
                    adapter.xray_restart()
                hold.unlink()
                self.assertTrue(adapter.xray_restart()["ok"])
                self.assertTrue(adapter.xray_apply({})["ok"])
                restart.assert_called_once()
                apply.assert_called_once_with({})

    def test_disabled_write_does_not_create_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"lock"
            with patch.object(host,"LIFECYCLE_LOCK",path):
                with self.assertRaises(host.Rejected):
                    host.Adapter().xray_restart()
                self.assertFalse(path.exists())


if __name__=="__main__":
    unittest.main()
