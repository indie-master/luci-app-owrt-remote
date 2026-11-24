import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_publication import build_publication, public_summary
from transport_profiles import ProfileError


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.live = {"inbounds":[{"tag":"legacy","port":18443,"settings":{"clients":[{"id":"old"}]}}],
                     "routing":{"rules":[{"inboundTag":["legacy"],"outboundTag":"old"}]},
                     "outbounds":[{"tag":"old","protocol":"freedom"}]}
        self.profile = {"name":"remote","transport":"reality-xhttp","port":18550,
                        "server_name":"target.example.org","server_names":["target.example.org"],
                        "target_host":"other.example.org","target_port":443,
                        "private_key":"private-fixture","public_key":"A"*43,
                        "short_ids":["aabb"],"path":"/reverse/"}
        self.artifact = {"profile":self.profile,"remote_host":"dial.example.org","public_port":443}
        self.inventory = {"mode":"external-stream","public_port":443,
                          "allowed_names":["target.example.org","old.example.org"],
                          "existing_routes":[{"public_port":443,"sni":"site.example.org"}],
                          "occupied_ports":[443,18551],
                          "managed_routes":[{"public_port":443,"sni":"old.example.org","upstream":"127.0.0.1:18552"}],
                          "nginx_config_sha256":"a"*64}

    def build(self):
        return build_publication(self.live,self.artifact,self.inventory,profile_id="remote",revision=1)

    def test_additive_and_private(self):
        before = copy.deepcopy(self.live)
        plan = self.build()
        self.assertEqual(self.live,before)
        self.assertEqual(plan["candidate"]["inbounds"][:-1],before["inbounds"])
        self.assertEqual(plan["candidate"]["routing"],before["routing"])
        self.assertEqual(plan["candidate"]["outbounds"],before["outbounds"])
        inbound=plan["candidate"]["inbounds"][-1]
        self.assertEqual(inbound["listen"],"127.0.0.1")
        self.assertEqual(inbound["settings"]["clients"],[])
        self.assertEqual(plan["fragment"],"old.example.org 127.0.0.1:18552;\ntarget.example.org 127.0.0.1:18550;\n")
        self.assertNotIn("private-fixture",str(public_summary(plan)))
        self.assertFalse(plan["published"])

    def test_external_route_and_port_conflicts(self):
        self.inventory["existing_routes"].append({"public_port":443,"sni":"TARGET.example.org"})
        with self.assertRaises(ProfileError):
            self.build()
        self.inventory["existing_routes"].pop()
        self.profile["port"]=18551
        with self.assertRaises(ProfileError):
            self.build()

    def test_unapproved_name_and_recursive_target(self):
        self.profile["target_host"]="dial.example.org"
        with self.assertRaisesRegex(ProfileError,"loop"):
            self.build()
        self.profile["target_host"]="other.example.org"
        self.profile["server_names"].append("site.example.org")
        with self.assertRaises(ProfileError):
            self.build()
        self.profile["server_names"]=["target.example.org","unapproved.example.org"]
        with self.assertRaisesRegex(ProfileError,"approved"):
            self.build()

    def test_reject_inconsistent_inventory(self):
        self.inventory["managed_routes"][0]["upstream"]="127.0.0.1:18552; include /etc/passwd"
        with self.assertRaises(ProfileError):
            self.build()


if __name__ == "__main__":
    unittest.main()
