import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"deploy"))
import active_upgrade_fleet as fleet


def row(admin=True,ssh=True,recent=True):
    return dict(admin=18100,ssh=19100,admin_ok=admin,ssh_ok=ssh,recent=recent,seen=1000)


class FleetTests(unittest.TestCase):
    def test_partial_router_requires_named_acknowledgment(self):
        rows={"main":row(),"netis":row(ssh=False),"offline":row(False,False,False)}
        with self.assertRaisesRegex(RuntimeError,"netis"):
            fleet.baseline_from(rows)
        baseline=fleet.baseline_from(rows,["netis"])
        self.assertEqual(set(baseline),{"main","netis"})
        self.assertTrue(baseline["netis"]["admin_ok"])
        self.assertFalse(baseline["netis"]["ssh_ok"])
        with self.assertRaisesRegex(RuntimeError,"unknown acknowledged router IDs"):
            fleet.baseline_from(rows,["netis","typo"])
        with self.assertRaisesRegex(RuntimeError,"netis"):
            fleet.baseline_from(rows,["offline"])

    def test_acknowledged_router_goes_stale_before_cutover(self):
        rows={"main":row(),"PaxoBs":row(False,False,False)}
        baseline=fleet.baseline_from(rows,["PaxoBs"])
        self.assertIn("PaxoBs",baseline)
        self.assertTrue(fleet.preserved(baseline["PaxoBs"],rows["PaxoBs"],2000))
        self.assertFalse(fleet.preserved(baseline["PaxoBs"],None,2000))

    def test_acknowledged_router_recovers_before_cutover(self):
        rows={"main":row(),"PaxoBs":row()}
        baseline=fleet.baseline_from(rows,["PaxoBs"])
        self.assertTrue(baseline["PaxoBs"]["admin_ok"])
        self.assertTrue(baseline["PaxoBs"]["ssh_ok"])
        self.assertFalse(fleet.preserved(baseline["PaxoBs"],row(ssh=False),900))

    def test_new_unreviewed_degradation_still_blocks(self):
        rows={"main":row(),"PaxoBs":row(False,False,False),"netis":row(ssh=False)}
        with self.assertRaisesRegex(RuntimeError,"netis"):
            fleet.baseline_from(rows,["PaxoBs"])

    def test_every_working_channel_is_required_even_with_stale_heartbeat(self):
        baseline=fleet.baseline_from({"main":row(),"stale":row(recent=False)})
        self.assertIn("stale",baseline)
        self.assertFalse(fleet.preserved(baseline["stale"],row(ssh=False,recent=False),2000))
        self.assertTrue(fleet.preserved(baseline["stale"],row(recent=False),2000))

    def test_degraded_router_cannot_lose_other_channel_or_recent_heartbeat(self):
        before=fleet.baseline_from({"main":row(),"partial":row(ssh=False)},["partial"])["partial"]
        self.assertTrue(fleet.preserved(before,row(ssh=False),900))
        self.assertFalse(fleet.preserved(before,row(False,False),900))
        self.assertFalse(fleet.preserved(before,row(ssh=False),1100))
        self.assertFalse(fleet.preserved(before,dict(row(ssh=False),admin=18001),900))

    def test_no_complete_pair_blocks_upgrade(self):
        with self.assertRaisesRegex(RuntimeError,"no fully working"):
            fleet.baseline_from({"netis":row(ssh=False)},["netis"])

    def test_fragmented_banner_and_prebanner_are_accepted(self):
        sock=MagicMock()
        sock.recv.side_effect=[b"Welcome\r\nS",b"SH-2.0-dropbear\r\n"]
        with patch.object(fleet.socket,"create_connection",return_value=MagicMock(__enter__=lambda s:sock,__exit__=lambda *a:False)):
            self.assertTrue(fleet.ssh_check(19120)[0])

    def test_luci_root_error_retries_canonical_path(self):
        conn=MagicMock()
        conn.getresponse.side_effect=[MagicMock(status=404),MagicMock(status=200)]
        with patch.object(fleet.http.client,"HTTPConnection",return_value=conn):
            self.assertEqual(fleet.admin_check(18120),(True,"HTTP 200"))
        self.assertEqual([c.args[1] for c in conn.request.call_args_list],["/","/cgi-bin/luci/"])


if __name__=="__main__":
    unittest.main()
