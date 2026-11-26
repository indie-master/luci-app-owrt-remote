import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
import secure_profile_store as store
import secure_profile_catalog as catalog
import unified_transport_catalog as unified_catalog
original_connection = store.connection
from transport_profiles import ProfileError
from unified_transport_catalog import draft_deletion, handle, validate_draft_input


def profile():
    return {"name": "test", "transport": "reality-xhttp", "port": 18550,
            "server_name": "target.example.org",
            "server_names": ["target.example.org"], "target_host": "target.example.org",
            "target_port": 443, "path": "/reverse/"}


@unittest.skipUnless(shutil.which("openssl"), "OpenSSL required")
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        os.chmod(self.root, 0o700)

    def save(self, db, **overrides):
        data = dict(profile(), **overrides)
        return store.save_draft(db, profile=data, label="Management",
                                remote_host="router.example.org", public_port=443)

    def test_automatic_port_reservation_and_explicit_collision(self):
        def save(db, port):
            return store.save_allocated_draft(db, profile=dict(profile(), port=port),
                 label="Management", remote_host="router.example.org", public_port=443,
                 occupied_ports={24000,24002})
        with store.connection(self.root) as db:
            self.assertEqual(save(db,0)["backend_port"],24001)
            self.assertEqual(save(db,0)["backend_port"],24003)
            for port in (24001,24000,443):
                with self.assertRaises(ProfileError):
                    save(db,port)
            self.assertEqual(len(store.list_revisions(db)),2)
        with store.connection(self.root) as db:
            self.assertEqual(save(db,0)["backend_port"],24004)

    def test_unified_delete_requires_unexpired_confirmation_for_exact_revision(self):
        with store.connection(self.root) as db:
            self.save(db)
            self.save(db, port=18551)
            with patch("unified_transport_catalog.time.time", return_value=1000):
                grant = draft_deletion(db, "transport.confirm_delete", "test", 1)
            self.assertEqual(grant["expires_in"], 600)
            token = grant["confirmation"]
            with patch("unified_transport_catalog.time.time", return_value=1599):
                with self.assertRaises(ProfileError):
                    draft_deletion(db, "transport.delete", "test", 2, token)
            with patch("unified_transport_catalog.time.time", return_value=1600):
                with self.assertRaises(ProfileError):
                    draft_deletion(db, "transport.delete", "test", 1, token)
                grant = draft_deletion(db, "transport.confirm_delete", "test", 1)
                self.assertTrue(draft_deletion(db, "transport.delete", "test", 1,
                                               grant["confirmation"])["deleted"])
                with self.assertRaises(ProfileError):
                    draft_deletion(db, "transport.delete", "test", 1, grant["confirmation"])
            self.assertEqual([r["revision"] for r in store.list_revisions(db)], [2])

    def test_unified_catalog_allows_only_the_four_secure_profile_shapes(self):
        fields = {
            "wss": {"path": "/reverse/"},
            "xhttp": {"path": "/reverse/"},
            "grpc": {"service_name": "reverse"},
            "reality-tcp": {"target_host": "target.example.org", "target_port": 443,
                            "server_names": ["target.example.org"]},
        }
        common = {"name": "test", "port": 0, "server_name": "target.example.org"}
        for transport, extra in fields.items():
            with self.subTest(transport=transport):
                draft = dict(common, transport=transport, **extra)
                validate_draft_input(draft)
                with self.assertRaises(ProfileError):
                    validate_draft_input(dict(draft, private_key="unexpected"))
        with self.assertRaises(ProfileError):
            validate_draft_input(dict(common, transport="tcp"))

    def test_unified_catalog_plans_client_and_server_without_writing_active_config(self):
        state = SimpleNamespace(config=self.root / "config.json")
        active = b'{"inbounds":[]}'
        state.config.write_bytes(active)
        state.config.chmod(0o600)
        stack = SimpleNamespace(transport_store=self.root / ".transport-fixture",
                                config={"RO_TLS_DIR": str(self.root)},
                                plan={"public": 443, "host": "hub.example.org", "hub": 8088,
                                      "wss": 18443, "tls": None, "health": 18444, "http": None,
                                      "routes": {}, "mode": "standalone"})
        adapter = Mock()
        specs = {"wss": {"path": "/reverse"}, "xhttp": {"path": "/reverse"},
                 "grpc": {"service_name": "reverse"},
                 "reality-tcp": {"target_host": "target.example.org", "target_port": 443,
                                 "server_names": ["reality-tcp.example.org"]}}
        with patch("unified_transport_catalog.protected", side_effect=lambda root: root), \
             patch("unified_transport_catalog.live_tcp_ports", return_value=set()), \
             patch("unified_transport_catalog.mounted_certificate",
                   return_value={"certificate_file": "/etc/router-orchestrator/tls/fixture.crt",
                                 "key_file": "/etc/router-orchestrator/tls/fixture.key"}):
            for kind, fields in specs.items():
                with self.subTest(kind=kind):
                    name = kind + ".example.org"
                    profile = dict(name=kind, port=0, transport=kind, server_name=name, **fields)
                    draft = handle(adapter, stack, {}, state, "transport.draft",
                                   dict(profile=profile, label=kind, remote_host=name, public_port=443))
                    ref = dict(profile_id=kind, revision=draft["draft"]["revision"])
                    detail = handle(adapter, stack, {}, state, "transport.detail", ref)
                    self.assertEqual(set(detail["profile"]), set(profile))
                    adapter.reset_mock()
                    plan = handle(adapter, stack, {}, state, "transport.plan", ref)
                    self.assertEqual(plan["routes"][0]["sni"], name)
                    self.assertFalse(plan["applied"])
                    self.assertFalse(plan["live_listener_verified"])
                    self.assertEqual(adapter.test_config.call_count, 2)
                    server, client = [json.loads(call.args[-1]) for call in adapter.test_config.call_args_list]
                    self.assertEqual(server["inbounds"][0]["settings"]["clients"][0]["id"],
                                     client["outbounds"][1]["settings"]["id"])
                    self.assertEqual(state.config.read_bytes(), active)

    def test_private_artifact_and_public_metadata_are_separate(self):
        with store.connection(self.root) as db:
            draft = self.save(db)
            artifact = json.loads(db.execute("SELECT artifact FROM secure_profile_artifacts").fetchone()[0])
            secret = artifact["profile"]["private_key"]
            self.assertEqual(len(secret), 43)
            self.assertEqual(len(artifact["profile"]["public_key"]), 43)
            self.assertEqual(len(artifact["profile"]["short_ids"][0]), 16)
            self.assertNotIn(secret, json.dumps(draft))
            self.assertNotIn(secret, json.dumps(store.list_revisions(db)))
            self.assertEqual(catalog.available_profiles(db), [])
            self.assertEqual(catalog.desired_profiles(db, "netis"), [])
        self.assertEqual(stat.S_IMODE((self.root / "profiles.db").stat().st_mode), 0o600)
        with store.connection(self.root) as db:
            self.assertEqual(self.save(db)["revision"], 2)
            self.assertEqual(len(store.list_revisions(db)), 2)

    def test_stage_reopens_after_response_loss_and_approval_binds_exact_candidate(self):
        active = b'{"inbounds":[]}'
        state = SimpleNamespace(config=self.root / "config.json")
        state.config.write_bytes(active)
        state.config.chmod(0o600)
        stack = Mock(path=self.root / "stack.env",
                     config={"RO_TLS_DIR": str(self.root)},
                     plan={"public": 443, "host": "hub.example.org", "hub": 8088,
                           "wss": 18443, "tls": None, "health": 18444, "http": None,
                           "routes": {}, "mode": "external-only"})
        stack.transport_store = self.root / ".transport-fixture"
        stack.inspect.return_value = {"xray": {}}
        release = {"revision": "a" * 64}
        adapter = Mock()
        container = "b" * 64
        adapter.owned_xray.return_value = container
        tls = {"certificate_file": "certificate-v1", "key_file": "key-v1"}
        with patch("unified_transport_catalog.protected", side_effect=lambda root: root), \
             patch("unified_transport_catalog.live_tcp_ports", return_value=set()) as ports, \
             patch("unified_transport_catalog.tls_binding", side_effect=lambda *_: dict(tls)), \
             patch("unified_transport_catalog.mounted_certificate",
                   return_value={"certificate_file": "/etc/router-orchestrator/tls/fixture.crt",
                                 "key_file": "/etc/router-orchestrator/tls/fixture.key"}):
            def call(op, **args):
                return handle(adapter, stack, release, state, "transport." + op, args,
                              container=container)

            call("draft", profile={"name": "test", "transport": "wss", "port": 24000,
                                   "server_name": "test.example.org", "path": "/reverse"},
                 label="Management", remote_host="test.example.org", public_port=443)
            ref = {"profile_id": "test", "revision": 1}
            first = call("stage", **ref)
            receipt = first["stage"]
            self.assertFalse(first["applied"])
            self.assertFalse(first["published"])
            self.assertFalse(first["live_listener_verified"])
            self.assertIsNone(receipt["approved_at"])
            state.load = lambda: {}
            self.assertEqual(call("list")["revisions"][0]["stage"], receipt)
            self.assertEqual(call("stage", **ref)["stage"], receipt)
            approval = dict(ref, stage_sha256=receipt["stage_sha256"], confirmed=True)
            approved = call("approve", **approval)["stage"]
            self.assertIsNotNone(approved["approved_at"])
            self.assertEqual(call("approve", **approval)["stage"], approved)
            stack.verify_release.side_effect = ProfileError("ingress changed")
            with self.assertRaisesRegex(ProfileError, "ingress changed"):
                call("approve", **approval)
            stack.verify_release.side_effect = None
            self.assertEqual(call("list")["revisions"][0]["stage"], approved)

            tls["certificate_file"] = "certificate-v2"
            with self.assertRaisesRegex(ProfileError, "stage again"):
                call("approve", **approval)
            def interrupt_write(*args):
                unified_catalog_record(*args)
                raise InterruptedError("before SQLite commit")

            unified_catalog_record = unified_catalog.record_stage
            with patch.object(unified_catalog, "record_stage", side_effect=interrupt_write):
                with self.assertRaises(InterruptedError):
                    call("stage", **ref)
            self.assertEqual(call("list")["revisions"][0]["stage"], approved)
            updated = call("stage", **ref)["stage"]
            self.assertNotEqual(updated["stage_sha256"], receipt["stage_sha256"])
            self.assertIsNone(updated["approved_at"])
            ports.side_effect = [set(), {24000}]
            with self.assertRaisesRegex(ProfileError, "inputs changed"):
                call("stage", **ref)
            self.assertEqual(call("list")["revisions"][0]["stage"], updated)
            self.assertEqual(state.config.read_bytes(), active)
            self.assertNotIn("key-v1", json.dumps(first))

    def test_immutable_artifact_validation_rejects_tampering(self):
        with store.connection(self.root) as db:
            self.save(db)
            artifact, digest, published = store.load_artifact(db, "test", 1)
            self.assertFalse(published)
            self.assertEqual(artifact["remote_host"], "router.example.org")
            self.assertEqual(len(digest), 64)
            db.execute("UPDATE secure_profile_artifacts SET artifact=?",
                       (b'{"modified":true}',))
            with self.assertRaisesRegex(ProfileError, "digest mismatch"):
                store.load_artifact(db, "test", 1)

    def test_adapter_draft_validation_is_not_publication_or_assignment(self):
        import host_adapter
        with store.connection(self.root) as db:
            self.save(db)
        live = self.root / "xray.json"
        original = b'{"inbounds":[],"routing":{"rules":[]}}'
        live.write_bytes(original)
        adapter = host_adapter.Adapter()
        with patch.object(store, "connection", side_effect=lambda: original_connection(self.root)), \
                patch.object(host_adapter, "CONFIG", live), \
                patch.object(adapter, "transport_preview", return_value={"validated": True}) as preview:
            result = adapter.handle({"op":"secure.catalog.validate","profile_id":"test","revision":1})
        self.assertTrue(result["xray_validated"])
        self.assertFalse(result["published"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["ingress_inventory_checked"])
        self.assertNotIn("private_key", json.dumps(result))
        self.assertEqual(live.read_bytes(), original)
        preview.assert_called_once()
        with store.connection(self.root) as db:
            self.assertEqual(catalog.available_profiles(db), [])
            self.assertEqual(catalog.desired_profiles(db, "netis"), [])

    def test_tls_draft_resolves_mounted_certificate(self):
        selected = {"certificate_file": "/etc/router-orchestrator/tls/cert.pem",
                    "key_file": "/etc/router-orchestrator/tls/key.pem"}
        with store.connection(self.root) as db:
            with patch("secure_certificate_inventory.mounted_certificate", return_value=selected) as choose:
                store.save_draft(db, profile={"name":"tls", "transport":"tcp-tls",
                    "port":18551, "server_name":"router.example.org"},
                    label="TLS", remote_host="router.example.org", public_port=443)
            artifact, _, _ = store.load_artifact(db, "tls", 1)
            self.assertEqual(artifact["profile"]["certificate_file"], selected["certificate_file"])
            choose.assert_called_once_with("router.example.org")

    def test_bad_pair_and_plaintext_rejected_without_draft(self):
        with store.connection(self.root) as db:
            pair = profile()
            store.reality_keys(pair)
            wrong = profile()
            store.reality_keys(wrong)
            with self.assertRaises(ProfileError):
                self.save(db, private_key=pair["private_key"], public_key=wrong["public_key"])
            with self.assertRaises(ProfileError):
                self.save(db, transport="tcp")
            self.assertEqual(store.list_revisions(db), [])

    def test_store_rejects_symlink_and_world_accessible_directory(self):
        os.chmod(self.root, 0o755)
        with self.assertRaises(ProfileError):
            with store.connection(self.root):
                self.fail("unsafe directory accepted")
        os.chmod(self.root, 0o700)
        target = self.root / "unrelated"
        target.write_text("untouched")
        (self.root / "profiles.db").symlink_to(target)
        with self.assertRaises(ProfileError):
            with store.connection(self.root):
                self.fail("symlink accepted")
        self.assertEqual(target.read_text(), "untouched")


if __name__ == "__main__":
    unittest.main()
