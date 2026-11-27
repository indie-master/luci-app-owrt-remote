import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
import secure_profile_catalog as catalog
from transport_profiles import ProfileError


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        catalog.initialize(self.db)

    def draft(self, profile_id="direct", transport="tcp-tls"):
        return catalog.create_draft(self.db, profile_id=profile_id, label=profile_id,
                                    transport=transport, hostname="router.example.org",
                                    public_port=443, artifact_sha256="a"*64)

    def publish(self, profile_id, revision):
        catalog.publish_verified(self.db, profile_id, revision,
                                 verified_artifact_sha256="a"*64)

    def test_drafts_hidden_until_published_and_public_metadata_only(self):
        self.assertEqual(self.draft(), 1)
        self.assertEqual(catalog.available_profiles(self.db), [])
        with self.assertRaises(ProfileError):
            catalog.publish_verified(self.db, "direct", 1, verified_artifact_sha256="b"*64)
        self.publish("direct", 1)
        self.assertEqual(catalog.available_profiles(self.db)[0]["transport"], "tcp-tls")
        self.assertNotIn("artifact_sha256", catalog.available_profiles(self.db)[0])
        self.assertEqual(self.draft(), 2)
        self.assertEqual(len(catalog.available_profiles(self.db)), 1)

    def test_preferences_are_explicit_and_router_specific(self):
        self.draft()
        self.publish("direct", 1)
        self.draft("backup", "reality-xhttp")
        self.publish("backup", 1)
        choice = [{"profile_id":"direct","revision":1}, {"profile_id":"backup","revision":1}]
        catalog.set_desired_profiles(self.db, "netis", choice)
        self.assertEqual(catalog.desired_profiles(self.db, "netis"), choice)
        self.assertEqual(catalog.desired_profiles(self.db, "main"), [])
        self.draft()
        self.publish("direct", 2)
        self.assertEqual(catalog.desired_profiles(self.db, "netis"), choice)

    def test_downgrade_duplicate_and_unpublished_rejected(self):
        with self.assertRaises(ProfileError):
            self.draft(transport="tcp")
        self.draft()
        choice = [{"profile_id":"direct","revision":1}]
        with self.assertRaises(ProfileError):
            catalog.set_desired_profiles(self.db, "netis", choice)
        self.publish("direct", 1)
        with self.assertRaises(ProfileError):
            catalog.set_desired_profiles(self.db, "netis", choice*2)
        self.assertEqual(catalog.desired_profiles(self.db, "netis"), [])


if __name__ == "__main__":
    unittest.main()
