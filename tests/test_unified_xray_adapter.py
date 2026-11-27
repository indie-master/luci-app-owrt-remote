"""Offline transaction and IPC contract regressions using private synthetic state."""

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import signal
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

from test_docker_stack_runtime import config_document
from test_orchestrator_stack_config import fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker"))
import manager as m
from docker_stack_integration_ci import (AdapterCheckFailed, adapter_check, adapter_failure_sites, hub_check)
from stack_runtime import OWNER, REVISION, Stack
from unified_xray_adapter import MAX_REQUEST, XRAY_TMPFS, Adapter, parameters, read_request
from unified_transport_runtime import Runtime as TransportRuntime, receipt
from secure_profile_store import connection, load_artifact
from unified_xray_state import XrayState, assignment_id, encode

HUB_SPEC = importlib.util.spec_from_file_location("unified_xray_test_hub", ROOT / "vps/owrt-remote-hub.py")
hub = importlib.util.module_from_spec(HUB_SPEC)
sys.modules[HUB_SPEC.name] = hub
with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"OWRT_REMOTE_STATE_DIR": directory}):
    HUB_SPEC.loader.exec_module(hub)


def router(port=30100):
    return {"id": "demo", "clients": [str(uuid.uuid4()), str(uuid.uuid4())],
            "ports": [port, port + 1], "targets": [["127.0.0.1", 80], ["127.0.0.1", 22]]}


class CIDiagnosticsTests(unittest.TestCase):
    def test_adapter_failure_sites_excludes_private_data_and_preserves_cause(self):
        try:
            try:
                exec(compile("raise TypeError('private-inner')",
                             str(ROOT / "docker/unified_transport_runtime.py"), "exec"), {})
            except TypeError as cause:
                exec(compile("raise KeyError('private-outer') from cause",
                             str(ROOT / "docker/unified_xray_state.py"), "exec"),
                     {"cause": cause})
        except KeyError as error:
            detail = adapter_failure_sites(error)
        else:
            self.fail("expected chained exception")
        self.assertEqual(detail, "KeyError@docker/unified_xray_state.py:1;"
                                 "TypeError@docker/unified_transport_runtime.py:1")
        try:
            exec(compile("raise ValueError('private-value')",
                         "/private/secret-path.py", "exec"), {})
        except ValueError as error:
            self.assertEqual(adapter_failure_sites(error), "ValueError@unknown")


    def test_runtime_error_reports_only_a_known_ipc_outcome(self):
        body = "with check('profile-lifecycle', 'publish'):\n    ipc('transport.publish')"
        for message, outcome in (
            ("Xray adapter rejected the operation", "ipc_rejected"),
            ("Xray adapter unavailable", "ipc_unavailable"),
            ("Invalid Xray response", "ipc_invalid_response"),
            ("Xray adapter peer verification failed", "ipc_peer_rejected"),
            ("private runtime detail", "runtime_error"),
        ):
            def execute_child(*args, **kwargs):
                program = args[-1]
                result = subprocess.run(
                    [sys.executable, "-c", "import runpy\n"
                     f"def ipc(*args, **kwargs): raise RuntimeError({message!r})\n"
                     "runpy.run_path = lambda _: {'unified_xray_request': ipc}\n" + program],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.stderr, "")
                result.check_returncode()

            with self.subTest(outcome=outcome), patch(
                "docker_stack_integration_ci.docker", side_effect=execute_child,
            ), self.assertRaises(AdapterCheckFailed) as caught:
                hub_check("fixture", body)
            self.assertEqual(str(caught.exception),
                             f"stage=profile-lifecycle check=publish error={outcome}")

    def test_http_status_survives_failed_docker_exec(self):
        body = "with check('registration', 'create-wss'):\n    pass"
        error = subprocess.CalledProcessError(
            1, ["docker", "sensitive-command"],
            output="RO_ADAPTER_CHECK stage=registration check=create-wss expected_http=200 actual_http=409\n",
            stderr="sensitive-stderr",
        )
        with patch("docker_stack_integration_ci.docker", side_effect=error) as docker:
            with self.assertRaises(AdapterCheckFailed) as caught:
                hub_check("fixture", body)
        self.assertEqual(str(caught.exception),
                         "stage=registration check=create-wss expected_http=200 actual_http=409")
        compile(docker.call_args.args[-1], "<container-check>", "exec")

    def test_untrusted_child_output_is_never_forwarded(self):
        body = "with check('registration', 'create-wss'):\n    pass"
        for output in ("RO_ADAPTER_CHECK stage=registration check=undeclared error=timeout\n",
                       "RO_ADAPTER_CHECK stage=registration check=create-wss error=timeout\nsensitive-output"):
            error = subprocess.CalledProcessError(1, ["docker", "sensitive-command"],
                                                  output=output, stderr="sensitive-stderr")
            with self.subTest(output=output), patch("docker_stack_integration_ci.docker", side_effect=error):
                with self.assertRaises(AdapterCheckFailed) as caught:
                    with adapter_check("hub-api", "exec"):
                        hub_check("fixture", body)
                self.assertEqual(str(caught.exception), "stage=hub-api check=exec error=command_exit=1")
                self.assertTrue(caught.exception.__suppress_context__)


class ProfileProbeMergeTests(unittest.TestCase):
    def test_existing_untagged_inbounds_and_unique_target(self):
        item = {"profile": {"name": "fixture"}, "revision": 1,
                "public_port": 9443, "remote_host": "127.0.0.1"}
        baseline = {"port": 18090, "settings": {"clients": []}}
        target = {"tag": "ro-profile-fixture-1", "port": 30123,
                  "settings": {"clients": []}}
        probe = {"inbounds": [{"settings": {"clients": [{"id": "test-client"}]}},
                              {"tag": "probe-fixture", "port": 31001}],
                 "routing": {"rules": []}}
        for entries, unique in (
            ([baseline, target], True),
            ([baseline], False),
            ([baseline, target, target], False),
        ):
            with self.subTest(count=len(entries), unique=unique):
                adapter, state = Mock(), Mock()
                adapter.test_config.side_effect = m.Blocked("test validation boundary")
                runtime = TransportRuntime(adapter, Mock(), {}, state, "fixture", Mock())
                data = encode({"inbounds": entries, "routing": {"rules": []}})
                sockets = [Mock() for _ in range(4)]
                for sock, port in zip(sockets, (31001, 31002, 31003, 31004)):
                    sock.getsockname.return_value = ("127.0.0.1", port)
                with (
                    patch("unified_transport_runtime.reserve", side_effect=sockets),
                    patch("unified_transport_runtime.Proof"),
                    patch("unified_transport_runtime.build_probe",
                          return_value=(probe, {"outbounds": []})),
                ):
                    with self.assertRaisesRegex(
                        m.Blocked, "test validation boundary" if unique else
                        "Expected exactly one profile probe listener",
                    ):
                        runtime.prove({"routers": {}}, [item], data, item,
                                      {"token": "b" * 32})
                state.save.assert_not_called()
                adapter.restart.assert_not_called()
                self.assertEqual(json.loads(data)["inbounds"], entries)
                if unique:
                    adapter.test_config.assert_called_once()
                    trial = json.loads(adapter.test_config.call_args.args[2])
                    self.assertEqual(trial["inbounds"][0], baseline)
                    self.assertEqual(trial["inbounds"][1]["settings"]["clients"],
                                     [{"id": "test-client"}])
                    self.assertEqual(trial["inbounds"][2], probe["inbounds"][1])
                else:
                    adapter.test_config.assert_not_called()


class WebAuthorizationTests(unittest.TestCase):
    def test_stage_and_approval_require_owner_then_csrf(self):
        for operation in ("stage", "approve"):
            handler = Mock()
            handler.parsed.return_value.path = "/api/secure-profiles/" + operation
            handler.require_owner.return_value = False
            with patch.object(hub, "unified_xray_enabled", return_value=True):
                hub.Handler.do_POST(handler)
            handler.require_owner.assert_called_once_with(json_mode=True)
            handler.post_unified_transport_catalog.assert_not_called()
            handler.unified_xray_csrf.return_value = False
            with patch.object(hub, "unified_xray_request") as request:
                hub.Handler.post_unified_transport_catalog(handler, operation)
            request.assert_not_called()
            handler.read_payload.assert_not_called()

    def test_malformed_csrf_header_receives_403(self):
        handler = Mock(headers={"X-Unified-Xray-CSRF": "\xff"})
        self.assertFalse(hub.Handler.unified_xray_csrf(handler))
        handler.send_json.assert_called_once_with(403, {"ok": False, "error": "invalid CSRF or origin"})

    def test_unified_transport_page_is_owner_only_and_uses_unified_csrf(self):
        for path in ("/secure-profiles/", "/transport-configurator/"):
            with self.subTest(path=path), patch.object(hub, "unified_xray_enabled", return_value=True):
                handler = Mock()
                handler.parsed.return_value.path = path
                handler.require_owner.return_value = False
                hub.Handler.do_GET(handler)
                handler.send_bytes.assert_not_called()
                handler.require_owner.return_value = True
                hub.Handler.do_GET(handler)
                page = handler.send_bytes.call_args.args[1].decode()
                self.assertIn('data-csrf="' + hub.UNIFIED_XRAY_CSRF + '"', page)
                self.assertIn("X-Unified-Xray-CSRF", page)
                self.assertNotIn("/api/transports/router/pilot", page)

    def test_unified_delete_rejects_boolean_confirmation(self):
        handler = Mock()
        handler.read_payload.return_value = {"profile_id": "demo", "revision": 1, "confirmed": True}
        with patch.object(hub, "unified_xray_request") as request:
            hub.Handler.post_unified_transport_catalog(handler, "delete")
        request.assert_not_called()
        self.assertEqual(handler.send_json.call_args.args[0], 400)


class ContractTests(unittest.TestCase):
    def test_stage_and_approval_roles_have_distinct_typed_payloads(self):
        ref = {"profile_id": "demo", "revision": 1}
        self.assertEqual(parameters({"op": "transport.stage", **ref}), ("transport.stage", ref))
        approval = dict(ref, stage_sha256="a" * 64, confirmed=True)
        self.assertEqual(parameters({"op": "transport.approve", **approval}),
                         ("transport.approve", approval))
        for changes in ({"confirmed": 1}, {"confirmed": False}, {"revision": True},
                        {"profile_id": "../other"}, {"stage_sha256": "invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                parameters(dict({"op": "transport.approve", **approval}, **changes))
        self.assertEqual(parameters({"op": "transport.publish", **approval}),
                         ("transport.publish", approval))
        for operation in ("activate", "disable", "rollback"):
            name = "transport." + operation
            self.assertEqual(parameters({"op": name, **ref, "confirmed": True}),
                             (name, dict(ref, confirmed=True)))
            with self.assertRaises(ValueError):
                parameters({"op": name, **ref, "confirmed": False})
        with self.assertRaises(m.Blocked):
            parameters({"op": "transport.publish", **ref, "confirmed": True})

    def test_allowlist(self):
        for op in ("status", "health", "version", "restart"):
            self.assertEqual(parameters({"op": op}), (op, {}))
        for request in ({"op": "shell"}, {"op": "restart", "container": "other"},
                        {"op": "apply_config", "config": {}}, {"op": "status", "path": "/tmp"},
                        {"op": "rollback", "transaction": "../x"}, {"op": ["restart"]},
                        {"op": "validate_config", "router_id": "demo", "router": dict(router(), ports=[True, 1])},
                        {"op": "validate_config", "router_id": "demo", "router": dict(router(), targets=[["/tmp", 80], ["localhost", 22]])}):
            with self.subTest(op=request["op"]), self.assertRaises(m.Blocked):
                parameters(request)

    def test_assignment_requires_typed_distinct_revisions(self):
        one = {"profile_id": "demo", "revision": 1}
        request = {"op": "transport.assign", "router_id": "router-a", "profiles": [one], "confirmed": True}
        self.assertEqual(parameters(request)[0], "transport.assign")
        for invalid in ([], [one, one], [dict(one, revision=True)], [dict(one, port=8080)]):
            with self.assertRaises(ValueError):
                parameters(dict(request, profiles=invalid))

    def test_ipc_framing_size_and_single_request(self):
        for parts in ([b"x" * MAX_REQUEST], [b'{}\n{}\n'], [b'{"op":"status"}', b""]):
            channel = Mock()
            channel.recv.side_effect = parts
            with self.subTest(parts=len(parts)), self.assertRaises(m.Blocked):
                read_request(channel)
        channel = Mock()
        channel.recv.side_effect = [b'{"op":', b'"status"}\n']
        self.assertEqual(read_request(channel), {"op": "status"})

    def test_unauthorized_socket_peer_reads_no_payload(self):
        adapter = Adapter(Path("/unused"))
        channel = Mock()
        channel.getsockopt.return_value = struct.pack("3i", 123, 21000, m.HUB_GID)
        with patch.object(adapter, "open_project") as open_project:
            adapter.serve(channel)
        channel.recv.assert_not_called()
        channel.sendall.assert_not_called()
        open_project.assert_not_called()

    def test_correct_uid_outside_hub_reads_no_payload(self):
        adapter = Adapter(Path("/unused"))
        channel = Mock()
        channel.getsockopt.return_value = struct.pack("3i", 123, m.HUB_UID, m.HUB_GID)
        with patch.object(adapter, "open_project", return_value=(None, None, {"hub": {}})),              patch("unified_xray_adapter.peer_is_hub", return_value=False):
            adapter.serve(channel)
        channel.recv.assert_not_called()
        channel.sendall.assert_not_called()

    def test_hub_only_cannot_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            config = fixture(Path(folder), "external-only")
            base = {key: config[key] for key in m.ALLOWED}
            m.validate_settings(base)
            with self.assertRaises(m.Blocked):
                m.validate_settings(dict(base, RO_XRAY_BACKEND="unified_adapter"))

    def test_lock_contention(self):
        if os.geteuid() != 0:
            self.skipTest("Root ownership required by the real deployment lock")
        with tempfile.TemporaryDirectory() as folder, patch.object(m, "LOCK", Path(folder) / ".deploy.lock"):
            with m.deployment_lock(), self.assertRaisesRegex(m.Blocked, "Another"):
                with m.deployment_lock():
                    self.fail("a concurrent operation took the deployment lock")


@unittest.skipUnless(os.geteuid() == 0, "Root ownership required by production path validation")
class TransactionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.env = self.root / "stack.env"
        config = fixture(self.root, "external-only")
        config["RO_XRAY_BACKEND"] = "unified_adapter"
        self.stack = Stack(self.env, config)
        self.state = XrayState(self.stack)
        self.stack.state.mkdir()
        self.state.config.parent.mkdir(mode=0o755)
        (self.root / "assets").mkdir(mode=0o755)
        (self.root / "tls").mkdir(mode=0o755)
        base = config_document()
        base["inbounds"][0]["settings"] = {"clients": [], "decryption": "none"}
        self.baseline = encode(base)
        self.state.config.write_bytes(self.baseline)
        self.state.config.chmod(0o640)
        self.release = {"revision": "revision", "images": {"xray": "sha256:" + "1" * 64}}
        self.record = {
            "Image": self.release["images"]["xray"], "Id": "b" * 64, "Name": "/" + self.stack.name + "-xray",
            "HostConfig": {"NetworkMode": "host", "ReadonlyRootfs": True, "Privileged": False,
                           "Tmpfs": dict(XRAY_TMPFS)},
            "Config": {"Labels": {OWNER: self.stack.owner, REVISION: "revision",
                         "com.docker.compose.project": self.stack.name, "com.docker.compose.service": "xray"},
                       "User": f"{m.HUB_UID}:{m.HUB_GID}", "Cmd": ["run", "-config", "/etc/xray/owrt-remote.json"]},
            "Mounts": [{"Type": "bind", "RW": False, "Destination": dest, "Source": config[key]}
                       for dest, key in (("/etc/xray", "RO_XRAY_CONFIG_DIR"),
                                         ("/usr/local/share/xray", "RO_XRAY_ASSET_DIR"),
                                         ("/etc/router-orchestrator/tls", "RO_TLS_DIR"))],
        }
        self.adapter = Adapter(self.env)
        self.spec = router()
        self.token = "b" * 32
        for owner, name, kwargs in (
            (self.stack, "verify_release", {}), (self.adapter, "test_config", {}),
            (self.adapter, "restart", {}), (self.adapter, "probe", {"return_value": True}),
            (m, "LEGACY_HUB_STATE", {"new": self.root / "legacy-data"}),
            (m, "ENV", {"new": self.root / "hub-only.env"}),
            (m, "HUB_DEPLOY_ROOT", {"new": self.root}),
            (m, "status_service", {"return_value": False}),
            ("unified_xray_adapter.socket.socket", None, {}),
        ):
            hook = patch(owner, **kwargs) if name is None else patch.object(owner, name, **kwargs)
            hook.start()
            self.addCleanup(hook.stop)

    def call(self, op, **args):
        return self.adapter.handle(self.stack, self.release, {"xray": self.record}, {"op": op, **args})

    def prepare(self):
        self.token = uuid.uuid4().hex
        return self.call("apply_config", router_id="demo", router=self.spec, transaction=self.token)

    def test_profile_publish_requires_approval_and_commits_matching_receipt(self):
        host = "lifecycle.example.invalid"
        tls = self.root / "tls"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
             "-subj", "/CN=" + host, "-addext", "subjectAltName=DNS:" + host,
             "-keyout", str(tls / "privkey.pem"), "-out", str(tls / "fullchain.pem")],
            check=True, capture_output=True, timeout=10,
        )
        ref = {"profile_id": "lifecycle-wss", "revision": 1}
        profile = {"name": ref["profile_id"], "transport": "wss", "server_name": host,
                   "port": 30123, "path": "/lifecycle/"}
        with (
            patch("unified_transport_catalog.live_tcp_ports", return_value=set()),
            patch("unified_transport_runtime.live_tcp_ports", return_value=set()),
            patch.object(self.stack, "inspect", return_value={"xray": self.record}),
            patch.object(TransportRuntime, "prove") as prove,
        ):
            self.call("transport.draft", profile=profile, label="Lifecycle test",
                      remote_host="127.0.0.1", public_port=self.stack.plan["public"])
            stage = self.call("transport.stage", **ref)["stage"]
            approval = dict(ref, stage_sha256=stage["stage_sha256"], confirmed=True)
            with self.assertRaisesRegex(ValueError, "current stage must be approved"):
                self.call("transport.publish", **approval)
            prove.assert_not_called()
            self.assertEqual(self.state.config.read_bytes(), self.baseline)
            self.call("transport.approve", **approval)
            self.assertEqual(self.call("transport.publish", **approval),
                             {"published": True, "active": True, "applied": True})
            prove.assert_called_once()
        record = self.state.load()
        self.assertIsNone(record["profile_pending"])
        self.assertTrue(record["profile_committed"])
        with connection(self.stack.transport_store) as db:
            self.assertEqual(receipt(db), record["profile_committed"])
            self.assertTrue(load_artifact(db, **ref)[2])

    def test_validate_apply_commit_delete_and_restore(self):
        initial_material = self.stack.material()
        self.call("validate_config", router_id="demo", router=self.spec)
        self.assertFalse(self.state.exists())
        self.assertTrue(self.prepare()["changed"])
        with self.assertRaisesRegex(m.Blocked, "Pending Xray"):
            self.state.baseline()
        with self.assertRaisesRegex(m.Blocked, "Pending"):
            self.call("restart")
        self.assertFalse(self.call("status")["ready"])
        self.assertEqual(self.state.baseline(recover=True), self.baseline)
        self.call("apply_config", commit=self.token)
        self.call("apply_config", commit=self.token)
        self.assertEqual(self.state.baseline(), self.baseline)
        self.assertEqual(self.stack.material(), initial_material)
        self.assertIn(("127.0.0.1", self.spec["ports"][0]), self.stack.ports()["xray"])
        inbounds = json.loads(self.state.config.read_bytes())["inbounds"]
        self.assertEqual(inbounds[-1]["port"], self.spec["ports"][1])
        self.spec["ports"] = [30110, 30111]
        self.prepare()
        self.call("apply_config", commit=self.token)
        self.assertEqual(self.state.load()["routers"]["demo"]["ports"], [30110, 30111])
        self.call("apply_config", router_id="demo", router=None, transaction=self.token)
        self.call("rollback", transaction=self.token)
        self.assertEqual(set(self.state.load()["routers"]), {"demo"})
        delete_token = uuid.uuid4().hex
        self.call("apply_config", router_id="demo", router=None, transaction=delete_token)
        self.call("apply_config", commit=delete_token)
        self.assertEqual(self.state.config.read_bytes(), self.baseline)

    def test_recovery_while_only_journal_exists(self):
        self.state.save(dict(self.state.load(), pending={"token": self.token, "routers": {"demo": self.spec}}))
        self.call("rollback", transaction=self.token)
        self.assertEqual(self.state.config.read_bytes(), self.baseline)
        self.assertIsNone(self.state.load()["pending"])

    def test_sigkill_after_atomic_replace_recovers_durable_prepare(self):
        pid = os.fork()
        if pid == 0:
            try:
                self.adapter.restart.side_effect = lambda *_: os.kill(os.getpid(), signal.SIGKILL)
                self.prepare()
            finally:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        pending = self.state.load()["pending"]
        self.assertNotEqual(self.state.config.read_bytes(), self.baseline)
        self.call("rollback", transaction=pending["token"])
        self.assertEqual(self.state.config.read_bytes(), self.baseline)

    def test_staging_crash_keeps_release_identity_and_reserves_staging_path(self):
        original = self.stack.material()
        self.state.staging.mkdir(mode=0o700)
        with self.assertRaisesRegex(m.Blocked, "staging"):
            self.stack.material()
        candidate = self.state.staging / "candidate"
        candidate.write_text("incomplete")
        candidate.chmod(0o600)
        self.state.save(dict(self.state.load(), pending={"token": self.token, "routers": {"demo": self.spec}}))
        self.assertEqual(self.stack.material(recover_xray=True), original)
        self.call("rollback", transaction=self.token)
        self.assertFalse(self.state.staging.exists())
        self.assertEqual(self.stack.material(), original)

    def test_runtime_failure_restores_bytes(self):
        self.adapter.restart.side_effect = [m.Blocked("Xray did not start"), None]
        with self.assertRaisesRegex(m.Blocked, "did not start"):
            self.prepare()
        self.assertEqual(self.state.config.read_bytes(), self.baseline)
        self.assertIsNone(self.state.load()["pending"])

    def test_profile_rollback_keeps_unrelated_live_revision(self):
        from secure_profile_store import connection
        from unified_transport_runtime import Runtime

        def item(name, port):
            return {"revision": 1, "public_port": 9443, "remote_host": "fixture.invalid",
                    "profile": {"name": name, "transport": "wss", "server_name": name + ".fixture.invalid",
                                "port": port, "path": "/reverse/", "certificate_file":
                                "/etc/router-orchestrator/tls/fullchain.pem", "key_file":
                                "/etc/router-orchestrator/tls/privkey.pem"}}

        old = item("disabled", 30200)
        unrelated = item("live", 30201)
        self.state.save(dict(self.state.load(), profiles=[unrelated],
                             profile_rollbacks={"disabled:1": old, "live:1": None}))
        self.stack.transport_store.mkdir(mode=0o700)
        runtime = Runtime(self.adapter, self.stack, self.release, self.state, self.record["Id"], Mock())
        with (
            connection(self.stack.transport_store) as db,
            patch.object(runtime, "transact") as transact,
            patch("unified_transport_runtime.live_tcp_ports", return_value=set()),
        ):
            result = runtime.change(db, "transport.rollback", ("disabled", 1), {}, "", True)
        self.assertTrue(result["active"])
        _, _, candidate, _, rollbacks, additions, _ = transact.call_args.args
        self.assertEqual(candidate, [unrelated, old])
        self.assertEqual(additions, [old])
        self.assertEqual(rollbacks, {"live:1": None})

    def test_failed_rollback_restart_leaves_recoverable_prepare(self):
        self.adapter.restart.side_effect = [m.Blocked("candidate failed"), m.Blocked("restore failed"), None]
        with self.assertRaisesRegex(m.Blocked, "restore failed"):
            self.prepare()
        self.assertEqual(self.state.config.read_bytes(), self.baseline)
        self.assertEqual(self.state.load()["pending"]["token"], self.token)
        self.call("rollback", transaction=self.token)
        self.assertIsNone(self.state.load()["pending"])
        self.assertEqual(self.state.config.read_bytes(), self.baseline)

    def test_validation_failure_is_write_free(self):
        self.adapter.test_config.side_effect = subprocess.CalledProcessError(1, "xray")
        with self.assertRaises(subprocess.CalledProcessError):
            self.prepare()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.state.config.read_bytes(), self.baseline)

    def test_foreign_mount_image_and_labels_fail_closed(self):
        for mapping, key in ((self.record, "Image"), (self.record["Mounts"][0], "Source"),
                             (self.record["Config"]["Labels"], OWNER)):
            with patch.dict(mapping, {key: "foreign"}), self.assertRaises(m.Blocked):
                self.call("restart")
        self.adapter.restart.assert_not_called()

    def test_xray_image_volumes_must_be_explicitly_overridden(self):
        host = self.record["HostConfig"]
        self.assertEqual(self.adapter.owned_xray(self.stack, self.release, self.record), self.record["Id"])
        for dest in ("/usr/local/etc/xray", "/var/log/xray"):
            with self.subTest(dest=dest):
                mounts = self.record["Mounts"]
                mounts.append({"Type": "volume", "RW": True, "Destination": dest, "Source": "/foreign"})
                try:
                    with self.assertRaisesRegex(m.Blocked, "bind-mounts"):
                        self.adapter.owned_xray(self.stack, self.release, self.record)
                    mounts[-1] = {"Type": "tmpfs", "Destination": dest}
                    self.assertEqual(self.adapter.owned_xray(self.stack, self.release, self.record), self.record["Id"])
                finally:
                    mounts.pop()
                with patch.dict(host, {"Tmpfs": {p: v for p, v in XRAY_TMPFS.items() if p != dest}}):
                    with self.assertRaisesRegex(m.Blocked, "tmpfs"):
                        self.adapter.owned_xray(self.stack, self.release, self.record)
        for tmpfs in (dict(XRAY_TMPFS, **{"/unexpected": "rw"}),
                      dict(XRAY_TMPFS, **{"/usr/local/etc/xray": "rw"})):
            with patch.dict(host, {"Tmpfs": tmpfs}), self.assertRaisesRegex(m.Blocked, "tmpfs"):
                self.adapter.owned_xray(self.stack, self.release, self.record)
        self.record["Mounts"].append({"Type": "tmpfs", "Destination": "/unexpected"})
        with self.assertRaisesRegex(m.Blocked, "bind-mounts"):
            self.adapter.owned_xray(self.stack, self.release, self.record)

    def test_hold_blocks_prepare_and_recovery(self):
        self.prepare()
        (self.stack.state / ".xray-maintenance-hold").symlink_to(self.root / "missing")
        self.assertTrue(self.call("status")["maintenance_hold"])
        self.assertNotIn("restart", self.call("status")["operations"])
        with self.assertRaisesRegex(m.Blocked, "HOLD"):
            self.call("rollback", transaction=self.token)
        self.assertEqual(self.state.load()["pending"]["token"], self.token)

    def test_stage_and_approval_are_blocked_under_hold_and_pending_enrollment(self):
        ref = {"profile_id": "demo", "revision": 1}
        operations = (
            ("transport.stage", ref),
            ("transport.approve", dict(ref, stage_sha256="a" * 64, confirmed=True)),
            ("transport.publish", dict(ref, stage_sha256="a" * 64, confirmed=True)),
            ("transport.activate", dict(ref, confirmed=True)),
            ("transport.disable", dict(ref, confirmed=True)),
            ("transport.rollback", dict(ref, confirmed=True)),
        )
        self.prepare()
        with patch("unified_xray_adapter.transport_catalog.handle") as catalog:
            for operation, args in operations:
                with self.assertRaisesRegex(m.Blocked, "Pending Xray"):
                    self.call(operation, **args)
            hold = self.stack.state / ".xray-maintenance-hold"
            hold.touch()
            for operation, args in operations:
                with self.assertRaisesRegex(m.Blocked, "HOLD"):
                    self.call(operation, **args)
            catalog.assert_not_called()

    def test_symlink_and_out_of_band_config_refused(self):
        self.prepare()
        self.call("apply_config", commit=self.token)
        self.state.config.write_text("{}")
        with self.assertRaisesRegex(m.Blocked, "differs"):
            self.state.baseline()
        self.state.config.unlink()
        self.state.config.symlink_to(self.state.path)
        with self.assertRaisesRegex(m.Blocked, "alias"):
            self.state.baseline()


class SQLiteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "hub.db"
        self.hub_db = sqlite3.connect(self.db_path)
        self.addCleanup(self.hub_db.close)
        self.hub_db.row_factory = sqlite3.Row
        with patch.object(hub, "unified_xray_enabled", return_value=True):
            hub.init_db(self.hub_db)
        self.host = Mock()
        hook = patch.object(hub, "unified_xray_request", self.host)
        hook.start()
        self.addCleanup(hook.stop)
        self.status = {"public_port": 9443, "pending": None}

    def test_identity_rotation_is_router_bound_and_promotion_is_atomic(self):
        self.host.side_effect = [self.status, {"changed": True}, {}] * 2
        for name, port in (("demo", 30100), ("other", 30200)):
            hub.unified_router_edit(self.hub_db, name,
                                    {"entry_port": port, "ssh_entry_port": port + 1}, create=True)
        first = "Bearer " + hub.issue_transport_identity(self.hub_db, "demo")
        self.assertEqual(hub.transport_identity(self.hub_db, "other", first, promote=True), (False, False))
        self.assertEqual(hub.transport_identity(self.hub_db, "demo", first), (False, False))
        self.assertEqual(hub.transport_identity(self.hub_db, "demo", first, promote=True), (True, True))
        self.hub_db.commit()
        second = "Bearer " + hub.issue_transport_identity(self.hub_db, "demo")
        self.hub_db.commit()
        self.assertEqual(hub.transport_identity(self.hub_db, "demo", first), (True, True))
        self.assertEqual(hub.transport_identity(self.hub_db, "demo", second, promote=True), (True, True))
        self.hub_db.rollback()
        self.assertEqual(hub.transport_identity(self.hub_db, "demo", first), (True, True))
        self.assertEqual(hub.transport_identity(self.hub_db, "demo", second, promote=True), (True, True))
        self.hub_db.commit()
        self.assertEqual(hub.transport_identity(self.hub_db, "demo", first), (False, True))

    def test_lost_rotation_responses_keep_the_last_committed_credential(self):
        self.host.side_effect = [self.status, {"changed": True}, {}]
        hub.unified_router_edit(self.hub_db, "demo",
                                {"entry_port": 30100, "ssh_entry_port": 30101}, create=True)
        first = "Bearer " + hub.issue_transport_identity(self.hub_db, "demo")
        self.assertTrue(hub.transport_identity(self.hub_db, "demo", first, promote=True)[0])
        self.hub_db.commit()
        hub.issue_transport_identity(self.hub_db, "demo")  # owner lost the issuance response
        self.hub_db.commit()
        second = "Bearer " + hub.issue_transport_identity(self.hub_db, "demo")
        self.hub_db.commit()
        self.assertTrue(hub.transport_identity(self.hub_db, "demo", first)[0])
        self.assertTrue(hub.transport_identity(self.hub_db, "demo", second, promote=True)[0])
        self.hub_db.commit()  # agent lost the promotion response
        with sqlite3.connect(self.db_path) as restarted:
            restarted.row_factory = sqlite3.Row
            self.assertFalse(hub.transport_identity(restarted, "demo", first, promote=True)[0])
            self.assertTrue(hub.transport_identity(restarted, "demo", second, promote=True)[0])

    def test_task_is_redelivered_after_hub_restart_and_ttl_cannot_replace_it(self):
        hub.queue_transport_task(self.hub_db, "demo", "stage", "enrollment", None)
        self.hub_db.commit()
        self.assertIsNone(hub.claim_transport_task(self.hub_db, "other"))
        claimed = hub.claim_transport_task(self.hub_db, "demo")
        self.hub_db.rollback()  # failure before the claim commit
        self.assertEqual(hub.claim_transport_task(self.hub_db, "demo"), claimed)
        self.hub_db.execute("update transport_router_task set updated_at=0")
        self.hub_db.commit()  # response is lost after commit
        with sqlite3.connect(self.db_path) as restarted:
            restarted.row_factory = sqlite3.Row
            self.assertEqual(hub.claim_transport_task(restarted, "demo"), claimed)
            with self.assertRaisesRegex(ValueError, "already pending"):
                hub.queue_transport_task(restarted, "demo", "stage", "enrollment", None)

    def test_task_receipt_rolls_back_and_is_idempotent_after_the_next_task(self):
        hub.queue_transport_task(self.hub_db, "demo", "stage", "enrollment", None)
        self.hub_db.commit()
        task_id = self.hub_db.execute("select task_id from transport_router_task").fetchone()[0]
        with self.assertRaises(ValueError):
            hub.acknowledge_transport_task(self.hub_db, "demo", task_id, None, True)
        hub.claim_transport_task(self.hub_db, "demo")
        self.hub_db.commit()
        for router, supplied_id, target in [
                ("other", task_id, None), ("demo", "0" * 32, None),
                ("demo", task_id, {"profile_id": "live", "revision": 2, "assignment_id": "a" * 64})]:
            with self.subTest(router=router, target=target), self.assertRaises(ValueError):
                hub.acknowledge_transport_task(self.hub_db, router, supplied_id, target, True)
        self.hub_db.execute("""create trigger fail_receipt before insert on transport_router_receipt
            begin select raise(abort, 'injected error after updating task'); end""")
        with self.assertRaises(sqlite3.IntegrityError), self.hub_db:
            hub.acknowledge_transport_task(self.hub_db, "demo", task_id, None, True)
        self.assertEqual(hub.claim_transport_task(self.hub_db, "demo")["task_id"], task_id)
        self.hub_db.execute("drop trigger fail_receipt")
        with self.hub_db:
            hub.acknowledge_transport_task(self.hub_db, "demo", task_id, None, True)
        self.assertIsNone(hub.claim_transport_task(self.hub_db, "demo"))
        self.hub_db.commit()
        with sqlite3.connect(self.db_path) as restarted:
            restarted.row_factory = sqlite3.Row
            # The first ACK response was lost. A new command may already be queued.
            hub.queue_transport_task(restarted, "demo", "stage", "enrollment", None)
            next_task = hub.claim_transport_task(restarted, "demo")
            hub.acknowledge_transport_task(restarted, "demo", next_task["task_id"], None, False)
            hub.acknowledge_transport_task(restarted, "demo", task_id, None, True)
            with self.assertRaises(ValueError):
                hub.acknowledge_transport_task(restarted, "demo", task_id, None, False)
            self.assertIsNone(hub.claim_transport_task(restarted, "demo"))

    def test_adapter_portal_rejects_same_revision_reassigned_to_new_credentials(self):
        state = Mock()
        runtime = TransportRuntime.__new__(TransportRuntime)
        runtime.state = state
        pair = router()
        profile = {"profile": {"name": "live"}, "revision": 2, "assignments": {"demo": pair}}
        state.load.return_value = {"pending": None, "routers": {"demo": pair}, "profiles": [profile]}
        target = {"profile_id": "live", "revision": 2, "assignment_id": assignment_id(pair)}
        self.assertEqual(runtime.router_portals("demo", target)["ports"], pair["ports"])
        profile["assignments"]["demo"] = router()
        with self.assertRaises(m.Blocked):
            runtime.router_portals("demo", target)

    def test_task_requires_exact_assignment_and_confirmed_activation(self):
        self.host.side_effect = [self.status, {"changed": True}, {}]
        hub.unified_router_edit(self.hub_db, "demo",
                                {"entry_port": 30100, "ssh_entry_port": 30101}, create=True)
        target = {"profile_id": "live", "revision": 2, "assignment_id": "a" * 64}
        self.host.side_effect = None
        self.host.return_value = {"profiles": [{**target, "outbounds": []}]}
        for stale in (dict(target, profile_id="other"), dict(target, revision=1),
                      dict(target, assignment_id="b" * 64)):
            with self.assertRaisesRegex(ValueError, "assignment changed"):
                hub.queue_transport_task(self.hub_db, "demo", "activate", "0", stale)
        hub.queue_transport_task(self.hub_db, "demo", "activate", "0", target)
        task_id = hub.claim_transport_task(self.hub_db, "demo")["task_id"]
        with self.assertRaisesRegex(ValueError, "activation is not confirmed"):
            hub.acknowledge_transport_task(self.hub_db, "demo", task_id, target, True)
        self.hub_db.execute("update routers set transport_active_ref=? where id='demo'",
                            (hub.transport_target(target),))
        hub.acknowledge_transport_task(self.hub_db, "demo", task_id, target, True)

    def test_agent_task_api_requires_v2_and_rejects_unmatched_receipt(self):
        self.host.side_effect = [self.status, {"changed": True}, {}]
        hub.unified_router_edit(self.hub_db, "demo",
                                {"entry_port": 30100, "ssh_entry_port": 30101}, create=True)
        token = "Bearer " + hub.issue_transport_identity(self.hub_db, "demo")
        hub.transport_identity(self.hub_db, "demo", token, promote=True)
        hub.queue_transport_task(self.hub_db, "demo", "stage", "enrollment", None)
        self.hub_db.commit()
        self.host.side_effect = None
        self.host.return_value = {"maintenance_hold": False, "ready": True}
        handler = hub.Handler.__new__(hub.Handler)
        handler.parsed = lambda: SimpleNamespace(path="/api/agent/transport-task")
        handler.headers = {"Authorization": token}
        handler.request_scheme = lambda: "https"
        handler.server = SimpleNamespace(app=SimpleNamespace(conn=lambda: self.hub_db))
        handler.send_json = Mock()

        def request(payload):
            handler.read_payload = lambda **kwargs: {"id": "demo", **payload}
            with patch.object(hub, "unified_xray_enabled", return_value=True):
                handler.do_POST()
            return handler.send_json.call_args.args

        self.assertEqual(request({})[0], 409)
        self.host.return_value["maintenance_hold"] = True
        self.assertEqual(request({"protocol": 2})[0], 409)
        self.host.return_value["maintenance_hold"] = False
        status, response = request({"protocol": 2})
        self.assertEqual(status, 200)
        self.assertEqual(request({"protocol": 2}), (200, response))
        receipt = {"task_id": response["task"]["task_id"], "target": None, "succeeded": True}
        self.assertEqual(request(dict(receipt, task_id="0" * 32))[0], 409)
        self.assertEqual(request(receipt), (200, {"ok": True}))
        self.assertEqual(request(receipt), (200, {"ok": True}))
        self.assertEqual(request({"protocol": 2}), (200, {"ok": True, "task": None}))

    def test_channel_promotion_changes_routes_only_after_verified_probe(self):
        self.host.side_effect = [self.status, {"changed": True}, {}]
        hub.unified_router_edit(self.hub_db, "demo",
                                {"entry_port": 30100, "ssh_entry_port": 30101}, create=True)
        self.hub_db.execute("update routers set last_seen=? where id='demo'", (hub.now_ts(),))
        self.hub_db.commit()
        profile = {"profile_id": "live", "revision": 2}
        healthy = dict(self.status, maintenance_hold=False, ready=True)
        self.host.side_effect = [healthy, {"ports": [30200, 30201]},
                                 healthy, {"ports": [30200, 30201]}, healthy]
        with patch.object(hub, "probe_transport_channels", side_effect=[None, 7]), \
             patch.object(hub, "unified_xray_enabled", return_value=True):
            self.assertFalse(hub.Handler.agent_transport_probe(None, self.hub_db, "demo", profile,
                                                                 activate=True)["ok"])
            self.assertEqual(hub.router_channel_port(hub.get_active_router(self.hub_db, "demo")), 30100)
            self.assertTrue(hub.Handler.agent_transport_probe(None, self.hub_db, "demo", profile,
                                                                activate=True)["ok"])
            row = hub.get_active_router(self.hub_db, "demo")
            self.assertEqual(hub.router_channel_port(row), 30200)
            self.assertEqual(hub.router_channel_port(row, ssh=True), 30201)

    def test_failed_prepare_rolls_back_sqlite_enrollment(self):
        self.host.side_effect = [self.status, RuntimeError("no ready"), self.status]
        with self.assertRaises(RuntimeError):
            hub.unified_router_edit(self.hub_db, "demo", {"entry_port": 30100, "ssh_entry_port": 30101}, create=True)
        self.assertIsNone(hub.get_router(self.hub_db, "demo"))
        self.assertIsNone(self.hub_db.execute("select token from xray_receipt").fetchone())

    def test_failed_edit_keeps_existing_row_and_receipt(self):
        self.host.side_effect = [self.status, {"changed": True}, {}]
        hub.unified_router_edit(self.hub_db, "demo", {"entry_port": 30100, "ssh_entry_port": 30101}, create=True)
        before = hub.get_active_router(self.hub_db, "demo")
        receipt = self.hub_db.execute("select token from xray_receipt").fetchone()
        self.host.side_effect = [self.status, RuntimeError("no ready"), self.status]
        with self.assertRaises(RuntimeError):
            hub.unified_router_edit(self.hub_db, "demo", {"entry_port": 30110})
        self.assertEqual(hub.get_active_router(self.hub_db, "demo"), before)
        self.assertEqual(self.hub_db.execute("select token from xray_receipt").fetchone(), receipt)

    def test_commit_response_loss_reconciles_using_receipt(self):
        self.host.side_effect = [self.status, {"changed": True}, RuntimeError("lost")]
        with self.assertRaises(RuntimeError):
            hub.unified_router_edit(self.hub_db, "demo", {"entry_port": 30100, "ssh_entry_port": 30101}, create=True)
        self.assertEqual(hub.get_router(self.hub_db, "demo")["vless_port"], 9443)
        token = self.hub_db.execute("select token from xray_receipt").fetchone()[0]
        self.host.side_effect = [{"pending": token}, {}]
        hub.reconcile_unified_xray(self.hub_db)
        self.host.assert_called_with("apply_config", commit=token)

    def test_no_receipt_reconciles_by_rollback(self):
        token = uuid.uuid4().hex
        self.host.side_effect = [{"pending": token}, {}]
        hub.reconcile_unified_xray(self.hub_db)
        self.host.assert_called_with("rollback", transaction=token)

    def test_unified_heartbeat_cannot_resurrect_deleted_router(self):
        with patch.object(hub, "unified_xray_enabled", return_value=True):
            with self.assertRaisesRegex(ValueError, "Register"):
                hub.heartbeat(self.hub_db, {"id": "unknown"})
