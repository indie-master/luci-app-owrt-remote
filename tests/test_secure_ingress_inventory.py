import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_ingress_inventory import reviewed_inventory, read_inventory, approve_draft_names, live_tcp_ports
from transport_profiles import ProfileError


class InventoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "ingress.json"
        self.inventory = {"mode":"external-stream","public_port":443,
                          "allowed_names":["remote.example.org"],"existing_routes":[],
                          "occupied_ports":[443],"managed_routes":[],
                          "nginx_config_sha256":"a"*64}
        self.path.write_text(json.dumps(self.inventory))
        self.path.chmod(0o600)

    def test_approve_names_only_changes_inventory_not_runtime(self):
        artifact = {"profile":{"name":"test","transport":"reality-tcp","port":24001,
                   "server_name":"new.example.org","server_names":["new.example.org"],
                   "target_host":"new.example.org","target_port":443,
                   "private_key":"fixture","short_ids":["aabb"]},
                   "public_port":443,"remote_host":"dial.example.org"}
        with patch("secure_ingress_inventory.nginx_digest",return_value="a"*64), \
                patch("secure_ingress_inventory.live_tcp_ports",return_value={443}):
            result = approve_draft_names(artifact,path=self.path)
            self.assertEqual(result["approved_names"],["new.example.org"])
            self.assertFalse(result["applied"])
            current = read_inventory(self.path)
            self.assertEqual(current["allowed_names"],["new.example.org","remote.example.org"])
            self.assertEqual(current["managed_routes"],[])
            current["existing_routes"]=[{"sni":"other.example.org","public_port":443}]
            self.path.write_text(json.dumps(current))
            artifact["profile"].update(server_name="other.example.org",server_names=["other.example.org"])
            with self.assertRaises(ProfileError):
                approve_draft_names(artifact,path=self.path)
            self.assertEqual(read_inventory(self.path),current)

    def test_live_socket_ports(self):
        import subprocess
        response = subprocess.CompletedProcess([],0,"LISTEN 0 128 127.0.0.1:24001 0.0.0.0:*\nLISTEN 0 128 [::]:443 [::]:*\n","")
        with patch("secure_ingress_inventory.subprocess.run",return_value=response):
            self.assertEqual(live_tcp_ports(),{24001,443})

    def test_current_disk_inventory_required(self):
        with patch("secure_ingress_inventory.nginx_digest", return_value="a"*64):
            self.assertEqual(reviewed_inventory(self.path), self.inventory)
        with patch("secure_ingress_inventory.nginx_digest", return_value="b"*64):
            with self.assertRaisesRegex(ProfileError, "changed"):
                reviewed_inventory(self.path)

    def test_reject_world_accessible_or_symlink_inventory(self):
        self.path.chmod(0o644)
        with self.assertRaises(ProfileError):
            read_inventory(self.path)
        self.path.chmod(0o600)
        link = self.path.with_name("link")
        link.symlink_to(self.path)
        with self.assertRaises(ProfileError):
            reviewed_inventory(link)


if __name__ == "__main__":
    unittest.main()
