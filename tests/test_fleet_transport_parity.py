"""TCP/WSS fleet parity: no Cudy-specific assumptions in the client bundle.

Runs jq against representative reverse JSON without calling a real router,
Hub or Xray service. Fails on mixed, insecure or stale transport configs.
"""
from pathlib import Path
import importlib.util
import json
import re
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
CHECK=ROOT/"tools/router_fleet_transport_check.sh"
CLIENT=ROOT/"files/usr/sbin/owrt-remote"
INSTALLER=ROOT/"install.sh"
BUILDER=ROOT/"tools/build_router_bundle.py"

def filters():
    s=CHECK.read_text()
    patterns=re.findall(r'jq -e(?: --arg sni "\$sni" --arg path "\$ws_path")? \x27(.*?)\x27 "\$cfg"',
                        s, flags=re.S)
    if len(patterns)!=2:
        raise AssertionError("Expected exactly two embedded TCP/WSS jq checks")
    return patterns

def outbound(tag, transport):
    stream={"network":"tcp","security":"none"} if transport=="tcp" else {
      "network":"ws","security":"tls",
      "tlsSettings":{"serverName":"hub.example","allowInsecure":False,
                     "alpn":["http/1.1"]},
      "wsSettings":{"path":"/owrt-ws/","host":"hub.example"}
    }
    return {"tag":tag,"streamSettings":stream}

def cfg(a="tcp",b=None):
    return {"outbounds":[outbound("vps-interconn",a),
                         outbound("vps-ssh-interconn",b or a)]}

@unittest.skipUnless(shutil.which("jq"),"jq binary required for transport tests")
class FleetTransportParityTests(unittest.TestCase):
    def jq(self,transport,data):
        tcp,wss=filters()
        args=["jq","-e"]
        if transport=="ws":
            args += ["--arg","sni","hub.example","--arg","path","/owrt-ws/"]
        return subprocess.run([*args,tcp if transport=="tcp" else wss],
                input=json.dumps(data),text=True,capture_output=True,timeout=4)
    def test_legacy_tcp_and_independent_wss_pass(self):
        self.assertEqual(self.jq("tcp",cfg()).returncode,0)
        self.assertEqual(self.jq("ws",cfg("ws")).returncode,0)
    def test_multi_router_specific_ports_are_not_hardcoded(self):
        src=CHECK.read_text()
        self.assertNotIn("18443",src)
        self.assertNotIn('router_id" = main',src)
        self.assertIn('uci -q get owrtremote.main.vps_port',src)
        self.assertIn('case "$transport" in tcp|ws)',src)
    def test_mismatch_and_mixed_reverse_links_are_never_accepted(self):
        for transport,case in [
            ("tcp",cfg("ws")),
            ("ws",cfg("tcp")),
            ("ws",cfg("tcp","ws")),
            ("tcp",cfg("tcp","ws"))
        ]:
            with self.subTest(transport=transport,config=case):
                self.assertNotEqual(self.jq(transport,case).returncode,0)
    def test_wss_rejects_disabled_security_validation_and_wrong_sni_path(self):
        base=cfg("ws")
        for bad in ("allowInsecure","serverName","path","host","alpn"):
            test=json.loads(json.dumps(base))
            for outbound_item in test["outbounds"]:
                st=outbound_item["streamSettings"]
                if bad=="allowInsecure": st["tlsSettings"][bad]=True
                elif bad=="serverName": st["tlsSettings"][bad]="mismatch.example"
                elif bad=="alpn": st["tlsSettings"][bad]=["h2"]
                else: st["wsSettings"][bad]="wrong"
            with self.subTest(bad=bad):
                self.assertNotEqual(self.jq("ws",test).returncode,0)
    def test_only_exactly_two_unique_reverse_tags(self):
        for transport in ("tcp","ws"):
            one=cfg(transport)
            one["outbounds"].pop()
            duplicate=cfg(transport)
            duplicate["outbounds"][1]["tag"]="vps-interconn"
            extra=cfg(transport)
            extra["outbounds"].append(outbound("vps-ssh-interconn",transport))
            for case in (one,duplicate,extra):
                with self.subTest(transport=transport,n=len(case["outbounds"])):
                    self.assertNotEqual(self.jq(transport,case).returncode,0)

class FleetReleaseContracts(unittest.TestCase):
    def test_agent_retains_both_tcp_and_wss_renderers(self):
        s=CLIENT.read_text()
        self.assertIn('case "$vps_transport" in tcp|ws)',s)
        self.assertIn('if [ "$vps_transport" = ws ]; then',s)
        self.assertIn('tlsSettings',s)
        self.assertIn('wsSettings',s)
        self.assertIn('allowInsecure',s)
    def test_pinned_bundle_never_replaces_per_router_credentials_or_transport(self):
        spec=importlib.util.spec_from_file_location("fleet_builder",BUILDER)
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        paths=[entry[0] for entry in module.FILES]
        self.assertEqual(len(paths),10)
        self.assertNotIn("etc/config/owrtremote",paths)
        self.assertNotIn("etc/xray/owrt-remote-client.json",paths)
        self.assertNotIn("usr/bin/xray",paths)
    def test_legacy_installer_preserves_uci_and_blocks_wss_mismatch(self):
        s=INSTALLER.read_text()
        self.assertIn('transport_preflight()',s)
        self.assertIn('existing" = ws',s)
        self.assertIn('existing" = tcp',s)
        self.assertIn('install_config()',s)
        self.assertRegex(s,r'(?m)^transport_preflight$')
    def test_non_pilot_fleet_checker_never_changes_real_services(self):
        s=CHECK.read_text()
        for operation in ("uci set","uci commit","service restart","service stop",
                          "service signal","ubus call service add",
                          "ubus call service delete","kill -","/etc/init.d/owrt-remote",
                          "sysupgrade","opkg install"):
            self.assertNotIn(operation,s)
        self.assertIn("READ ONLY",s)

if __name__=="__main__":
    unittest.main()
