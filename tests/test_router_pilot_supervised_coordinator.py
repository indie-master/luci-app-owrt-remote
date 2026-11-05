"""Synthetic coordinator contract: fake ubus, full transaction fixture only.

Never registers a real OpenWrt procd service or writes the actual /etc.
"""
from pathlib import Path
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
COORD=ROOT/"tools/router_pilot_procd_coordinator.sh"
TXN=ROOT/"tools/router_pilot_transaction.sh"
SECONDARY=ROOT/"tools/router_pilot_secondary_guard.sh"
TESTFILE=ROOT/"tests/test_router_pilot_transaction.py"

def fixture_class():
    spec=importlib.util.spec_from_file_location("coordinator_fixture",TESTFILE)
    mod=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.RouterPilotTransactionTests

class SupervisedCoordinatorTests(unittest.TestCase):
    def setUp(self):
        cls=fixture_class()
        self.fx=cls("test_manual_rollback_restores_every_file")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.fx.prepared()
        (self.fx.state/"secondary-guard.sh").write_bytes(SECONDARY.read_bytes())
        self.mockdir=self.fx.temp/"fake-bin"
        self.mockdir.mkdir()
        self.calls=self.fx.temp/"ubus-calls.jsonl"
        script=self.mockdir/"ubus"
        script.write_text("""#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
state=Path(os.environ["RO_COORD_TEST_STATE"])
events=Path(os.environ["RO_COORD_TEST_EVENTS"])
assert sys.argv[1]=="call" and sys.argv[2]=="service"
cmd=sys.argv[3]
args=json.loads(sys.argv[4])
with events.open("a") as f:
    f.write(json.dumps({"action":cmd,"args":args})+"\\n")
if cmd=="add":
    assert args["name"].startswith("ro-pilot-supervised-")
    assert set(args["instances"])=={"watchdog","secondary","safety_cleanup"}
    (state/"watchdog-ready").touch()
    (state/"secondary-ready").touch()
    print("{}")
elif cmd=="list":
    name=args["name"]
    print(json.dumps({name:{"instances":{
        "watchdog":{"running":True},"secondary":{"running":True}
    }}}))
elif cmd=="delete":
    print("{}")
else:
    sys.exit(1)
""")
        script.chmod(0o700)
        self.env={**self.fx.env,
                  "PATH":str(self.mockdir)+":"+os.environ.get("PATH",""),
                  "RO_COORD_TEST_STATE":str(self.fx.state),
                  "RO_COORD_TEST_EVENTS":str(self.calls)}
        arm=self.fx.run_cmd(TXN,"arm-supervised",self.fx.digest,
                            env={**self.env,"RO_PILOT_TIMEOUT":"20"})
        self.assertEqual(arm.returncode,0,arm.stdout+arm.stderr)

    def coordinator(self,*args):
        return subprocess.run(["sh",str(COORD),*args],
                              env=self.env, text=True,
                              capture_output=True,timeout=10)

    def test_start_two_workers_then_apply_restore_and_stop(self):
        self.assertFalse((self.fx.state/"pending").exists())
        r=self.coordinator("start")
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertIn("both supervised workers ready",r.stdout)
        events=[json.loads(line) for line in self.calls.read_text().splitlines()]
        add=next(ev["args"] for ev in events if ev["action"]=="add")
        self.assertEqual(add["instances"]["watchdog"]["respawn"],[3600,2,0])
        self.assertEqual(add["instances"]["secondary"]["respawn"],[3600,2,0])
        self.assertEqual(add["instances"]["watchdog"]["env"]["RO_TEST_ROOT"],str(self.fx.router))
        self.assertEqual(add["instances"]["secondary"]["env"]["RO_TEST_MODE"],"1")
        self.assertEqual(add["instances"]["watchdog"]["command"][-1],"watchdog")
        applied=self.fx.run_cmd(TXN,"apply-supervised",self.fx.digest,env=self.env)
        self.assertEqual(applied.returncode,0,applied.stdout+applied.stderr)
        self.assertTrue((self.fx.state/"pending").exists())
        refuse=self.coordinator("stop")
        self.assertNotEqual(refuse.returncode,0)
        self.assertIn("pending transaction",refuse.stderr)
        restored=self.fx.run_cmd(TXN,"rollback",env=self.env)
        self.assertEqual(restored.returncode,0,restored.stdout+restored.stderr)
        stopped=self.coordinator("stop")
        self.assertEqual(stopped.returncode,0,stopped.stdout+stopped.stderr)
        self.assertFalse((self.fx.state/"procd-service-name").exists())
        self.fx.restore_assertions()
        events=[json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual(sum(x["action"]=="delete" for x in events),1)

    def test_coordinator_cannot_register_before_arm_or_without_second_guard(self):
        (self.fx.state/"armed").unlink()
        out=self.coordinator("start")
        self.assertNotEqual(out.returncode,0)
        self.assertIn("arm-supervised",out.stderr)
        self.assertFalse(self.calls.exists())

    def test_real_root_rejected_even_in_test_mode(self):
        env={**self.env,"RO_TEST_ROOT":"/"}
        r=subprocess.run(["sh",str(COORD),"start"],
                         env=env,text=True,capture_output=True,timeout=4)
        self.assertNotEqual(r.returncode,0)
        self.assertIn("not a permitted temporary fake router",r.stderr)

if __name__=="__main__":
    unittest.main()
