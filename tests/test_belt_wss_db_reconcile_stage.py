"""Private multi-WSS reconciliation staging tests; no host services."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "belt_wss_db_reconcile_stage.py"
FAKE_HUB = r'''#!/usr/bin/env python3
import argparse, json, sqlite3
p=argparse.ArgumentParser()
p.add_argument("--db")
p.add_argument("command")
p.add_argument("--listen-host")
p.add_argument("--listen-port")
p.add_argument("--out")
a=p.parse_args()
db=sqlite3.connect(a.db)
db.row_factory=sqlite3.Row
rows=db.execute("select * from routers order by id").fetchall()
tcp=[]; wss=[]
inbounds=[
  {"tag":"owrt-remote-vless","port":18443,"listen":"0.0.0.0",
   "settings":{"clients":tcp}},
  {"tag":"owrt-remote-wss","port":18444,"listen":"127.0.0.1",
   "settings":{"clients":wss}},
]
rules=[]
for row in rows:
 rid=row["id"]
 pair=[
  {"email":rid+"@owrt-remote","id":row["vless_uuid"]},
  {"email":rid+"-ssh@owrt-remote","id":row["ssh_vless_uuid"]}
 ]
 if row["vps_transport"]=="tcp" or row["vps_allow_legacy"]==1:
  tcp.extend(pair)
 if row["vps_transport"]=="ws":
  wss.extend(pair)
 for prefix, suffix, col in (("entry-","","entry_port"),
                            ("ssh-","-ssh","ssh_entry_port")):
  tag=prefix+rid
  inbounds.append({"tag":tag,"port":row[col],"protocol":"tunnel"})
  rules.append({"type":"field","inboundTag":[tag],
                "outboundTag":"reverse-"+rid+suffix})
data={"inbounds":inbounds,
      "outbounds":[{"tag":"direct"},{"tag":"blocked"}],
      "routing":{"rules":rules}}
with open(a.out,"w") as dst: json.dump(data,dst)
'''


class MultiWssStageTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.db = self.root / "hub.db"
        self.live = self.root / "server.json"
        self.hub = self.root / "hub.py"
        self.hub.write_text(FAKE_HUB)
        self.xray = self.root / "xray"
        self.xray.write_text('#!/bin/sh\nexit 0\n')
        self.xray.chmod(0o700)
        with sqlite3.connect(self.db) as conn:
            conn.execute("""
              create table routers(
                id text primary key, vless_uuid text, ssh_vless_uuid text,
                vps_transport text, vless_port integer,
                vps_allow_legacy integer, entry_port integer,
                ssh_entry_port integer, deleted_at integer default 0)
            """)
            for i, name in enumerate(("AX3000T-Shteinberg","Netis_N6_router5",
                                      "PaxoBs","WR3000S_2","main")):
                is_ws = name=="Netis_N6_router5"
                conn.execute(
                    "insert into routers values (?,?,?,?,?,?,?,?,0)",
                    (name,name+"-uuid",name+"-ssh-uuid",
                     "ws" if is_ws else "tcp",443 if is_ws else 18443,
                     0,18080+i*10,19080+i*10))
        # Build deployed live Xray from an isolated preview of the desired DB
        # with legacy registration preserved for three migrated routers.
        future=self.root / "future.db"
        future.write_bytes(self.db.read_bytes())
        with sqlite3.connect(future) as conn:
            conn.execute("""
                update routers
                set vps_transport='ws', vless_port=443, vps_allow_legacy=1
                where id in ('PaxoBs','WR3000S_2','main')""")
        subprocess.run([sys.executable,str(self.hub),"--db",str(future),
                        "render-xray","--listen-host","0.0.0.0",
                        "--listen-port","18443","--out",str(self.live)],check=True)

    def run_stage(self):
        return subprocess.run([
            sys.executable,str(SCRIPT),"--db",str(self.db),
            "--live",str(self.live),"--hub-source",str(self.hub),
            "--xray-bin",str(self.xray),"--stage-parent",str(self.root),
            "--migrate","PaxoBs","WR3000S_2","main",
            "--existing-wss","Netis_N6_router5",
            "--leave-tcp","AX3000T-Shteinberg"
        ],capture_output=True,text=True)

    def test_exact_match_with_legacy_fallback_does_not_write_live_db(self):
        original_db = self.db.read_bytes()
        original_live = self.live.read_bytes()
        result=self.run_stage()
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertIn("LEGACY=1: generated config parity MATCH",result.stdout)
        self.assertIn("LEGACY=0: generated config parity DIFFERS",result.stdout)
        self.assertIn("PRODUCTION: unchanged",result.stdout)
        self.assertEqual(self.db.read_bytes(),original_db)
        self.assertEqual(self.live.read_bytes(),original_live)

    def test_id_mismatch_blocks(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("update routers set vless_uuid='secret-wrong' where id='PaxoBs'")
        result=self.run_stage()
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn("secret-wrong",result.stdout+result.stderr)

    def test_missing_wss_registration_blocks(self):
        live=json.loads(self.live.read_text())
        clients=live["inbounds"][1]["settings"]["clients"]
        live["inbounds"][1]["settings"]["clients"]=[
            c for c in clients if c["email"]!="main-ssh@owrt-remote"]
        self.live.write_text(json.dumps(live))
        self.assertNotEqual(self.run_stage().returncode,0)

    def test_unexpected_inventory_blocks(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("delete from routers where id='PaxoBs'")
        self.assertNotEqual(self.run_stage().returncode,0)


if __name__ == "__main__":
    unittest.main()
