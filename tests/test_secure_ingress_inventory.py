import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_ingress_inventory import reviewed_inventory, read_inventory
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
