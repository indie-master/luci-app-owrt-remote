"""No-host-mutation contract tests for Belt xray.preview integration."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_host_adapter_xray_dryrun as dry


class XrayDryRunTests(unittest.TestCase):
    def test_live_preview_calls_only_existing_private_snapshot_runner(self):
        report = {"routers": 6, "wss": 5, "tcp": 1}
        with patch.object(dry.acceptance, "preflight") as start, \
             patch.object(dry.acceptance, "check_systemd") as service, \
             patch.object(dry.acceptance, "root_peer_rejected",
                          return_value=True), \
             patch.object(dry.acceptance, "restricted_probe",
                          return_value={"xray_sha256": "a" * 64}), \
             patch.object(dry.acceptance, "sha256", return_value="a" * 64), \
             patch.object(dry.acceptance, "PINNED", ROOT / "docker/host_adapter.py"), \
             patch.object(dry.synthetic, "preview",
                          return_value=report) as preview:
            result = dry.live_preview(Path("/private/archive"),
                                          "f" * 40, Path("/private"))
        self.assertEqual(result, report)
        start.assert_called_once_with("f" * 40)
        service.assert_called_once_with(
            "router-orchestrator-host-adapter.service")
        self.assertIs(preview.call_args.kwargs["live_validator"],
                      dry.send_preview_as_peer)

    def test_refuses_unverified_peer(self):
        with patch.object(dry.acceptance, "preflight"), \
             patch.object(dry.acceptance, "check_systemd"), \
             patch.object(dry.acceptance, "PINNED", ROOT / "docker/host_adapter.py"), \
             patch.object(dry.acceptance, "root_peer_rejected",
                          return_value=False), \
             patch.object(dry.synthetic, "preview") as preview:
            with self.assertRaisesRegex(dry.Blocked, "does not reject root"):
                dry.live_preview(Path("/private/archive"),
                                 "f" * 40, Path("/private"))
            preview.assert_not_called()

    def test_command_only_sends_preview_and_keeps_secrets_in_memory(self):
        text = (ROOT / "tools/orchestrator_host_adapter_xray_dryrun.py").read_text()
        self.assertIn('"op": "xray.preview"', text)
        self.assertIn("os.setuid(UID)", text)
        self.assertIn("live_validator=send_preview_as_peer", text)
        self.assertNotIn('"op": "xray.apply"', text)
        self.assertNotIn('"op": "xray.restart"', text)
        self.assertNotIn("systemctl restart", text)

    def test_safe_upgrade_only_restarts_adapter_service(self):
        text = (ROOT / "tools/orchestrator_host_adapter_readonly_upgrade.sh").read_text()
        self.assertIn("systemctl restart \"$ADAPTER\"", text)
        self.assertIn("cmp -s \"$PINNED\"", text)
        self.assertIn("before-preview-", text)
        self.assertNotIn("systemctl restart owrt-remote-xray.service", text)
        self.assertNotIn("systemctl restart owrt-remote.service", text)
        self.assertNotIn("--allow-xray-apply", text)
        self.assertNotIn("--allow-xray-restart", text)


if __name__ == "__main__":
    unittest.main()
