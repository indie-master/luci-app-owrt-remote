"""Offline WSS candidate-builder safety and compatibility tests."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/build_wss_candidate.py"
spec = importlib.util.spec_from_file_location("candidate", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture():
    links = [
        {"tag": tag, "protocol": "vless",
         "settings": {"address": "old.example", "port": 18443, "id": "PRIVATE-UUID"},
         "streamSettings": {"network": "tcp", "security": "none"}}
        for tag in ("vps-interconn", "vps-ssh-interconn")
    ]
    return {"routing": {"rules": [{"outboundTag": "direct"}]},
            "outbounds": links + [{"tag": "router-admin", "protocol": "freedom", "settings": {"redirect": "127.0.0.1:80"}}, {"tag": "router-ssh", "protocol": "freedom", "settings": {"redirect": "127.0.0.1:22"}}, {"tag": "direct", "protocol": "freedom"}]}


class WssCandidateTests(unittest.TestCase):
    def test_success_does_not_mutate_source_or_other_routes(self):
        source = fixture()
        result = module.build_candidate(source, host="swiftlessvpn.ru",
                                        sni="swiftlessvpn.ru", path="/owrt-ws/", port=443)
        self.assertEqual(source["outbounds"][0]["streamSettings"]["network"], "tcp")
        self.assertEqual(result["routing"], source["routing"])
        self.assertEqual(result["outbounds"][4], source["outbounds"][4])
        self.assertEqual(result["outbounds"][2]["settings"]["finalRules"],
                         [{"action": "allow", "network": "tcp", "port": 80}])
        self.assertEqual(result["outbounds"][3]["settings"]["finalRules"],
                         [{"action": "allow", "network": "tcp", "port": 22}])
        for link in result["outbounds"][:2]:
            self.assertEqual(link["settings"]["id"], "PRIVATE-UUID")
            self.assertEqual(link["settings"]["port"], 443)
            self.assertEqual(link["streamSettings"]["network"], "ws")
            self.assertEqual(link["streamSettings"]["security"], "tls")
            self.assertEqual(link["streamSettings"]["tlsSettings"]["alpn"], ["http/1.1"])
            self.assertIs(link["streamSettings"]["tlsSettings"]["allowInsecure"], False)

    def test_rejects_mixed_duplicate_missing_and_non_vless(self):
        for mode in ("mixed", "duplicate", "missing", "wrong_protocol"):
            with self.subTest(mode=mode):
                candidate = fixture()
                if mode == "mixed":
                    candidate["outbounds"][0]["streamSettings"]["network"] = "ws"
                elif mode == "duplicate":
                    candidate["outbounds"][1]["tag"] = "vps-interconn"
                elif mode == "missing":
                    candidate["outbounds"].pop(1)
                else:
                    candidate["outbounds"][0]["protocol"] = "freedom"
                with self.assertRaises(module.CandidateError):
                    module.build_candidate(candidate, host="swiftlessvpn.ru",
                                           sni="swiftlessvpn.ru", path="/owrt-ws/", port=443)

    def test_fail_closed_on_remote_redirect_or_broad_rules(self):
        for redirect in ("0.0.0.0:80", "192.168.1.1:80", "127.0.0.1:0", "127.0.0.1:100000"):
            with self.subTest(redirect=redirect):
                data = fixture()
                data["outbounds"][2]["settings"]["redirect"] = redirect
                with self.assertRaises(module.CandidateError):
                    module.build_candidate(data, host="swiftlessvpn.ru",
                                           sni="swiftlessvpn.ru", path="/owrt-ws/", port=443)
        data = fixture()
        data["outbounds"][2]["settings"]["finalRules"] = [{"action": "allow", "network": "tcp"}]
        with self.assertRaises(module.CandidateError):
            module.build_candidate(data, host="swiftlessvpn.ru",
                                   sni="swiftlessvpn.ru", path="/owrt-ws/", port=443)

    def test_output_is_private_exclusive_and_never_logs_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            src = Path(folder) / "original.json"
            out = Path(folder) / "candidate.json"
            src.write_text(json.dumps(fixture()))
            args = [sys.executable, str(SCRIPT), "--source", str(src),
                    "--out", str(out), "--host", "swiftlessvpn.ru",
                    "--sni", "swiftlessvpn.ru"]
            first = subprocess.run(args, text=True, capture_output=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("PRIVATE-UUID", first.stdout + first.stderr)
            original_content = out.read_bytes()
            second = subprocess.run(args, text=True, capture_output=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertEqual(out.read_bytes(), original_content)
            self.assertEqual(json.loads(src.read_text())["outbounds"][0]["settings"]["port"], 18443)


if __name__ == "__main__":
    unittest.main()
