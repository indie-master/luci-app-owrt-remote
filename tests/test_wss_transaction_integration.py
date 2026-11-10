"""Isolated stateful migration tests: no real /etc, no network and no Xray."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "tools/router_wss_pilot_transaction.sh").read_text()
SHELL = shutil.which("busybox") or "/bin/sh"


class MigrationIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wss-transaction-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.agent = self.root / "owrt-remote"
        self.candidate_agent = self.root / "candidate-agent"
        self.uci = self.root / "live-uci"
        self.json_file = self.root / "live-xray.json"
        self.tx = self.root / "installed-transaction.sh"
        self.init = self.root / "installed-recovery.init"
        self.rc_link = self.root / "boot-symlink"
        self.download_tx = self.root / "download-transaction.sh"
        self.download_init = self.root / "download-recovery.init"
        self.service = self.root / "service"
        self.fake_xray = self.root / "xray"
        self.original = "router_id=sample-router\nvps_port=18443\nvps_transport=tcp\nxray_bin=" + str(self.fake_xray) + "\n"
        self.candidate = self.original.replace("18443", "443").replace("vps_transport=tcp", "vps_transport=ws")
        self.uci.write_text(self.original)
        self.agent.write_text("#!/bin/sh\necho legacy\n")
        self.candidate_agent.write_text("#!/bin/sh\n# OWRT_REMOTE_UCI_CONFIG_DIR\necho updated\n")
        self.agent.chmod(0o700)
        self.candidate_agent.chmod(0o700)
        self.fake_xray.write_text("#!/bin/sh\nexit 0\n")
        self.fake_xray.chmod(0o700)
        links = []
        for name, uuid in (("vps-interconn", "uuid-one"), ("vps-ssh-interconn", "uuid-two")):
            links.append({"tag": name, "settings": {"id": uuid, "encryption": "none",
                          "reverse": {"tag": name + "-reverse"}, "flow": "" },
                          "streamSettings": {"network": "tcp", "security": "none"}})
        original_json = {"outbounds": links}
        candidate_json = json.loads(json.dumps(original_json))
        for link in candidate_json["outbounds"]:
            link["settings"]["port"] = 443
            link["streamSettings"] = {"network": "ws", "security": "tls",
                                      "tlsSettings": {"allowInsecure": False}}
        self.json_file.write_text(json.dumps(original_json))
        (self.stage / "original-uci").write_text(self.original)
        (self.stage / "candidate-uci").write_text(self.candidate)
        (self.stage / "original-xray.json").write_text(json.dumps(original_json))
        (self.stage / "candidate-xray.json").write_text(json.dumps(candidate_json))
        self.sh("cd \"$1\" && sha256sum original-uci original-xray.json candidate-uci candidate-xray.json > SHA256SUMS", self.stage)
        uci_mock = self.bin / "uci"
        uci_mock.write_text(f"""#!/bin/sh
case "$1 $2 $3" in
 "-q get owrtremote.main.router_id") sed -n 's/^router_id=//p' '{self.uci}' ;;
 "-q get owrtremote.main.vps_port") sed -n 's/^vps_port=//p' '{self.uci}' ;;
 "-q get owrtremote.main.vps_transport") sed -n 's/^vps_transport=//p' '{self.uci}' ;;
 "-q get owrtremote.main.xray_bin") sed -n 's/^xray_bin=//p' '{self.uci}' ;;
 *) exit 1 ;;
esac
""")
        uci_mock.chmod(0o700)
        self.service.write_text("#!/bin/sh\necho restart >> '" + str(self.root / "service-calls") + "'\n")
        self.service.chmod(0o700)
        self.download_init.write_text(f"""#!/bin/sh
case "$1" in
 enable) ln -sf '{self.init}' '{self.rc_link}' ;;
 start) touch '{self.stage}/ready' ;;
 stop) : ;;
 *) exit 1 ;;
esac
""")
        self.download_init.chmod(0o700)
        patched = SOURCE
        for old, new in [
            ("ST=/etc/router-orchestrator/wss-pilot", f"ST={self.stage}"),
            ("UCI=/etc/config/owrtremote", f"UCI={self.uci}"),
            ("JSON=/etc/xray/owrt-remote-client.json", f"JSON={self.json_file}"),
            ("AGENT=/usr/sbin/owrt-remote", f"AGENT={self.agent}"),
            ("TX=/usr/libexec/ro-wss-transaction", f"TX={self.tx}"),
            ("INIT=/etc/init.d/ro-wss-recovery", f"INIT={self.init}"),
            ("/etc/rc.d/S08ro-wss-recovery", str(self.rc_link)),
            ("/etc/init.d/owrt-remote", str(self.service)),
            ("/etc/init.d/ro-wss-recovery", str(self.init)),
            ("/tmp/ro-wss-transaction.sh", str(self.download_tx)),
            ("/tmp/ro-wss-recovery.init", str(self.download_init)),
            ("mkdir -p /usr/libexec", f"mkdir -p '{self.root}'"),
            ("df -Pk /overlay", f"df -Pk '{self.root}'"),
            ('[ "$(id -u)" -eq 0 ]', "[ 0 -eq 0 ]"),
        ]:
            patched = patched.replace(old, new)
        self.download_tx.write_text(patched)
        self.download_tx.chmod(0o700)
        self.env = dict(os.environ, PATH=str(self.bin) + ":" + os.environ["PATH"])
        self.assertEqual(self.invoke("status").returncode, 0)

    def sh(self, source, *args):
        return subprocess.run(["/bin/sh", "-c", source, "_", *map(str, args)],
                              capture_output=True, text=True, check=True)

    def invoke(self, *args):
        return subprocess.run(["/bin/sh", str(self.download_tx), *args],
                              capture_output=True, text=True, env=self.env, timeout=15)

    def prepared(self):
        result = self.invoke("prepare", str(self.candidate_agent))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uci.read_text(), self.original)
        self.assertTrue(self.rc_link.is_symlink())

    def applied(self):
        self.prepared()
        result = self.invoke("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uci.read_text(), self.candidate)
        self.assertTrue((self.stage / "pending").exists())

    def assert_recovered(self):
        self.assertEqual(self.uci.read_text(), self.original)
        self.assertEqual(self.agent.read_text(), "#!/bin/sh\necho legacy\n")
        self.assertEqual(self.json_file.read_text(), (self.stage / "original-xray.json").read_text())
        self.assertFalse((self.stage / "pending").exists())

    def test_prepare_only_does_not_touch_running_transport(self):
        self.prepared()
        self.assertFalse((self.stage / "pending").exists())
        self.assertFalse((self.root / "service-calls").exists())

    def test_confirm_keeps_wss(self):
        self.applied()
        result = self.invoke("confirm", "sample-router")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uci.read_text(), self.candidate)
        self.assertFalse((self.stage / "pending").exists())

    def test_deadline_rolls_back_original_18443(self):
        self.applied()
        (self.stage / "deadline").write_text("0\n")
        result = self.invoke("watchdog")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_recovered()

    def test_reboot_reverts_even_partial_install(self):
        self.applied()
        self.agent.write_text("half installed\n")
        self.uci.write_text("broken partial write\n")
        result = self.invoke("boot-recover")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_recovered()

    def test_credential_mismatch_refused_without_modifying_tcp(self):
        data = json.loads((self.stage / "candidate-xray.json").read_text())
        data["outbounds"][0]["settings"]["id"] = "MISMATCH"
        (self.stage / "candidate-xray.json").write_text(json.dumps(data))
        self.sh("cd \"$1\" && sha256sum original-uci original-xray.json candidate-uci candidate-xray.json > SHA256SUMS", self.stage)
        result = self.invoke("prepare", str(self.candidate_agent))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("credentials or tags changed", result.stderr)
        self.assertEqual(self.uci.read_text(), self.original)


if __name__ == "__main__":
    unittest.main()
