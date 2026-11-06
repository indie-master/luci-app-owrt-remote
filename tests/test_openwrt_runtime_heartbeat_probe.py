"""Contract tests for the isolated real-procd heartbeat-only rehearsal.

The hardware-specific --run mode is NEVER invoked by CI, since CI has
no OpenWrt procd. Only synthetic roots are permitted.
"""
import subprocess
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PROBE=ROOT/"tools/openwrt_runtime_heartbeat_probe.sh"
ACTIVATE=ROOT/"tools/router_heartbeat_activation.sh"

class HeartbeatRuntimeProbeContract(unittest.TestCase):
    def test_posix_syntax(self):
        for script in (PROBE,ACTIVATE):
            r=subprocess.run(["sh","-n",str(script)],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_default_and_bad_arguments_cannot_register_service(self):
        for args in ([],["--run"],["--inspect"],["--run","/does/not/exist"]):
            r=subprocess.run(["sh",str(PROBE),*args],
                capture_output=True,text=True,timeout=3)
            self.assertNotEqual(r.returncode,0)

    def test_isolated_service_and_both_activation_directions(self):
        s=PROBE.read_text()
        self.assertIn("ro-runtime-test-",s)
        self.assertIn("mktemp -d /tmp/ro-runtime-test-",s)
        self.assertIn('name="ro-runtime-test-$nonce"',s)
        self.assertIn('export RO_TEST_MODE=1',s)
        self.assertIn('export RO_TEST_ROOT="$root"',s)
        self.assertIn('export RO_TEST_SERVICE_NAME="$name"',s)
        self.assertIn('sh "$activation" --activate',s)
        self.assertIn('sh "$activation" --restore',s)
        self.assertIn('fake Xray PID unchanged',s)
        self.assertIn('keep fixture',s)
        self.assertNotIn('instance:"xray",signal:',s)
        self.assertNotIn('service state',s)
        self.assertNotIn('/etc/init.d/owrt-remote restart',s)
        self.assertNotIn('/usr/bin/ubus',s)
        self.assertIn('ubus call service delete',s)

    def test_isolated_full_lifecycle_with_fake_procd_tracks_two_heartbeats(self):
        import json
        import os
        import tempfile
        with tempfile.TemporaryDirectory(prefix="ro-probe-mock-",dir="/tmp") as d:
            home=Path(d)
            b=home/"bin"
            b.mkdir()
            db=home/"ubus.json"
            db.write_text(json.dumps({"name":"","registered":False,
                                      "xray":4070,"heartbeat":5070}))
            events=home/"events.jsonl"
            shim=b/"ubus"
            shim.write_text(r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
db=Path(os.environ["RO_PROBE_MOCK_DB"])
events=Path(os.environ["RO_PROBE_MOCK_EVENTS"])
record=json.loads(db.read_text())
if sys.argv[1:] == ["list"]:
    print("service")
    sys.exit(0)
if sys.argv[1:3] != ["call","service"]:
    sys.exit(13)
verb=sys.argv[3]
arg=json.loads(sys.argv[4])
if verb=="add":
    assert set(arg["instances"])=={"xray","heartbeat","safety"}
    assert arg["name"].startswith("ro-runtime-test-")
    record["name"]=arg["name"]
    record["registered"]=True
elif verb=="delete":
    assert record["name"]==arg["name"]
    record["registered"]=False
elif verb=="signal":
    assert arg=={"name":record["name"],"instance":"heartbeat","signal":15}
    assert record["registered"] is True
    record["heartbeat"]+=1
elif verb=="list":
    if record["registered"] and arg.get("name",record["name"])==record["name"]:
        print(json.dumps({record["name"]:{"instances":{
            "xray":{"running":True,"pid":record["xray"]},
            "heartbeat":{"running":True,"pid":record["heartbeat"]},
            "safety":{"running":True,"pid":6070}
        }}}))
    else:
        print("{}")
else:
    sys.exit(14)
with events.open("a") as f:
    f.write(json.dumps({"verb":verb,"args":arg})+"\n")
db.write_text(json.dumps(record))
if verb!="list":
    print("{}")
''')
            shim.chmod(0o700)
            env={**os.environ,
                 "PATH":str(b)+":"+os.environ.get("PATH",""),
                 "RO_PROBE_MOCK_DB":str(db),
                 "RO_PROBE_MOCK_EVENTS":str(events)}
            r=subprocess.run(["sh",str(PROBE),"--run",str(ACTIVATE)],
                             env=env,text=True,capture_output=True,timeout=15)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            self.assertIn("targeted fake heartbeat activation",r.stdout)
            self.assertIn("targeted fake heartbeat rollback activation",r.stdout)
            self.assertIn("no working instances signaled",r.stdout)
            result=json.loads(db.read_text())
            self.assertFalse(result["registered"])
            self.assertEqual(result["xray"],4070)
            self.assertEqual(result["heartbeat"],5072)
            calls=[json.loads(line) for line in events.read_text().splitlines()]
            self.assertEqual(sum(item["verb"]=="add" for item in calls),1)
            self.assertEqual(sum(item["verb"]=="delete" for item in calls),1)
            targets=[item["args"]["instance"] for item in calls if item["verb"]=="signal"]
            self.assertEqual(targets,["heartbeat","heartbeat"])

if __name__=="__main__":
    unittest.main()
