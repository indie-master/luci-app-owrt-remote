import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("upgrade",ROOT/"deploy/prepare_active_upgrade.py")
upgrade=importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class UpgradePreparationTests(unittest.TestCase):
    def test_only_hub_image_is_changed(self):
        old=b"# comment\nRO_HUB_IMAGE=old\nRO_XRAY_IMAGE=unchanged\nRO_TLS_DIR=/opt/router-orchestrator/tls\n"
        expected=old.replace(b"RO_HUB_IMAGE=old",b"RO_HUB_IMAGE=new")
        self.assertEqual(upgrade.replace_hub(old,"new"),expected)
        self.assertEqual(upgrade.parse_env(expected)["RO_XRAY_IMAGE"],"unchanged")

    def test_duplicate_or_shell_syntax_is_refused(self):
        for data in (b"RO_HUB_IMAGE=old\nRO_HUB_IMAGE=other\n", b"export RO_HUB_IMAGE=old\n",b"not an assignment\n"):
            with self.assertRaises(RuntimeError):
                upgrade.replace_hub(data,"new")

    def test_sqlite_online_backup_preserves_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/"live.db"
            dst=Path(tmp)/"backup.db"
            with sqlite3.connect(src) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE routers(id TEXT)")
                db.execute("INSERT INTO routers VALUES ('netis')")
                db.commit()
                upgrade.snapshot_db(src,dst)
                with sqlite3.connect(dst) as backup:
                    self.assertEqual(backup.execute("SELECT id FROM routers").fetchone()[0],"netis")
            self.assertEqual(dst.stat().st_mode & 0o777,0o600)

    def test_private_artifacts_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"manifest.json"
            upgrade.private_write(path,b"first")
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(FileExistsError):
                upgrade.private_write(path,b"second")
            self.assertEqual(path.read_bytes(),b"first")


if __name__=="__main__":
    unittest.main()
