"""Offline Docker transaction regressions with disposable SQLite fixtures."""
import contextlib
import importlib.util
import io
import json
import os
import signal
from pathlib import Path
import sqlite3
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("docker_manager", Path(__file__).resolve().parents[1] / "docker/manager.py")
manager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manager)


class BackupRecoveryTests(unittest.TestCase):
    def test_compose_status_error_blocks_backup(self):
        failure = subprocess.CalledProcessError(1, ["docker", "compose"])
        with patch.object(manager, "compose", side_effect=failure), \
             patch.object(manager, "require_root"), patch.object(manager, "status_service", return_value=False), \
             patch.object(manager, "state_path") as state:
            with self.assertRaises(subprocess.CalledProcessError):
                manager.backup()
            state.assert_not_called()

    def test_unconfirmed_stop_blocks_backup(self):
        for value in ("running", "paused", "restarting", "unexpected"):
            with self.subTest(state=value), \
                 patch.object(manager, "compose", return_value=SimpleNamespace(stdout=value + "\n")), \
                 patch.object(manager, "require_root"), patch.object(manager, "status_service", return_value=False), \
                 patch.object(manager, "state_path") as state:
                with self.assertRaises(manager.Blocked):
                    manager.backup()
                state.assert_not_called()

    def test_active_host_hub_blocks_backup(self):
        with patch.object(manager, "require_root"), patch.object(manager, "status_service", return_value=True), \
             patch.object(manager, "docker_hub_states") as docker:
            with self.assertRaisesRegex(manager.Blocked, "mixed-writer backup"):
                manager.backup()
            docker.assert_not_called()

class TransactionTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = root / "state"
        self.state.mkdir()
        self.db = self.enterContext(contextlib.closing(sqlite3.connect(self.state / "hub.db")))
        self.db.execute("CREATE TABLE data (value TEXT)")
        self.db.commit()
        self.old = "router-orchestrator-hub:old"
        self.new = "router-orchestrator-hub:new"
        self.images = {self.old: "sha256:" + "1" * 64, self.new: "sha256:" + "2" * 64}
        self.running = True
        self.actual_image = self.images[self.old]
        self.starts = []
        self.model = "model"
        self.health = True
        self.failure = None
        self.enterContext(patch.multiple(
            manager, LOCK=root / ".lock", HUB_DEPLOY_ROOT=root, ENV=root / ".env", RELEASE_STATE=root / ".release",
            require_root=lambda: None, status_service=lambda: False,
            compose_signature=lambda config: self.model, image_id=self.images.__getitem__,
            observed_hub=self.observe, compose_up=self.up, compose_stop=self.stop,
            health=lambda port, revision=None: self.health, backup=lambda: root / "snapshot.tar.gz"))
        self.config = {"RO_IMAGE": self.old, "RO_STATE_DIR": str(self.state),
                       "RO_PUBLIC_URL": "https://test.invalid", "RO_PORT": "18089",
                       "RO_XRAY_BACKEND": "external_readonly"}
        manager.save_env(self.config)

    def observe(self, config, images):
        if self.actual_image is None:
            return None
        manager.require(self.actual_image in images, "Unexpected actual image")
        return {"Image": self.actual_image, "Config": {"Labels": {}},
                "State": {"Status": "running" if self.running else "exited"}}

    def up(self, identifier):
        image = manager.settings()["RO_IMAGE"]
        self.assertEqual(identifier, self.images[image])
        self.actual_image = identifier
        self.running = True
        self.starts.append(image)
        if image == self.new and self.failure:
            self.failure()

    def stop(self):
        self.running = False

    def switch(self):
        manager.switch_image("deploy", manager.settings(), self.new)

    def prepare(self, phase):
        target = dict(self.config, RO_IMAGE=self.new)
        tx = manager.begin_transaction("deploy", target, self.images[self.new], self.config,
                                       self.images[self.old], manager.schema_fingerprint(self.config))
        if phase != "prepared":
            manager.save_transaction(tx, phase)
        return tx

    def test_preparation_failures_restart_original_and_keep_data(self):
        original_stop = self.stop

        def partial_stop():
            original_stop()
            raise OSError("stop failure")

        for name, failing in (("compose_stop", partial_stop), ("backup", lambda: 1 / 0)):
            with self.subTest(phase=name), patch.object(manager, name, failing):
                with self.assertRaises((OSError, ZeroDivisionError)):
                    self.switch()
                self.assertTrue(self.running)
                self.assertEqual(self.actual_image, self.images[self.old])
                self.assertIsNone(manager.pending_transaction())

    def test_recovery_after_process_loss_at_switch_boundaries(self):
        for phase in ("prepared", "before-env", "after-env", "after-up", "reverting"):
            with self.subTest(phase=phase):
                self.prepare("prepared" if phase == "prepared" else
                             "reverting" if phase == "reverting" else "switching")
                self.stop()
                if phase in {"after-env", "after-up", "reverting"}:
                    manager.write_image(self.new)
                if phase == "after-up":
                    self.up(self.images[self.new])
                    self.db.execute("INSERT INTO data VALUES ('written by candidate')")
                    self.db.commit()
                manager.recover()
                self.assertEqual(manager.settings(), self.config)
                self.assertTrue(self.running)
                self.assertEqual(self.actual_image, self.images[self.old])
                self.assertEqual(manager.read_transaction()["phase"], "recovered")
        self.assertEqual(self.db.execute("SELECT value FROM data").fetchall(), [("written by candidate",)])

    def test_sigkill_after_env_replace_leaves_recoverable_journal(self):
        write_image = manager.write_image

        def interrupt(image):
            write_image(image)
            os.kill(os.getpid(), signal.SIGKILL)

        with patch.object(manager, "write_image", interrupt):
            pid = os.fork()
            if pid == 0:
                try:
                    self.switch()
                finally:
                    os._exit(1)
            _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), -signal.SIGKILL)
        self.assertEqual(manager.pending_transaction()["phase"], "switching")
        self.assertEqual(manager.settings()["RO_IMAGE"], self.new)
        manager.recover()
        self.assertEqual(manager.settings(), self.config)

    def test_corrupt_live_database_blocks_recovery(self):
        self.prepare("switching")
        self.db.close()
        (self.state / "hub.db").write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            manager.recover()
        self.assertFalse(self.running)
        self.assertEqual(self.starts, [])
        self.assertEqual((self.state / "hub.db").read_bytes(), b"not sqlite")

    def test_metadata_failure_resumes_commit_without_reverting(self):
        with patch.object(manager, "write_release", side_effect=OSError("metadata failure")):
            with self.assertRaisesRegex(OSError, "metadata failure"):
                self.switch()
        self.assertEqual(manager.pending_transaction()["phase"], "commit")
        self.stop()  # Simulate restart with committed container stopped.
        manager.recover()
        self.assertEqual(self.starts, [self.new, self.new])
        self.assertEqual(manager.read_release()["current_image"], self.new)
        self.assertEqual(manager.read_transaction()["phase"], "complete")

    def test_incompatible_candidate_failure_blocks_old_image_and_preserves_writes(self):
        def migrate():
            self.db.execute("ALTER TABLE data ADD COLUMN migration TEXT")
            self.db.execute("INSERT INTO data VALUES ('new data', 'v2')")
            self.db.commit()
            raise OSError("candidate failure")

        self.failure = migrate
        with self.assertRaisesRegex(manager.Blocked, "automatic rollback blocked"):
            self.switch()
        self.assertEqual(manager.pending_transaction()["phase"], "switching")
        self.assertFalse(self.running)
        self.assertEqual(self.starts, [self.new])
        self.assertEqual(self.db.execute("SELECT * FROM data").fetchall(), [("new data", "v2")])

    def test_candidate_failure_without_migration_restarts_original(self):
        self.failure = lambda: (_ for _ in ()).throw(OSError("candidate failure"))
        with self.assertRaisesRegex(OSError, "candidate failure"):
            self.switch()
        self.assertEqual(self.starts, [self.new, self.old])
        self.assertIsNone(manager.pending_transaction())

    def test_source_health_failure_leaves_recovery_resumable(self):
        def fail_and_mark_unhealthy():
            self.health = False
            raise OSError("candidate failure")

        self.failure = fail_and_mark_unhealthy
        with self.assertRaisesRegex(manager.Blocked, "failed /health"):
            self.switch()
        self.assertEqual(manager.pending_transaction()["phase"], "reverting")
        self.assertEqual(self.starts, [self.new, self.old])
        self.health = True
        manager.recover()
        self.assertEqual(manager.read_transaction()["phase"], "recovered")
        self.assertEqual(self.starts, [self.new, self.old])

    def test_recovery_checks_model_tags_and_actual_image(self):
        self.prepare("switching")
        self.model = "changed"
        with self.assertRaisesRegex(manager.Blocked, "Compose model changed"):
            manager.recover()
        self.model = "model"
        self.actual_image = "unrelated"
        with self.assertRaisesRegex(manager.Blocked, "Unexpected actual image"):
            manager.recover()
        self.actual_image = self.images[self.old]
        self.images[self.old] = "retagged"
        with self.assertRaisesRegex(manager.Blocked, "Original image tag changed"):
            manager.recover()
        self.assertTrue(self.running)
        self.assertEqual(self.starts, [])

    def test_rollback_requires_verified_ids_and_schema(self):
        self.switch()
        prior = manager.read_release()
        with patch.object(manager, "read_release", return_value={
            "previous_image": self.old, "current_image": self.new,
        }), self.assertRaisesRegex(manager.Blocked, "IDs are unverified"):
            manager.rollback(SimpleNamespace(confirm=True))
        self.db.execute("PRAGMA user_version=2")
        with self.assertRaisesRegex(manager.Blocked, "schema compatibility is unverified"):
            manager.rollback(SimpleNamespace(confirm=True))
        self.db.execute("PRAGMA user_version=0")
        manager.rollback(SimpleNamespace(confirm=True))
        self.assertEqual(manager.read_release()["current_id"], prior["previous_id"])


class InstallTransactionTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = root / "parent" / "new-state"
        self.image = "router-orchestrator-hub:fixture"
        self.identifier = "sha256:" + "3" * 64
        self.running = False
        self.starts = 0
        self.ready = True
        self.enterContext(patch.multiple(
            manager, LOCK=root / ".lock", HUB_DEPLOY_ROOT=root, ENV=root / ".env", HUB_UID=os.getuid(),
            HUB_GID=os.getgid(), status_service=lambda: False,
            compose_signature=lambda config: "model", image_id=lambda image: self.identifier,
            health=lambda port, revision=None: self.ready, observed_hub=self.observe, compose_up=self.up,
            compose_stop=lambda: setattr(self, "running", False)))
        self.config = {"RO_IMAGE": self.image, "RO_STATE_DIR": str(self.state),
                       "RO_PUBLIC_URL": "https://test.invalid", "RO_PORT": "18089",
                       "RO_XRAY_BACKEND": "external_readonly"}

    def observe(self, config, images):
        if not self.starts:
            return None
        return {"Config": {"Labels": {}}, "State": {"Status": "running" if self.running else "exited"}}

    def up(self, identifier):
        self.assertEqual(identifier, self.identifier)
        self.running = True
        self.starts += 1
        with contextlib.closing(sqlite3.connect(self.state / "hub.db")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS fixture (value TEXT)")
            db.commit()

    def test_resume_before_or_after_atomic_env_publish(self):
        tx = manager.begin_transaction("install", self.config, self.identifier)
        with patch.object(manager, "save_env", side_effect=OSError("could not publish settings")):
            with self.assertRaisesRegex(OSError, "publish settings"):
                manager.finish_install(tx)
        self.assertEqual(manager.pending_transaction()["phase"], "prepared")
        self.assertFalse(manager.ENV.exists())
        manager.save_env(self.config)  # Process dies just after publishing settings.
        manager.recover()
        self.assertEqual(manager.read_transaction()["phase"], "complete")
        self.assertEqual(self.starts, 1)

    def test_unhealthy_install_preserves_db_and_recovers_same_image(self):
        tx = manager.begin_transaction("install", self.config, self.identifier)
        self.ready = False
        with self.assertRaisesRegex(manager.Blocked, "failed /health"):
            manager.finish_install(tx)
        self.assertFalse(self.running)
        self.assertEqual(manager.pending_transaction()["phase"], "starting")
        with contextlib.closing(sqlite3.connect(self.state / "hub.db")) as db:
            db.execute("INSERT INTO fixture VALUES ('keep')")
            db.commit()
        self.ready = True
        manager.recover()
        with contextlib.closing(sqlite3.connect(self.state / "hub.db")) as db:
            self.assertEqual(db.execute("SELECT value FROM fixture").fetchall(), [("keep",)])
        self.assertIsNone(manager.pending_transaction())

    def test_unexpected_state_before_env_publish_blocks_install_recovery(self):
        manager.begin_transaction("install", self.config, self.identifier)
        self.state.mkdir(mode=0o700, parents=True)
        unrelated = self.state / "unrelated"
        unrelated.write_text("untouched")
        with self.assertRaisesRegex(manager.Blocked, "operator inspection"):
            manager.recover()
        self.assertEqual(unrelated.read_text(), "untouched")
        self.assertFalse(manager.ENV.exists())
        self.assertEqual(self.starts, 0)


class BackupArchiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state = self.root / "state"
        self.state.mkdir()
        self.archives = self.root / "backups"
        self.db = self.enterContext(contextlib.closing(sqlite3.connect(self.state / "hub.db")))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE fleet (name TEXT)")
        self.db.execute("INSERT INTO fleet VALUES ('synthetic-router')")
        self.db.commit()
        (self.state / "nested").mkdir()
        (self.state / "nested" / "hub.db").write_bytes(b"auxiliary file")
        self.enterContext(patch.multiple(manager, BACKUPS=self.archives, LOCK=self.root / ".lock",
                                         HUB_DEPLOY_ROOT=self.root,
                                         HUB_UID=os.getuid(), HUB_GID=os.getgid(),
                                         require_root=lambda: None, status_service=lambda: False,
                                         docker_hub_states=lambda: ["exited"],
                                         state_path=lambda: self.state))

    def test_sqlite_snapshot_restores_wal_data_and_nested_files(self):
        self.assertTrue((self.state / "hub.db-wal").is_file())
        archive = manager.backup()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        dest = self.root / "restored#state"
        manager.restore(SimpleNamespace(destination=dest, archive=archive))
        self.assertEqual({item.name for item in dest.iterdir()}, {"hub.db", "nested"})
        with contextlib.closing(sqlite3.connect(dest / "hub.db")) as restored:
            self.assertEqual(restored.execute("SELECT name FROM fleet").fetchall(), [("synthetic-router",)])
        self.assertEqual((dest / "nested" / "hub.db").read_bytes(), b"auxiliary file")

    def test_file_sync_precedes_publication_and_directory_sync_follows(self):
        events = []
        link = os.link
        fsync = os.fsync

        def track_link(source, destination):
            events.append(("link", Path(destination)))
            link(source, destination)

        def track_sync(fd):
            events.append(("sync", Path(os.readlink(f"/proc/self/fd/{fd}"))))
            fsync(fd)

        with patch.object(manager.os, "link", track_link), \
             patch.object(manager.os, "fsync", track_sync):
            archive = manager.backup()
        published = events.index(("link", archive))
        self.assertEqual(events[published - 1][0], "sync")
        self.assertEqual(events[published - 1][1].name, archive.name)
        self.assertEqual(events[published + 1], ("sync", self.archives))

    def test_failed_archive_write_leaves_no_published_backup(self):
        with patch.object(manager.tarfile.TarFile, "addfile", side_effect=OSError("write failure")):
            with self.assertRaisesRegex(OSError, "write failure"):
                manager.backup()
        self.assertEqual(list(self.archives.iterdir()), [])

    def test_backup_streams_files_and_snapshot_without_read_bytes(self):
        with patch.object(Path, "read_bytes", side_effect=AssertionError("read_bytes is unbounded")):
            manager.backup()

    def test_creation_limits_never_publish_partial_archive(self):
        for limit, value, error in (("MAX_FILES", 1, "Too many backup"),
                                    ("MAX_DATA", 1, "SQLite snapshot too large"),
                                    ("MAX_DATA", 8400, "Backup data too large"),
                                    ("MAX_MANIFEST", 10, "manifest/data too large"),
                                    ("MAX_ARCHIVE", 10, "Compressed backup too large")):
            with self.subTest(limit=limit, value=value), patch.object(manager, limit, value):
                with self.assertRaisesRegex(manager.Blocked, error):
                    manager.backup()
                self.assertEqual(list(self.archives.iterdir()), [])

    def test_restore_rejects_digest_mismatch_and_removes_partial_state(self):
        archive = self.root / "corrupt.tar.gz"
        with manager.tarfile.open(archive, "w:gz") as tar:
            for name, data in (
                ("state/hub.db", b"invalid snapshot"),
                ("manifest.json", json.dumps({"format": 1, "files": {"hub.db": "0" * 64}}).encode()),
            ):
                entry = manager.tarfile.TarInfo(name)
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
        dest = self.root / "restored"
        with self.assertRaisesRegex(manager.Blocked, "Backup digest mismatch"):
            manager.restore(SimpleNamespace(destination=dest, archive=archive))
        self.assertFalse(dest.exists())

    def test_restore_rejects_destination_alias_before_opening_archive(self):
        (self.root / "alias").symlink_to(self.state, target_is_directory=True)
        with patch.object(manager.os, "open") as open_archive:
            with self.assertRaises(manager.Blocked):
                manager.restore(SimpleNamespace(destination=self.root / "alias" / "new",
                                                archive="unused"))
            open_archive.assert_not_called()

    def test_restore_rejects_truncated_gzip_footer(self):
        archive = manager.backup()
        with archive.open("r+b") as stream:
            stream.truncate(archive.stat().st_size - 8)
        dest = self.root / "restored"
        with self.assertRaises(EOFError):
            manager.restore(SimpleNamespace(destination=dest, archive=archive))
        self.assertFalse(dest.exists())

    def test_restore_never_replaces_a_concurrent_destination(self):
        archive = manager.backup()
        dest = self.root / "restored"
        publish = manager.publish_restore

        def concurrent_create(source, target):
            target.mkdir()
            publish(source, target)

        with patch.object(manager, "publish_restore", side_effect=concurrent_create):
            with self.assertRaises(FileExistsError):
                manager.restore(SimpleNamespace(destination=dest, archive=archive))
        self.assertEqual(list(dest.iterdir()), [])
        self.assertEqual(list(self.root.glob(".router-restore-*")), [])

    def test_sigkill_before_restore_publication_leaves_no_destination(self):
        archive = manager.backup()
        dest = self.root / "restored"
        pid = os.fork()
        if pid == 0:
            try:
                with patch.object(manager, "publish_restore",
                                  side_effect=lambda *_: os.kill(os.getpid(), signal.SIGKILL)):
                    manager.restore(SimpleNamespace(destination=dest, archive=archive))
            finally:
                os._exit(1)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(os.WTERMSIG(status), signal.SIGKILL)
        self.assertFalse(dest.exists())
        manager.restore(SimpleNamespace(destination=dest, archive=archive))
        self.assertTrue((dest / "hub.db").is_file())


if __name__ == "__main__":
    unittest.main()
