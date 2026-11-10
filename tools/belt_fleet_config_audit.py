#!/usr/bin/env python3
"""Read-only guard against drift between Remote HUB SQLite and deployed Xray.

No database writes, no configuration writes, no daemon interaction and no
credential disclosure. The operator supplies which routers actually migrated
to WSS, since a dual-authorized TCP/WSS server config cannot tell which
transport a router is currently using.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = "/var/lib/owrt-remote/hub.db"
DEFAULT_XRAY = "/etc/xray/owrt-remote.json"
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def report(level: str, router: str, reason: str) -> None:
    print(f"{level}: {router}: {reason}")


def get_clients(config: dict, tag: str) -> dict[str, dict]:
    inbounds = [x for x in config.get("inbounds", [])
                if isinstance(x, dict) and x.get("tag") == tag]
    if len(inbounds) != 1:
        raise ValueError(f"expected exactly one {tag} inbound, found {len(inbounds)}")
    inbound = inbounds[0]
    settings = inbound.get("settings", {})
    clients = settings.get("clients", [])
    if not isinstance(clients, list):
        raise ValueError(f"{tag} clients must be an array")
    result = {}
    for client in clients:
        if not isinstance(client, dict) or not isinstance(client.get("email"), str):
            raise ValueError(f"{tag} contains an invalid client object")
        email = client["email"]
        if email in result:
            raise ValueError(f"{tag} contains duplicate client identity {email}")
        result[email] = client
    return result


def load_inventory(db: Path) -> tuple[list[dict], set[str]]:
    # Opening the original SQLite database read-only also observes live WAL
    # entries. Never copy the DB by cp while the HUB is writing it.
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        conn.row_factory = sqlite3.Row
        columns = {r["name"] for r in conn.execute("pragma table_info(routers)")}
        required = {"id", "entry_port", "ssh_entry_port",
                    "vless_uuid", "ssh_vless_uuid"}
        if not required.issubset(columns):
            raise ValueError("HUB routers table lacks required columns: " +
                             ", ".join(sorted(required - columns)))
        fields = sorted(required | ({"vps_transport", "vless_port",
                                     "vps_allow_legacy", "deleted_at"} & columns))
        where = " where coalesce(deleted_at,0)=0" if "deleted_at" in columns else ""
        query = f"select {', '.join(fields)} from routers{where} order by id"
        return [dict(r) for r in conn.execute(query).fetchall()], columns
    finally:
        conn.close()


def audit(db: Path, xray: Path, expected_wss: set[str],
          expected_tcp: set[str]) -> int:
    issues = 0
    try:
        rows, columns = load_inventory(db)
        with xray.open(encoding="utf-8") as src:
            config = json.load(src)
        if not isinstance(config, dict):
            raise ValueError("Xray config root must be an object")
        tcp = get_clients(config, "owrt-remote-vless")
        wss = get_clients(config, "owrt-remote-wss")
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        # Do not print paths or data from SQLite/Xray errors: paths or content
        # might contain sensitive deployment information.
        report("ERROR", "preflight", type(exc).__name__)
        return 2

    inbound_by_tag = {}
    for item in config.get("inbounds", []):
        if isinstance(item, dict):
            inbound_by_tag.setdefault(item.get("tag"), []).append(item)
    rules = [r for r in config.get("routing", {}).get("rules", [])
             if isinstance(r, dict)]
    inventory_ids = set()
    for row in rows:
        rid = str(row["id"])
        inventory_ids.add(rid)
        problems = []
        if not ID_RE.fullmatch(rid):
            problems.append("unsafe router ID")
            report("ERROR", rid, "; ".join(problems))
            issues += 1
            continue
        configured_transport = str(row.get("vps_transport") or "").lower()
        expected = "ws" if rid in expected_wss else "tcp" if rid in expected_tcp else ""
        if expected and "vps_transport" not in columns:
            problems.append("HUB schema has no vps_transport (cannot ensure persistence)")
        elif expected and configured_transport != expected:
            problems.append(f"HUB vps_transport={configured_transport or 'unset'}, expected={expected}")
        if configured_transport and configured_transport not in {"tcp", "ws"}:
            problems.append("unsupported HUB transport")
        if expected == "ws" and "vless_port" in columns:
            try:
                if int(row["vless_port"]) != 443:
                    problems.append("HUB vless_port is not 443 for confirmed WSS")
            except (TypeError, ValueError):
                problems.append("invalid HUB vless_port")
        names = (f"{rid}@owrt-remote", f"{rid}-ssh@owrt-remote")
        uuids = (row.get("vless_uuid"), row.get("ssh_vless_uuid"))
        if not all(isinstance(u, str) and u for u in uuids) or uuids[0] == uuids[1]:
            problems.append("missing or repeated HUB reverse UUIDs")
        for label, uid, suffix in zip(names, uuids, ("", "-ssh")):
            required_transport = "ws" if expected == "ws" else "tcp"
            mandatory = wss if required_transport == "ws" else tcp
            if label not in mandatory:
                problems.append(f"{required_transport} client missing: {'SSH' if suffix else 'LuCI'}")
            elif mandatory[label].get("id") != uid:
                problems.append(f"{required_transport} UUID mismatch: {'SSH' if suffix else 'LuCI'}")
            # Existing fallback authorization must never have mismatched UUIDs.
            for name, pool in (("TCP", tcp), ("WSS", wss)):
                if label in pool and pool[label].get("id") != uid:
                    problems.append(f"{name} registered UUID differs from HUB: {'SSH' if suffix else 'LuCI'}")
        for tag, field, kind in ((f"entry-{rid}", "entry_port", "LuCI"),
                                 (f"ssh-{rid}", "ssh_entry_port", "SSH")):
            inbound = inbound_by_tag.get(tag, [])
            if len(inbound) != 1:
                problems.append(f"{kind} reverse inbound missing/duplicate")
                continue
            try:
                if int(inbound[0].get("port", -1)) != int(row[field]):
                    problems.append(f"{kind} reverse port differs from HUB")
            except (ValueError, TypeError):
                problems.append(f"{kind} reverse port invalid")
            expected_route = "reverse-" + rid + ("-ssh" if kind == "SSH" else "")
            if not any(tag in r.get("inboundTag", []) and
                       r.get("outboundTag") == expected_route for r in rules):
                problems.append(f"{kind} reverse routing rule missing")
        if problems:
            for reason in dict.fromkeys(problems):
                report("ERROR", rid, reason)
                issues += 1
        else:
            report("PASS", rid, f"HUB/Xray identities and routes agree; DB transport={configured_transport or 'unset'}")
    for rid in sorted(expected_wss | expected_tcp):
        if rid not in inventory_ids:
            report("ERROR", rid, "not found in active HUB inventory")
            issues += 1
    if expected_wss & expected_tcp:
        report("ERROR", "arguments", "router listed in both expected transport sets")
        issues += 1
    print(f"SUMMARY: checked={len(rows)}, expected_wss={len(expected_wss)}, "
          f"expected_tcp={len(expected_tcp)}, issues={issues}")
    print("READ ONLY: no files, SQLite rows, Xray processes or services modified.")
    print("NOTE: This checks stored configuration, not active process configuration or live connectivity.")
    return 2 if issues else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(DEFAULT_DB))
    parser.add_argument("--xray", type=Path, default=Path(DEFAULT_XRAY))
    parser.add_argument("--expect-wss", nargs="*", default=[], metavar="ROUTER_ID",
                        help="Router IDs confirmed to use WSS (not inferred from dual auth)")
    parser.add_argument("--expect-tcp", nargs="*", default=[], metavar="ROUTER_ID",
                        help="Router IDs intentionally remaining on TCP")
    args = parser.parse_args(argv)
    return audit(args.db, args.xray, set(args.expect_wss), set(args.expect_tcp))


if __name__ == "__main__":
    sys.exit(main())
