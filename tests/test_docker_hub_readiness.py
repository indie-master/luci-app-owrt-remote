"""Offline regressions for Docker HUB startup and CI failure reporting."""
from contextlib import redirect_stderr, redirect_stdout
import http.client
import io
import json
import os
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import docker_integration_ci as integration


class HubReadinessTests(unittest.TestCase):
    def test_clean_serve_initializes_auth_once_and_reports_loaded_revision(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "OWRT_REMOTE_STATE_DIR": directory,
            "OWRT_REMOTE_RUNTIME": "docker",
            "OWRT_REMOTE_BIND": "127.0.0.1",
            "OWRT_REMOTE_PORT": "18089",
        }, clear=True):
            hub = runpy.run_path(str(integration.manager.APP))
            args = hub["parser"]().parse_args(["serve"])
            globals_ = args.func.__globals__
            password_digest = Mock(wraps=hub["password_digest"])

            def verify_health():
                state = Path(directory)
                self.assertTrue((state / "hub.db").is_file())
                self.assertTrue((state / "hub-auth.json").is_file())
                self.assertTrue((state / "hub-login.txt").is_file())
                password_digest.assert_called_once()
                handler = hub["Handler"].__new__(hub["Handler"])
                handler.send_json = Mock()
                handler.path = "/health"
                handler.do_GET()
                handler.send_json.assert_called_once_with(200, {"ok": True})
                handler.send_json.reset_mock()
                handler.path = "/health?panel-update=1"
                handler.do_GET()
                handler.send_json.assert_called_once_with(200, {
                    "ok": True, "revision": integration.manager.file_hash(integration.manager.APP),
                })

            # Run the actual startup path up to the socket boundary.
            server = Mock()
            server.serve_forever.side_effect = verify_health
            make_server = Mock(return_value=server)
            with patch.dict(globals_, {"password_digest": password_digest,
                                       "make_http_server": make_server}), redirect_stdout(io.StringIO()):
                args.func(args)
            make_server.assert_called_once()
            self.assertEqual(make_server.call_args.args[1:], ("127.0.0.1", 18089))
            server.serve_forever.assert_called_once()

    def test_save_auth_preserves_existing_totp(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "OWRT_REMOTE_STATE_DIR": directory,
        }, clear=True):
            hub = runpy.run_path(str(integration.manager.APP))
            auth = hub["normalize_auth_state"]({
                "username": "owner", "totp": {"enabled": True, "secret": "JBSWY3DPEHPK3PXP"},
            })
            hub["write_json_private"](hub["AUTH_FILE"], auth)
            updated = hub["save_auth"]("owner", "synthetic-test-password")
            self.assertTrue(updated["totp"]["enabled"])
            self.assertEqual(updated["totp"], auth["totp"])
            self.assertEqual(hub["load_auth"](), updated)

    def test_failure_report_keeps_only_allowlisted_fields(self):
        secret = "synthetic-secret-do-not-emit"
        health = {"Status": "unhealthy", "FailingStreak": 3,
                  "Log": [{"ExitCode": 1, "Output": f"ConnectionRefusedError: {secret}"}]}
        responses = [
            SimpleNamespace(returncode=0, stdout="exited 1 false 2\n"),
            SimpleNamespace(returncode=0, stdout=json.dumps(health)),
            SimpleNamespace(returncode=0, stdout=f"HUB_LOGIN: {secret}\n",
                            stderr=f"PermissionError: {secret}\n"),
        ]
        output = io.StringIO()
        with patch.object(integration.subprocess, "run", side_effect=responses), \
             patch.object(http.client, "HTTPConnection") as connection, redirect_stderr(output):
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = json.dumps({
                "ok": True, "revision": secret, "token": secret,
            }).encode()
            integration.diagnose_hub("ro-ci-fixture", 18089)
        self.assertNotIn(secret, output.getvalue())
        report = json.loads(output.getvalue().removeprefix("HUB failure diagnostics "))
        self.assertEqual(report["state"]["restarts"], 2)
        self.assertEqual(report["healthcheck"]["probes"], [
            {"exit_code": 1, "signals": ["health probe failed"]},
        ])
        self.assertEqual(report["startup"], ["permission denied"])
        self.assertEqual(report["http"]["/health?panel-update=1"],
                         {"status": 200, "ok": True, "revision_matches_source": False})
