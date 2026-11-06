"""The heartbeat-only activation contract never signals Xray or real procd.

This is a complete fake ubus test. Real router activation remains disabled.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

BASE=Path(__file__).resolve().parents[1]
SCRIPT=BASE/"tools/router_heartbeat_activation.sh"

MOCK_UBUS=r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
assert sys.argv[1:3] == ["call","service"]
action=sys.argv[3]
args=json.loads(sys.argv[4])
name=os.environ["RO_TEST_SERVICE_NAME"]
events=Path(os.environ["RO_MOCK_EVENTS"])
state=Path(os.environ["RO_MOCK_STATE"])
if action == "list":
    assert args["name"] == name
    p=json.loads(state.read_text())
    print(json.dumps({name:{"instances":{
      "xray":{"running":True,"pid":p["xray"]},
      "heartbeat":{"running":True,"pid":p["heartbeat"]}
    }}}))
elif action == "signal":
    assert args == {"name":name,"instance":"heartbeat","signal":15},args
    p=json.loads(state.read_text())
    p["heartbeat"]+=1
    if os.environ.get("RO_MOCK_BREAK_XRAY") == "1":
        p["xray"]+=1
    state.write_text(json.dumps(p))
    with events.open("a") as f:
        f.write(json.dumps(args)+"\n")
    print("{}")
else:
    sys.exit(9)
'''

class HeartbeatOnlyActivationTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(prefix="ro-runtime-test-",dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.router=self.root/"router"
        self.router.mkdir()
        (self.router/".ro-runtime-synthetic-root").touch()
        self.state=self.router/"etc/router-orchestrator/upgrade"
        self.state.mkdir(parents=True)
        mockbin=self.root/"bin"
        mockbin.mkdir()
        ubus=mockbin/"ubus"
        ubus.write_text(MOCK_UBUS)
        ubus.chmod(0o700)
        self.events=self.root/"events"
        self.mockstate=self.root/"state.json"
        self.mockstate.write_text(json.dumps({"xray":4000,"heartbeat":5000}))
        self.env={**os.environ,"RO_TEST_MODE":"1","RO_TEST_ROOT":str(self.router),
                  "RO_TEST_SERVICE_NAME":"ro-runtime-test-1234abcd",
                  "RO_MOCK_EVENTS":str(self.events),
                  "RO_MOCK_STATE":str(self.mockstate),
                  "PATH":str(mockbin)+":"+os.environ.get("PATH","")}

    def invoke(self,*args,env=None):
        return subprocess.run(["sh",str(SCRIPT),*args],env=env or self.env,
                              capture_output=True,text=True,timeout=8)

    def test_readonly_inspection_never_sends_signal(self):
        p=self.invoke("--inspect")
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn("xray_pid=4000 heartbeat_pid=5000",p.stdout)
        self.assertFalse(self.events.exists())

    def test_pending_activation_signals_only_heartbeat(self):
        (self.state/"pending").touch()
        p=self.invoke("--activate")
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertIn("heartbeat restarted 5000 -> 5001",p.stdout)
        self.assertIn("xray PID unchanged: 4000",p.stdout)
        self.assertEqual(json.loads(self.events.read_text()), {
             "name":"ro-runtime-test-1234abcd","instance":"heartbeat","signal":15})
        self.assertEqual(json.loads(self.mockstate.read_text())["xray"],4000)

    def test_rollback_requires_rolled_back_marker_and_restores_old_heartbeat(self):
        p=self.invoke("--restore")
        self.assertNotEqual(p.returncode,0)
        self.assertFalse(self.events.exists())
        (self.state/"rolled-back").touch()
        p=self.invoke("--restore")
        self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.assertEqual(json.loads(self.mockstate.read_text()),{
            "xray":4000,"heartbeat":5001})

    def test_detects_xray_pid_change_and_blocks_rollout(self):
        (self.state/"pending").touch()
        p=self.invoke("--activate",env={**self.env,"RO_MOCK_BREAK_XRAY":"1"})
        self.assertNotEqual(p.returncode,0)
        self.assertIn("xray PID changed",p.stderr)

    def test_cannot_activate_without_transaction_or_in_real_mode(self):
        p=self.invoke("--activate")
        self.assertNotEqual(p.returncode,0)
        self.assertFalse(self.events.exists())
        live=self.invoke("--activate",env={**os.environ,"PATH":self.env["PATH"]})
        self.assertNotEqual(live.returncode,0)
        self.assertIn("production heartbeat activation blocked",live.stderr)

    def test_runtime_activation_source_does_not_call_service_wide_restart(self):
        s=SCRIPT.read_text()
        self.assertNotIn('service state',s.split('read_status()')[1])
        self.assertNotIn('init.d/owrt-remote restart',s.split('read_status()')[1])
        self.assertIn('instance:"heartbeat",signal:15',s)
        self.assertNotIn('instance:"xray"',s)

if __name__=="__main__":
    unittest.main()
