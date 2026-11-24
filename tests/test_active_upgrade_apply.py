import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"deploy"))
import apply_active_upgrade as upgrade


class UpgradeApplyTests(unittest.TestCase):
    def test_absent_new_units_do_not_block_stop(self):
        present=set(upgrade.TIMERS[:2])
        present.update(t.removesuffix(".timer")+".service" for t in upgrade.TIMERS[:2])
        with patch.object(upgrade,"unit_exists",side_effect=lambda unit:unit in present), \
             patch.object(upgrade,"run") as run,patch.object(upgrade,"check_quiescent") as check:
            upgrade.stop_workers()
        calls=[c.args[2] for c in run.call_args_list]
        self.assertEqual(set(calls),present)
        check.assert_called_once()

    def test_external_nginx_change_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=[Path(tmp)/name for name in ("xray","map","nginx")]
            for path in paths:
                path.write_bytes(b"original")
            record={key:upgrade.digest(b"original") for key in ("xray_sha","map_sha","nginx_sha")}
            record["nginx_full_sha"]="a"*64
            with patch.object(upgrade,"XRAY",paths[0]),patch.object(upgrade,"NGINX",paths[1]), \
                 patch.object(upgrade,"EXTERNAL_NGINX",paths[2]), \
                 patch.object(upgrade,"nginx_digest",return_value="b"*64):
                with self.assertRaisesRegex(RuntimeError,"include tree changed"):
                    upgrade.check_files(record)

    def test_unit_install_and_restore_preserves_old_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            units=root/"units"
            src=root/"src"
            stage=root/"stage"
            units.mkdir()
            (src/"deploy/systemd").mkdir(parents=True)
            (stage/"activation-units").mkdir(parents=True)
            names=("old.service","new.service")
            (units/names[0]).write_bytes(b"old")
            (stage/"activation-units"/names[0]).write_bytes(b"old")
            for name in names:
                (src/"deploy/systemd"/name).write_bytes(b"candidate")
            with patch.object(upgrade,"UNITS",units),patch.object(upgrade,"SRC",src), \
                 patch.object(upgrade,"UNIT_NAMES",names),patch.object(upgrade,"run"), \
                 patch.object(upgrade,"atomic",side_effect=lambda p,d,*args:p.write_bytes(d)):
                upgrade.install_units(stage)
                self.assertEqual((units/names[1]).read_bytes(),b"candidate")
                upgrade.install_units(stage,restore=True)
                self.assertEqual((units/names[0]).read_bytes(),b"old")
                self.assertFalse((units/names[1]).exists())

    def test_recovery_never_clears_foreign_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"hold"
            path.write_bytes(b"other")
            with patch.object(upgrade.life,"TRANSPORT_HOLD",path):
                with self.assertRaisesRegex(RuntimeError,"ownership changed"):
                    upgrade.release_hold()
            self.assertEqual(path.read_bytes(),b"other")


if __name__=="__main__":
    unittest.main()
