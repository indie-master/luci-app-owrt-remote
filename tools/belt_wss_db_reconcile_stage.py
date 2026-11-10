#!/usr/bin/env python3
"""Stage multi-WSS HUB DB reconciliation in private SQLite snapshots, never in production."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


def blocked(msg: str) -> int:
    print("BLOCKED: " + msg, file=sys.stderr)
    return 2


def canonical(config: dict) -> dict:
    """Normalize only client/inbound ordering and disjoint routing rules."""
    result = copy.deepcopy(config)
    inbounds = result["inbounds"]
    for inbound in inbounds:
        clients = (inbound.get("settings") or {}).get("clients")
        if isinstance(clients, list):
            clients.sort(key=lambda x: x.get("email", ""))
    inbounds.sort(key=lambda x: x["tag"])
    # Preserve outbound ordering: the first outbound can be the default.
    rules = result["routing"]["rules"]
    selected = []
    for r in rules:
        if (r.get("type") != "field"
                or not isinstance(r.get("inboundTag"), list)
                or len(r["inboundTag"]) != 1
                or not isinstance(r.get("outboundTag"), str)):
            raise ValueError("routing selector cannot safely be reordered")
        selected.append(r["inboundTag"][0])
    if len(set(selected)) != len(selected):
        raise ValueError("routing has overlapping inbound selectors")
    rules.sort(key=lambda r: r["inboundTag"][0])
    return result


def clients(config: dict, tag: str) -> dict[str, str]:
    matches = [x for x in config["inbounds"] if x.get("tag") == tag]
    if len(matches) != 1:
        raise ValueError("missing/duplicate required inbound")
    pool = {}
    for c in matches[0]["settings"]["clients"]:
        name = c["email"]
        if not isinstance(name, str) or name in pool:
            raise ValueError("invalid/duplicate client registration")
        pool[name] = c["id"]
    return pool


def db_rows(db: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            """select id, vless_uuid, ssh_vless_uuid, vps_transport,
                      vless_port, vps_allow_legacy, entry_port,
                      ssh_entry_port
               from routers where coalesce(deleted_at,0)=0 order by id"""
        ).fetchall()


def check_inventory(rows: list[sqlite3.Row], live: dict, migrating: set[str],
                    existing_ws: set[str], existing_tcp: set[str]) -> None:
    expected = migrating | existing_ws | existing_tcp
    if len(rows) != len(expected) or {r["id"] for r in rows} != expected:
        raise ValueError("active HUB router inventory differs from explicit target set")
    tcp = clients(live, "owrt-remote-vless")
    wss = clients(live, "owrt-remote-wss")
    by_tag = {x.get("tag"): x for x in live["inbounds"]}
    rules = live["routing"]["rules"]
    for row in rows:
        rid = row["id"]
        if not isinstance(rid, str) or not rid or not all(
                c.isalnum() or c in "._-" for c in rid):
            raise ValueError("invalid router identity")
        old = rid in migrating
        ws = old or rid in existing_ws
        expected_transport = "tcp" if old or rid in existing_tcp else "ws"
        expected_port = 18443 if old or rid in existing_tcp else 443
        if row["vps_transport"] != expected_transport or row["vless_port"] != expected_port:
            raise ValueError(f"{rid}: unexpected HUB transport/port before staging")
        for email, uuid_value, suffix in (
            (rid + "@owrt-remote", row["vless_uuid"], ""),
            (rid + "-ssh@owrt-remote", row["ssh_vless_uuid"], "-ssh"),
        ):
            if not isinstance(uuid_value, str) or not uuid_value:
                raise ValueError(f"{rid}: missing HUB credentials")
            for name, pool in (("TCP", tcp), ("WSS", wss)):
                if (name == "TCP" and (old or rid in existing_tcp)) or (
                        name == "WSS" and ws):
                    if pool.get(email) != uuid_value:
                        raise ValueError(f"{rid}: {name} reverse identity mismatch")
                if email in pool and pool[email] != uuid_value:
                    raise ValueError(f"{rid}: {name} registered ID differs from HUB")
            tag = ("ssh-" if suffix else "entry-") + rid
            field = "ssh_entry_port" if suffix else "entry_port"
            if tag not in by_tag or by_tag[tag].get("port") != row[field]:
                raise ValueError(f"{rid}: LuCI/SSH reverse port mismatch")
            expected_out = "reverse-" + rid + suffix
            if not any(tag in (r.get("inboundTag") or []) and
                       r.get("outboundTag") == expected_out for r in rules):
                raise ValueError(f"{rid}: LuCI/SSH routing mismatch")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("/var/lib/owrt-remote/hub.db"))
    ap.add_argument("--live", type=Path, default=Path("/etc/xray/owrt-remote.json"))
    ap.add_argument("--hub-source", type=Path, default=Path("/opt/owrt-remote/owrt-remote-hub.py"))
    ap.add_argument("--xray-bin", type=Path, default=Path("/usr/local/bin/xray"))
    ap.add_argument("--stage-parent", type=Path, default=Path("/root"))
    ap.add_argument("--migrate", nargs="+", required=True)
    ap.add_argument("--existing-wss", nargs="+", required=True)
    ap.add_argument("--leave-tcp", nargs="+", required=True)
    args = ap.parse_args()
    target = set(args.migrate)
    existing_ws = set(args.existing_wss)
    existing_tcp = set(args.leave_tcp)
    if (len(target) != len(args.migrate) or
            len(existing_ws) != len(args.existing_wss) or
            len(existing_tcp) != len(args.leave_tcp) or
            target & existing_ws or target & existing_tcp or existing_ws & existing_tcp):
        return blocked("duplicate or overlapping router IDs")
    try:
        if os.geteuid() != 0:
            raise ValueError("must be root to create private stage")
        for p in (args.db, args.live, args.hub_source, args.xray_bin):
            if not p.is_file() or p.is_symlink():
                raise ValueError("required input missing or is symlink")
        if not os.access(args.xray_bin, os.X_OK):
            raise ValueError("Xray executable unavailable")
        if not args.stage_parent.is_dir() or args.stage_parent.is_symlink():
            raise ValueError("stage directory parent unavailable")
        live_bytes = args.live.read_bytes()
        live = json.loads(live_bytes)
        # Require dual-server inbounds on the deployed configuration.
        tags = {i["tag"]: i for i in live["inbounds"]}
        if tags["owrt-remote-vless"]["port"] != 18443 or (
                tags["owrt-remote-wss"]["port"] != 18444):
            raise ValueError("unexpected server listener ports")
        live_norm = canonical(live)
        directory = Path(tempfile.mkdtemp(prefix="ro-hub-db-parity-", dir=args.stage_parent))
        directory.chmod(0o700)
        db_backup = directory / "db-before.sqlite"
        with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True,
                             timeout=10) as src:
            with sqlite3.connect(db_backup) as dest:
                src.backup(dest)
        db_backup.chmod(0o600)
        rows = db_rows(db_backup)
        check_inventory(rows, live, target, existing_ws, existing_tcp)
        print("SOURCE: HUB SQLite snapshot created; live database not changed")
        print("INVENTORY: " + ", ".join(sorted(target)) + " queued for WSS/443")
        live_hash = hashlib.sha256(live_bytes).digest()
        hub_hash = hashlib.sha256(args.hub_source.read_bytes()).digest()
        matched = []
        for legacy in (0, 1):
            scenario = directory / f"scenario-legacy-{legacy}.sqlite"
            shutil.copyfile(db_backup, scenario)
            scenario.chmod(0o600)
            with sqlite3.connect(scenario) as conn:
                conn.execute("begin immediate")
                for rid in sorted(target):
                    cur = conn.execute(
                        """update routers set vps_transport='ws', vless_port=443,
                           vps_allow_legacy=? where id=? and vps_transport='tcp'
                           and vless_port=18443 and coalesce(deleted_at,0)=0""",
                        (legacy, rid))
                    if cur.rowcount != 1:
                        raise ValueError("staged router update did not match old state")
                conn.commit()
            candidate = directory / f"render-legacy-{legacy}.json"
            result = subprocess.run(
                [sys.executable, str(args.hub_source), "--db", str(scenario),
                 "render-xray", "--listen-host",
                 tags["owrt-remote-vless"].get("listen", "0.0.0.0"),
                 "--listen-port", "18443", "--out", str(candidate)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=45, check=False)
            if result.returncode or not candidate.is_file():
                print(f"LEGACY={legacy}: renderer failed on isolated snapshot (details suppressed)")
                continue
            candidate.chmod(0o600)
            try:
                data = json.loads(candidate.read_bytes())
                equivalent = canonical(data) == live_norm
            except (ValueError, KeyError, TypeError):
                equivalent = False
            print(f"LEGACY={legacy}: generated config parity " +
                  ("MATCH" if equivalent else "DIFFERS"))
            if equivalent:
                syntax = subprocess.run(
                    [str(args.xray_bin), "run", "-test", "-config", str(candidate)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    timeout=30, check=False)
                print(f"LEGACY={legacy}: Xray syntax " +
                      ("PASS" if syntax.returncode == 0 else "FAIL"))
                if syntax.returncode == 0:
                    matched.append(legacy)
        if hashlib.sha256(args.live.read_bytes()).digest() != live_hash:
            raise ValueError("live Xray config changed while staging")
        if hashlib.sha256(args.hub_source.read_bytes()).digest() != hub_hash:
            raise ValueError("HUB source changed while staging")
        print("STAGE: " + str(directory))
        print("PRODUCTION: unchanged (SQLite, Xray, services, routers)")
        if not matched:
            print("RESULT: BLOCKED — no generated candidate exactly matches live Xray")
            return 2
        print("RESULT: STAGED MATCH — legacy fallback mode(s): " +
              ",".join(map(str, matched)) + "; no production update performed")
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error,
            subprocess.TimeoutExpired) as exc:
        # Never echo source content, UUIDs, database path or raw generator errors.
        return blocked(type(exc).__name__ + " during private staging")


if __name__ == "__main__":
    sys.exit(main())
