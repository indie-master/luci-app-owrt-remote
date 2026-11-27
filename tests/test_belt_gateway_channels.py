"""Offline tests: no production data, network, or privileged operations."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import belt_gateway_channels as gateway


class GatewayChannelsTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"id": "example", "entry_port": 18080, "ssh_entry_port": 19080}]
        self.config = {"inbounds": [
            {"tag": "entry-example", "port": 18080, "listen": "127.0.0.1", "protocol": "tunnel",
             "settings": {"allowedNetwork": "tcp", "rewriteAddress": "127.0.0.1", "rewritePort": 80}},
            {"tag": "ssh-example", "port": 19080, "listen": "127.0.0.1", "protocol": "tunnel",
             "settings": {"allowedNetwork": "tcp", "rewriteAddress": "127.0.0.1", "rewritePort": 22}}],
            "routing": {"rules": [
                {"type": "field", "inboundTag": ["entry-example"], "outboundTag": "reverse-example"},
                {"type": "field", "inboundTag": ["ssh-example"], "outboundTag": "reverse-example-ssh"}]}}

    @patch.object(gateway, "ssh_check", return_value=True)
    @patch.object(gateway, "luci_check", return_value=False)
    def test_only_confirmed_ports_and_independent_channel_results(self, luci, ssh):
        result = gateway.survey(self.rows, self.config)
        self.assertEqual(result, {"example": {"luci_http": False, "ssh_banner": True}})
        luci.assert_called_once_with(18080)
        ssh.assert_called_once_with(19080)

    def test_mismatch_fails_before_any_network(self):
        variants = []
        bad = copy.deepcopy(self.config); bad["inbounds"][0]["port"] = 4; variants.append(bad)
        bad = copy.deepcopy(self.config); bad["inbounds"][0]["listen"] = "0.0.0.0"; variants.append(bad)
        bad = copy.deepcopy(self.config); bad["inbounds"].append(copy.deepcopy(bad["inbounds"][0])); variants.append(bad)
        bad = copy.deepcopy(self.config); bad["routing"]["rules"][0]["outboundTag"] = "other"; variants.append(bad)
        bad = copy.deepcopy(self.config); bad["routing"]["rules"].insert(0, {"outboundTag": "direct"}); variants.append(bad)
        bad = copy.deepcopy(self.config); bad["routing"]["rules"][0]["domain"] = ["example.com"]; variants.append(bad)
        bad = copy.deepcopy(self.config); bad["inbounds"][0]["settings"]["rewritePort"] = 81; variants.append(bad)
        bad = copy.deepcopy(self.config); bad["routing"]["rules"][0].pop("type"); variants.append(bad)
        with patch.object(gateway, "ssh_check") as ssh, patch.object(gateway, "luci_check") as luci:
            for config in variants:
                with self.subTest(config=config), self.assertRaises(ValueError):
                    gateway.survey(self.rows, config)
            ssh.assert_not_called(); luci.assert_not_called()

    def test_invalid_id_and_duplicate_port_refused(self):
        for rows in ([{"id": "../x", "entry_port": 18080, "ssh_entry_port": 19080}],
                     [{"id": "example", "entry_port": 18080, "ssh_entry_port": 18080}]):
            with self.assertRaises(ValueError):
                gateway.confirmed_mappings(rows, self.config)

    def test_probes_fix_loopback_and_http_path(self):
        with patch.object(gateway.socket, "create_connection", side_effect=OSError) as connect:
            self.assertFalse(gateway.ssh_check(19080))
            connect.assert_called_once_with(("127.0.0.1", 19080), timeout=4)
        with patch.object(gateway.http.client, "HTTPConnection") as conn:
            conn.return_value.getresponse.return_value.status = 403
            self.assertTrue(gateway.luci_check(18080))
            conn.assert_called_once_with("127.0.0.1", 18080, timeout=4)
            conn.return_value.request.assert_called_once_with("GET", "/cgi-bin/luci/", headers={"Connection": "close"})

    def test_ssh_requires_complete_valid_identification(self):
        for chunks, expected in (([b"notice\r\nSSH-2.", b"0-dropbear\r", b"\n"], True),
                                 ([b"SSH-2.0-dropbear", b""], False),
                                 ([b"SSH-2.0-\x00bad\r\n", b""], False)):
            with self.subTest(chunks=chunks), patch.object(gateway.socket, "create_connection") as connect:
                connect.return_value.__enter__.return_value.recv.side_effect = chunks
                self.assertEqual(gateway.ssh_check(19080), expected)


if __name__ == "__main__":
    unittest.main()
