"""Synthetic, credential-free tests for generic new WSS enrollment on Belt."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import belt_new_wss_enroll as tool


RID = "Movietonn"
ROW = {
    "vps_transport": "ws", "vless_port": 443, "vps_allow_legacy": 0,
    "vless_uuid": "11111111-1111-4111-8111-111111111111",
    "ssh_vless_uuid": "22222222-2222-4222-8222-222222222222",
    "entry_port": 18130, "ssh_entry_port": 19130,
    "admin_host": "127.0.0.1", "admin_port": 80,
    "ssh_host": "127.0.0.1", "ssh_port": 22,
}


def candidate():
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"tag": "owrt-remote-vless", "port": 18443, "settings": {"clients": []}},
            {"tag": "owrt-remote-wss", "port": 18444, "listen": "127.0.0.1",
             "settings": {"clients": [
                 {"id": "old-uuid", "email": "old@owrt-remote"},
                 {"id": ROW["vless_uuid"], "email": "Movietonn@owrt-remote"},
                 {"id": ROW["ssh_vless_uuid"], "email": "Movietonn-ssh@owrt-remote"},
             ]}, "streamSettings": {"network": "ws",
                                    "wsSettings": {"path": "/owrt-ws/"}}},
            {"tag": "entry-old", "listen": "127.0.0.1", "port": 18090,
             "protocol": "tunnel", "settings": {"allowedNetwork": "tcp",
             "rewriteAddress": "127.0.0.1", "rewritePort": 80}},
            {"tag": "entry-Movietonn", "listen": "127.0.0.1", "port": 18130,
             "protocol": "tunnel", "settings": {"allowedNetwork": "tcp",
             "rewriteAddress": "127.0.0.1", "rewritePort": 80}},
            {"tag": "ssh-Movietonn", "listen": "127.0.0.1", "port": 19130,
             "protocol": "tunnel", "settings": {"allowedNetwork": "tcp",
             "rewriteAddress": "127.0.0.1", "rewritePort": 22}},
        ],
        "outbounds": [{"tag": "direct", "protocol": "freedom"}],
        "routing": {"rules": [
            {"type": "field", "inboundTag": ["entry-old"],
             "outboundTag": "reverse-old"},
            {"type": "field", "inboundTag": ["entry-Movietonn"],
             "outboundTag": "reverse-Movietonn"},
            {"type": "field", "inboundTag": ["ssh-Movietonn"],
             "outboundTag": "reverse-Movietonn-ssh"},
        ]},
    }


class WssNewRouterEnrollment(unittest.TestCase):
    def setUp(self):
        self.new = candidate()
        self.live = tool.target_removed(self.new, RID, ROW)

    def test_exact_addition_is_allowed(self):
        self.assertEqual(tool.check_candidate(self.live, self.new, ROW, RID), "new")

    def test_already_registered_is_noop(self):
        self.assertEqual(tool.check_candidate(self.new, self.new, ROW, RID),
                         "already-registered")

    def test_existing_registration_or_outbound_change_blocks(self):
        for case in ("one-uuid", "outbound", "old-route"):
            modified = copy.deepcopy(self.live)
            if case == "one-uuid":
                new_user = next(c for c in self.new["inbounds"][1]["settings"]["clients"]
                                if c["email"] == "Movietonn@owrt-remote")
                modified["inbounds"][1]["settings"]["clients"].append(new_user)
            elif case == "outbound":
                modified["outbounds"].append({"tag": "rogue"})
            elif case == "old-route":
                modified["routing"]["rules"][0]["outboundTag"] = "wrong"
            with self.subTest(case=case), self.assertRaises(tool.Blocked):
                tool.check_candidate(modified, self.new, ROW, RID)

    def test_extra_router_in_generated_config_is_rejected(self):
        modified = copy.deepcopy(self.new)
        modified["inbounds"].append({
            "tag": "entry-unknown", "listen": "127.0.0.1", "port": 19999,
            "protocol": "tunnel", "settings": {"allowedNetwork": "tcp",
            "rewriteAddress": "127.0.0.1", "rewritePort": 80}})
        with self.assertRaises(tool.Blocked):
            tool.check_candidate(self.live, modified, ROW, RID)

    def test_uuid_reverse_destination_and_transport_mismatches_block(self):
        for case in ("uuid", "port", "path", "route", "transport", "legacy"):
            modified = copy.deepcopy(self.new)
            row = dict(ROW)
            if case == "uuid":
                modified["inbounds"][1]["settings"]["clients"][1]["id"] = "WRONG"
            elif case == "port":
                next(x for x in modified["inbounds"]
                     if x["tag"] == "entry-Movietonn")["port"] = 18131
            elif case == "path":
                modified["inbounds"][1]["streamSettings"]["wsSettings"]["path"] = "/different"
            elif case == "route":
                modified["routing"]["rules"][1]["outboundTag"] = "wrong"
            elif case == "transport":
                row["vps_transport"] = "tcp"
            elif case == "legacy":
                row["vps_allow_legacy"] = 1
            with self.subTest(case=case), self.assertRaises(tool.Blocked):
                tool.check_candidate(self.live, modified, row, RID)

    def test_duplicate_wss_identity_is_rejected(self):
        modified = copy.deepcopy(self.new)
        modified["inbounds"][1]["settings"]["clients"][2]["email"] = "Movietonn@owrt-remote"
        with self.assertRaises(tool.Blocked):
            tool.check_candidate(self.live, modified, ROW, RID)


if __name__ == "__main__":
    unittest.main()
