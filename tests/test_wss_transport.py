"""Regression tests for legacy TCP and WSS reverse compatibility.

Run with: python3 -m unittest discover -s tests -p test_wss_transport.py
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "vps" / "owrt-remote-hub.py"
AGENT = ROOT / "files" / "usr" / "sbin" / "owrt-remote"
STATE = tempfile.TemporaryDirectory(prefix="wss-transport-tests-")
spec = importlib.util.spec_from_file_location("wss_test_hub", SOURCE)
hub = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hub
with mock.patch.dict(os.environ, {"OWRT_REMOTE_STATE_DIR": STATE.name}):
    spec.loader.exec_module(hub)

ADMIN_UUID = "11111111-1111-4111-8111-111111111111"
SSH_UUID = "22222222-2222-4222-8222-222222222222"


class WssHubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wss-db-tests-")
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "hub.db"
        with hub.connect(self.db) as conn:
            hub.init_db(conn)

    def add(self, router_id, transport=None, port=None):
        values = {
            "id": router_id, "name": router_id,
            "entry_port": 18080 if router_id == "old" else 18120,
            "ssh_entry_port": 19080 if router_id == "old" else 19120,
            "vps_host": "hub.example",
            "vless_uuid": ADMIN_UUID if router_id == "old" else "33333333-3333-4333-8333-333333333333",
            "ssh_vless_uuid": SSH_UUID if router_id == "old" else "44444444-4444-4444-8444-444444444444",
        }
        if transport is not None:
            values["vps_transport"] = transport
        if port is not None:
            values["vless_port"] = port
        with hub.connect(self.db) as conn:
            hub.init_db(conn)
            return dict(hub.upsert_router(conn, values))

    def test_new_defaults_to_wss_and_existing_tcp_is_not_migrated(self):
        old = self.add("old", "tcp", 18443)
        fresh = self.add("new")
        self.assertEqual(old["vps_transport"], "tcp")
        self.assertEqual(old["vless_port"], 18443)
        self.assertEqual(fresh["vps_transport"], "ws")
        self.assertEqual(fresh["vless_port"], 443)
        with hub.connect(self.db) as conn:
            old2 = hub.upsert_router(conn, {"id": "old", "name": "renamed"})
        self.assertEqual(old2["vps_transport"], "tcp")
        self.assertEqual(old2["vless_port"], 18443)

    def test_server_separates_clients_preserves_luci_ssh_routes(self):
        self.add("old", "tcp", 18443)
        self.add("new")
        with hub.connect(self.db) as conn:
            rows = hub.list_router_rows(conn)
        config = hub.make_server_xray_config(rows, listen_port=18443)
        by_tag = {entry["tag"]: entry for entry in config["inbounds"]}
        self.assertEqual(len(by_tag["owrt-remote-vless"]["settings"]["clients"]), 2)
        self.assertEqual(len(by_tag["owrt-remote-wss"]["settings"]["clients"]), 2)
        self.assertEqual(by_tag["owrt-remote-vless"]["port"], 18443)
        self.assertEqual(by_tag["owrt-remote-wss"]["listen"], "127.0.0.1")
        self.assertEqual(by_tag["owrt-remote-wss"]["port"], 18444)
        self.assertEqual(by_tag["owrt-remote-wss"]["streamSettings"]["wsSettings"]["path"], "/owrt-ws/")
        for tag in ("entry-old", "ssh-old", "entry-new", "ssh-new"):
            self.assertIn(tag, by_tag)
        self.assertEqual(len(config["routing"]["rules"]), 4)

    def test_staged_wss_migration_keeps_legacy_uuids_until_finalize(self):
        self.add("old", "tcp", 18443)
        self.add("new")
        from argparse import Namespace

        def update(stage=False, finalize=False, direct=False, transport="ws"):
            args = Namespace(db=self.db, id="old", transport=transport,
                             stage=stage, finalize=finalize, direct=direct,
                             sync_xray=True)
            with mock.patch.object(hub, "reload_vps_xray", return_value={"validated": True}):
                hub.cmd_set_router_transport(args)

        update(stage=True)
        with hub.connect(self.db) as conn:
            migrated = dict(hub.get_router(conn, "old"))
            staged_config = hub.make_server_xray_config(hub.list_router_rows(conn), listen_port=18443)
        self.assertEqual(migrated["vps_transport"], "ws")
        self.assertEqual(migrated["vps_allow_legacy"], 1)
        self.assertEqual(migrated["vless_port"], 443)
        inbounds = {x["tag"]: x for x in staged_config["inbounds"]}
        self.assertEqual(len(inbounds["owrt-remote-vless"]["settings"]["clients"]), 2)
        self.assertEqual(len(inbounds["owrt-remote-wss"]["settings"]["clients"]), 4)
        old_tcp = next(x for x in inbounds["owrt-remote-vless"]["settings"]["clients"]
                       if x["reverse"]["tag"] == "reverse-old")
        old_ws = next(x for x in inbounds["owrt-remote-wss"]["settings"]["clients"]
                      if x["reverse"]["tag"] == "reverse-old")
        self.assertEqual(old_tcp, old_ws)
        update(finalize=True)
        with hub.connect(self.db) as conn:
            final = hub.get_router(conn, "old")
            final_config = hub.make_server_xray_config(hub.list_router_rows(conn), listen_port=18443)
        self.assertEqual(final["vps_allow_legacy"], 0)
        inbounds = {x["tag"]: x for x in final_config["inbounds"]}
        self.assertEqual(len(inbounds["owrt-remote-vless"]["settings"]["clients"]), 0)
        self.assertEqual(len(inbounds["owrt-remote-wss"]["settings"]["clients"]), 4)
        self.assertEqual(final["vless_uuid"], ADMIN_UUID)
        self.assertEqual(final["ssh_vless_uuid"], SSH_UUID)

    def test_abrupt_tcp_to_wss_change_is_refused(self):
        self.add("old", "tcp", 18443)
        from argparse import Namespace
        args = Namespace(db=self.db, id="old", transport="ws",
                         stage=False, finalize=False, direct=False, sync_xray=True)
        with self.assertRaises(SystemExit):
            hub.cmd_set_router_transport(args)
        with hub.connect(self.db) as conn:
            row = hub.get_router(conn, "old")
        self.assertEqual(row["vps_transport"], "tcp")
        self.assertEqual(row["vps_allow_legacy"], 0)

    def test_failed_staging_sync_restores_legacy_port_and_transport(self):
        self.add("old", "tcp", 18443)
        from argparse import Namespace
        args = Namespace(db=self.db, id="old", transport="ws",
                         stage=True, finalize=False, direct=False, sync_xray=True)
        with mock.patch.object(hub, "reload_vps_xray", side_effect=RuntimeError("validation error")):
            with self.assertRaises(RuntimeError):
                hub.cmd_set_router_transport(args)
        with hub.connect(self.db) as conn:
            row = hub.get_router(conn, "old")
        self.assertEqual((row["vps_transport"], row["vless_port"], row["vps_allow_legacy"]),
                         ("tcp", 18443, 0))

    def test_config_export_pins_wss_tls_and_preserves_legacy(self):
        old = self.add("old", "tcp", 18443)
        new = self.add("new")
        with mock.patch.object(hub, "agent_token", return_value="test-agent-token"):
            old_cfg = hub.build_openwrt_config_payload(old, "https://hub.example")
            new_cfg = hub.build_openwrt_config_payload(new, "https://hub.example")
            old_lines = "\n".join(hub.build_openwrt_config_lines(old_cfg))
            new_lines = "\n".join(hub.build_openwrt_config_lines(new_cfg))
        self.assertEqual(old_cfg["vps_transport"], "tcp")
        self.assertEqual(new_cfg["vps_transport"], "ws")
        self.assertEqual(new_cfg["vps_sni"], "hub.example")
        self.assertIn("vps_transport='tcp'", old_lines)
        self.assertIn("vps_transport='ws'", new_lines)
        self.assertIn("vps_ws_path='/owrt-ws/'", new_lines)
        self.assertIn("vps_port='443'", new_lines)
        self.assertIn('owrt_saved_xray_bin=', new_lines)
        self.assertIn('if [ -z "$owrt_saved_xray_bin" ]; then', new_lines)
        self.assertIn("uci set owrtremote.main.xray_bin='/usr/bin/xray'", new_lines)
        xray = hub.make_router_xray_config(new)
        outbounds = {o["tag"]: o for o in xray["outbounds"]}
        self.assertEqual(outbounds["vps-interconn"]["streamSettings"]["security"], "tls")
        self.assertEqual(outbounds["vps-ssh-interconn"]["streamSettings"]["network"], "ws")
        self.assertTrue(outbounds["router-admin"]["settings"]["finalRules"])
        legacy = hub.make_router_xray_config(old)
        self.assertEqual(legacy["outbounds"][2]["streamSettings"]["security"], "none")
        self.assertNotIn("finalRules", legacy["outbounds"][0]["settings"])

    def test_invalid_transport_cannot_replace_existing(self):
        self.add("old", "tcp", 18443)
        with hub.connect(self.db) as conn:
            with self.assertRaises(ValueError):
                hub.upsert_router(conn, {"id": "old", "vps_transport": "reality"})
            row = hub.get_router(conn, "old")
        self.assertEqual(row["vps_transport"], "tcp")

    def test_reload_preserves_live_legacy_port_18443(self):
        self.add("old", "tcp", 18443)
        with hub.connect(self.db) as conn:
            rows = hub.list_router_rows(conn)
        live = Path(self.temp.name) / "current.json"
        prior = hub.make_server_xray_config(rows, listen_port=18443)
        live.write_text(json.dumps(prior) + "\n", encoding="utf-8")
        self.add("new")
        from contextlib import nullcontext
        with mock.patch.dict(os.environ, {
            "OWRT_REMOTE_XRAY_CONFIG": str(live),
            "OWRT_REMOTE_XRAY_BIN": "/bin/xray-test",
        }), mock.patch.object(hub.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
             mock.patch.object(hub.socket, "create_connection", return_value=nullcontext()):
            result = hub.reload_vps_xray(self.db)
        self.assertFalse(result["unchanged"])
        config = json.loads(live.read_text(encoding="utf-8"))
        inbounds = {inbound["tag"]: inbound for inbound in config["inbounds"]}
        self.assertEqual(inbounds["owrt-remote-vless"]["port"], 18443)
        self.assertEqual(inbounds["owrt-remote-wss"]["port"], 18444)
        self.assertEqual(len(inbounds["owrt-remote-vless"]["settings"]["clients"]), 2)
        self.assertEqual(len(inbounds["owrt-remote-wss"]["settings"]["clients"]), 2)

    def test_reload_unchanged_skips_restart(self):
        self.add("old", "tcp", 18443)
        with hub.connect(self.db) as conn:
            rows = hub.list_router_rows(conn)
        live = Path(self.temp.name) / "current.json"
        live.write_text(json.dumps(hub.make_server_xray_config(rows, listen_port=18443),
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"OWRT_REMOTE_XRAY_CONFIG": str(live)}), \
             mock.patch.object(hub.subprocess, "run") as call:
            result = hub.reload_vps_xray(self.db)
        self.assertTrue(result["unchanged"])
        call.assert_not_called()

    def test_semantic_json_equal_without_reordering_rules_does_not_restart(self):
        self.add("old", "tcp", 18443)
        with hub.connect(self.db) as conn:
            rows = hub.list_router_rows(conn)
        live = Path(self.temp.name) / "current.json"
        original = hub.make_server_xray_config(rows, listen_port=18443)
        # Same config, deliberately different whitespace and map-key ordering.
        live.write_text(json.dumps(original, sort_keys=True, separators=(",", ":")))
        before = live.read_bytes()
        with mock.patch.dict(os.environ, {"OWRT_REMOTE_XRAY_CONFIG": str(live)}), \
             mock.patch.object(hub.subprocess, "run") as run:
            result = hub.reload_vps_xray(self.db)
        self.assertTrue(result["unchanged"])
        run.assert_not_called()
        self.assertEqual(live.read_bytes(), before)

    def test_failed_candidate_validation_does_not_overwrite_live_xray(self):
        self.add("old", "tcp", 18443)
        live = Path(self.temp.name) / "current.json"
        live.write_text('{"working":true}\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {
            "OWRT_REMOTE_XRAY_CONFIG": str(live),
            "OWRT_REMOTE_XRAY_BIN": shutil.which("false") or "/bin/false",
        }):
            with self.assertRaises(RuntimeError):
                hub.reload_vps_xray(self.db)
        self.assertEqual(live.read_text(), '{"working":true}\n')


@unittest.skipUnless(shutil.which("sh"), "POSIX shell required")
class WssAgentTests(unittest.TestCase):
    def render(self, transport):
        agent = AGENT.read_text(encoding="utf-8")
        prefix = agent.split('cmd="${1:-help}"', 1)[0]
        script = prefix + r'''
uci_get() {
    case "$1" in
        router_id) printf unit-router ;;
        vps_host) printf hub.example ;;
        vps_port) printf ''' + ("443" if transport == "ws" else "18443") + r''' ;;
        vps_transport) printf ''' + transport + r''' ;;
        vps_sni) printf hub.example ;;
        vps_ws_path) printf /owrt-ws/ ;;
        vless_uuid) printf 11111111-1111-4111-8111-111111111111 ;;
        ssh_vless_uuid) printf 22222222-2222-4222-8222-222222222222 ;;
        reverse_tag) printf reverse-in ;;
        ssh_reverse_tag) printf reverse-in-ssh ;;
        wan_direct) printf 0 ;;
        *) printf '%s' "$2" ;;
    esac
}
render_client_config --stdout
'''
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(["sh", "-c", script], capture_output=True,
                                    text=True, env={**os.environ, "TMPDIR": tmp}, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr[-1200:])
        return json.loads(result.stdout)

    def test_wss_protects_two_reverse_links_with_tls_and_final_rules(self):
        config = self.render("ws")
        for tag in ("vps-interconn", "vps-ssh-interconn"):
            outbound = next(o for o in config["outbounds"] if o["tag"] == tag)
            stream = outbound["streamSettings"]
            self.assertEqual(stream["network"], "ws")
            self.assertEqual(stream["security"], "tls")
            self.assertFalse(stream["tlsSettings"]["allowInsecure"])
            self.assertEqual(stream["tlsSettings"]["serverName"], "hub.example")
            self.assertEqual(stream["wsSettings"]["path"], "/owrt-ws/")
        for tag, port in (("router-admin", "80"), ("router-ssh", "22")):
            outbound = next(o for o in config["outbounds"] if o["tag"] == tag)
            rule = outbound["settings"]["finalRules"][0]
            self.assertEqual(rule["action"], "allow")
            self.assertEqual(rule["ip"], ["127.0.0.1/32"])
            self.assertEqual(rule["port"], port)

    def test_legacy_config_does_not_gain_wss_or_final_rules(self):
        config = self.render("tcp")
        for tag in ("vps-interconn", "vps-ssh-interconn"):
            outbound = next(o for o in config["outbounds"] if o["tag"] == tag)
            self.assertEqual(outbound["streamSettings"]["network"], "tcp")
            self.assertEqual(outbound["streamSettings"]["security"], "none")
        for tag in ("router-admin", "router-ssh"):
            outbound = next(o for o in config["outbounds"] if o["tag"] == tag)
            self.assertNotIn("finalRules", outbound["settings"])


if __name__ == "__main__":
    unittest.main()
