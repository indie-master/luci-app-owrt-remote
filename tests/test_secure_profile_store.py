import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
import secure_profile_store as store
import secure_profile_catalog as catalog
original_connection = store.connection
from transport_profiles import ProfileError


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
