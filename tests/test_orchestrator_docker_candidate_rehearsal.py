"""Candidate Docker migration guardrails; no daemon or Belt service required."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import orchestrator_docker_candidate_rehearsal as candidate


class CandidateRehearsal(unittest.TestCase):
    @staticmethod
    def inventories():
        return {"active_count": 6, "tcp": 1, "wss": 5,
                "tables": {"routers": 6, "router_logs": 5}}

    def test_preserves_fleet_and_auth(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            (state / "agent.token").write_text("secret")
            before = self.inventories()
            cred = [("main", "uuid", "otheruuid", 18101, 19101, "ws", 443, 0)]
            result = candidate.check_portability(
                before, self.inventories(), cred, cred,
                "schema", "schema", {"agent.token": candidate.digest(state / "agent.token")},
                state)
            self.assertTrue(result["schema_unchanged"])
            self.assertEqual(result["router_logs"], 5)

    def test_schema_change_is_visible_and_not_a_pass(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            result = candidate.check_portability(
                self.inventories(), self.inventories(), [], [],
                "old-schema", "new-schema", {}, state)
            self.assertFalse(result["schema_unchanged"])

    def test_rejects_lost_logs_credentials_or_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td)
            (state / "hub-auth.json").write_text("{}")
            data = self.inventories()
            for altered, ids, token in [
                ({**data, "tables": {**data["tables"], "router_logs": 4}}, [], {}),
                (data, ["changed"], {}),
                (data, [], {"hub-auth.json": "wrong-sha"}),
            ]:
                with self.subTest(altered=altered, ids=ids, token=token):
                    with self.assertRaises(candidate.Rejected):
                        candidate.check_portability(
                            data, altered, [], ids,
                            "same", "same", token, state)

    def test_refuses_symlinked_private_backup_parent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real = root / "real"
            real.mkdir()
            fake = root / "shortcut"
            fake.symlink_to(real, target_is_directory=True)
            with self.assertRaises(candidate.Rejected):
                candidate.verify_private_parent(fake)

    def test_requires_exact_commit_and_clean_tree(self):
        head = "d" * 40
        def simulate(*args, **kwargs):
            class Result:
                returncode = 0
                stdout = head + "\n" if args[1] == "rev-parse" else ""
            return Result()
        with patch.object(candidate, "run_cmd", side_effect=simulate):
            self.assertEqual(candidate.verify_checkout(head), head)
        with patch.object(candidate, "run_cmd", side_effect=simulate):
            with self.assertRaises(candidate.Rejected):
                candidate.verify_checkout("e" * 40)

    def test_runner_has_no_host_network_or_docker_socket(self):
        text = (Path(__file__).resolve().parents[1] /
                "tools/orchestrator_docker_candidate_rehearsal.py").read_text()
        self.assertIn('"--network", "none"', text)
        self.assertIn('"--log-driver", "none"', text)
        self.assertIn('"--read-only"', text)
        self.assertIn('"--cap-drop", "ALL"', text)
        self.assertIn('"OWRT_REMOTE_XRAY_CONTROL_BACKEND=external_readonly"', text)
        self.assertNotIn('"/var/run/docker.sock"', text)
        self.assertNotIn('"--privileged"', text)


if __name__ == "__main__":
    unittest.main()
