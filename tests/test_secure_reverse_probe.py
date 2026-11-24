import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"docker"))
from secure_reverse_probe import build_probe, needs_final_rules
from transport_profiles import ProfileError, SECURE_PROFILES


class ProbeTests(unittest.TestCase):
    def profile(self, transport):
        return {"name":"test","port":25401,"transport":transport,
                "server_name":"test.example.org","server_names":["test.example.org"],
                "target_host":"test.example.org","target_port":443,
                "private_key":"fixture","public_key":"A"*43,"short_ids":["aabb"],
                "certificate_file":"/etc/router-orchestrator/tls/fullchain.pem",
                "key_file":"/etc/router-orchestrator/tls/privkey.pem",
                "path":"/probe/","service_name":"probe"}

    def build(self, transport, version="Xray 26.11.9", **overrides):
        args=dict(ingress_port=25402,admin_port=25403,ssh_port=25404,
                  admin_target=25405,ssh_target=25406,core_version=version,proxy_protocol=True)
        args.update(overrides)
        return build_probe(self.profile(transport),**args)

    def test_seven_wire_profiles_and_dual_reverse_credentials(self):
        for mode in SECURE_PROFILES:
            with self.subTest(mode=mode):
                server,client=self.build(mode)
                self.assertEqual(server["outbounds"][0]["protocol"],"blackhole")
                self.assertTrue(all(i["listen"]=="127.0.0.1" for i in server["inbounds"]))
                inbound=server["inbounds"][0]
                self.assertEqual(inbound["streamSettings"]["sockopt"],{"acceptProxyProtocol":True})
                clients=inbound["settings"]["clients"]
                links=[o for o in client["outbounds"] if o["protocol"]=="vless"]
                self.assertEqual(len(links),2)
                self.assertNotEqual(clients[0]["id"],clients[1]["id"])
                for identity,link in zip(clients,links):
                    self.assertEqual(identity["id"],link["settings"]["id"])
                    self.assertEqual(identity["reverse"],link["settings"]["reverse"])
                    self.assertEqual(link["settings"]["port"],25402)
                    self.assertNotIn("sockopt",link["streamSettings"])
                self.assertNotIn("privateKey",str(client))
                for o in client["outbounds"]:
                    if o["protocol"]=="freedom":
                        self.assertEqual(o["settings"]["finalRules"][0]["ip"],["127.0.0.1/32"])
    def test_core_compatibility_and_port_collisions(self):
        self.assertFalse(needs_final_rules("Xray 26.3.27"))
        self.assertTrue(needs_final_rules("Xray 26.5.0"))
        _,client=self.build("wss",version="Xray 26.3.27")
        self.assertNotIn("finalRules",str(client))
        with self.assertRaises(ProfileError):
            self.build("wss",ssh_target=25405)
        with self.assertRaises(ProfileError):
            self.build("tcp")


if __name__=="__main__":
    unittest.main()
