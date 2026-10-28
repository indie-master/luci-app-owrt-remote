#!/usr/bin/env python3
"""Dry-run WSS migration against a SQLite SNAPSHOT of the running Belt HUB.

No daemon restarts, no production database writes and no live config replacements.
This script stores sensitive candidate JSON and a DB copy in a mode-0700 directory.
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

EXPECTED_ROUTER_COUNT = 5
LEGACY = "owrt-remote-vless"
WSS = "owrt-remote-wss"


class StagingError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise StagingError(message)


def client_index(inbound):
    clients = inbound["settings"]["clients"]
    index = {client["reverse"]["tag"]: client for client in clients}
    require(len(index) == len(clients), "Duplicate reverse client tag")
    return index


def inbound_index(config):
    inbounds = config["inbounds"]
    index = {item["tag"]: item for item in inbounds}
    require(len(index) == len(inbounds), "Duplicate inbound tag")
    return index


def normalized(config):
    """Order-insensitive comparison, without printing UUIDs or other credentials."""
    data = copy.deepcopy(config)
    for item in data.get("inbounds", []):
        clients = item.get("settings", {}).get("clients")
        if isinstance(clients, list):
            clients.sort(key=lambda c: c["reverse"]["tag"])
    data["inbounds"].sort(key=lambda x: x["tag"])
    data["outbounds"].sort(key=lambda x: x["tag"])
    data["routing"]["rules"].sort(key=lambda x: json.dumps(x, sort_keys=True))
    return data


def check_expected(config):
    mapping = inbound_index(config)
    require(LEGACY in mapping and WSS in mapping, "Expected TCP and WSS listeners not present")
    legacy, ws = mapping[LEGACY], mapping[WSS]
    require(legacy["port"] == 18443, "Legacy TCP must remain on :18443")
    require(legacy["streamSettings"]["network"] == "tcp", "Legacy transport changed")
    require(ws["listen"] == "127.0.0.1" and ws["port"] == 18444, "WSS localhost listener changed")
    require(ws["streamSettings"]["network"] == "ws", "WS transport missing")
    require(ws["streamSettings"]["wsSettings"]["path"] == "/owrt-ws/", "WebSocket path changed")
    tcp_clients = client_index(legacy)
    ws_clients = client_index(ws)
    require(len(tcp_clients) == 8, f"Expected 8 legacy clients, got {len(tcp_clients)}")
    require(len(ws_clients) == 2, f"Expected 2 WSS clients, got {len(ws_clients)}")
    base_tags = [tag for tag in ws_clients if tag.startswith("reverse-") and not tag.endswith("-ssh")]
    require(len(base_tags) == 1, "Expected exactly one WSS reverse router")
    ws_router = base_tags[0][len("reverse-"):]
    require(set(ws_clients) == {f"reverse-{ws_router}", f"reverse-{ws_router}-ssh"},
            "The two active WSS client reverse tags do not match")
    require(len(config["routing"]["rules"]) == 10, "Expected 10 reverse routing rules")
    return mapping, ws_router


def run_stage(args):
    live_path = Path(args.live).resolve()
    db_path = Path(args.db).resolve()
    source = Path(args.hub_source).resolve()
    binary = Path(args.xray_bin).resolve()
    require(live_path.is_file() and db_path.is_file(), "Live config or DB does not exist")
    require(source.is_file() and binary.is_file(), "HUB source or Xray binary does not exist")
    require(os.access(binary, os.X_OK), "Xray binary is not executable")

    os.umask(0o077)
    stage = Path(tempfile.mkdtemp(prefix="owrt-wss-v119-", dir=args.output_root)).resolve()
    stage.chmod(0o700)
    print("===== DRY-RUN ONLY =====", flush=True)
    print("Staging directory:", stage, flush=True)

    original = json.loads(live_path.read_text(encoding="utf-8"))
    _, active_ws_router = check_expected(original)
    orig_file = stage / "live-snapshot.json"
    orig_file.write_text(json.dumps(original, ensure_ascii=False, indent=2) + "\n")
    orig_file.chmod(0o600)

    snapshot = stage / "hub-snapshot.db"
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=15) as conn:
        with sqlite3.connect(snapshot) as dest:
            conn.backup(dest)
    snapshot.chmod(0o600)

    with sqlite3.connect(snapshot) as conn:
        names = {item[1] for item in conn.execute("pragma table_info(routers)")}
        require({"id", "vless_port", "deleted_at"} <= names, "Unexpected SQLite schema")
        rows = conn.execute(
            "select id, vless_port from routers where coalesce(deleted_at, 0)=0"
        ).fetchall()
        found = {rid for rid, _ in rows}
        require(len(found) == EXPECTED_ROUTER_COUNT, "Unexpected number of active DB routers")
        require(active_ws_router in found, "The active WSS router is absent from the DB")
        current_inbounds = inbound_index(original)
        for rid in found:
            require(f"entry-{rid}" in current_inbounds and f"ssh-{rid}" in current_inbounds,
                    "An active router is missing entry or SSH in the live config")
        require(all(port == 18443 for _, port in rows),
                "Original DB endpoint ports differ from the verified Belt baseline")

        # Migrate *only the snapshot*. All existing records remain tcp.
        if "vps_transport" not in names:
            conn.execute("alter table routers add column vps_transport text not null default 'tcp'")
        conn.execute("update routers set vps_transport='ws', vless_port=443 where id=?", (active_ws_router,))
        conn.commit()
        migrated = conn.execute(
            "select id, vps_transport, vless_port from routers where coalesce(deleted_at,0)=0"
        ).fetchall()
        require(sum(t == "ws" for _, t, _ in migrated) == 1,
                "The snapshot should contain exactly one WSS router")
        require(all((t == "ws" and p == 443) if rid == active_ws_router else
                    (t == "tcp" and p == 18443) for rid, t, p in migrated),
                "Unexpected transport/port migration in snapshot")

    candidate = stage / "candidate.json"
    controller = original["inbounds"][0]
    require(controller["tag"] == LEGACY, "The first live inbound is not the legacy listener")
    env = os.environ.copy()
    env["OWRT_REMOTE_STATE_DIR"] = str(stage / "private-state")
    env["OWRT_REMOTE_XRAY_CONFIG"] = str(stage / "NO_LIVE_CHANGES.json")
    command = [
        sys.executable, str(source), "--db", str(snapshot), "render-xray",
        "--listen-host", str(controller.get("listen", "0.0.0.0")),
        "--listen-port", str(controller["port"]), "--out", str(candidate),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=35)
    require(result.returncode == 0,
            "HUB render failed (output withheld; inspect local staged files): " +
            str(result.returncode))
    require(candidate.is_file(), "HUB did not create candidate.json")
    candidate.chmod(0o600)
    new = json.loads(candidate.read_text(encoding="utf-8"))
    _, generated_ws_router = check_expected(new)
    require(generated_ws_router == active_ws_router, "Generated WSS router differs from live WSS router")

    # No real UUID/SSH access/routers are allowed to change. Other than
    # ordering of maps/lists, the new generator should reproduce working state.
    identical = normalized(new) == normalized(original)
    live_in, new_in = inbound_index(original), inbound_index(new)
    print("===== REVERSE CONTRACT =====")
    print("Registered routers:", EXPECTED_ROUTER_COUNT)
    print("Legacy TCP clients: 8, port 18443")
    print("WSS clients: 2, local port 18444, public port 443")
    print("LuCI / SSH routing rules: 10")
    print("Transport DB migration performed: SNAPSHOT ONLY")
    print("Semantic match with currently working Xray JSON:", "YES" if identical else "NO")
    if not identical:
        changed = sorted(tag for tag in (set(live_in) | set(new_in))
                         if live_in.get(tag) != new_in.get(tag))
        if changed:
            print("Inbound tags with differences:", ", ".join(changed))
        if original.get("routing") != new.get("routing"):
            print("Routing differs (order may be irrelevant)")
        if original.get("outbounds") != new.get("outbounds"):
            print("Outbounds differ")
        if set(original) != set(new):
            print("Top-level JSON field names differ")
        raise StagingError("Candidate differs from live working configuration. DO NOT DEPLOY.")

    xray = subprocess.run(
        [str(binary), "run", "-test", "-config", str(candidate)],
        capture_output=True, text=True, timeout=30
    )
    log = stage / "xray-check.log"
    log.write_text(xray.stdout + "\n" + xray.stderr, encoding="utf-8")
    log.chmod(0o600)
    require(xray.returncode == 0, "Xray rejected candidate. See private " + str(log))
    print("Xray -test:", "OK")
    print("===== SAFE STAGING PASSED =====")
    print("Changes to live DB, live Xray and running services: NONE")
    print("Private stage files:", stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/var/lib/owrt-remote/hub.db")
    parser.add_argument("--live", default="/etc/xray/owrt-remote.json")
    parser.add_argument("--hub-source", default="vps/owrt-remote-hub.py")
    parser.add_argument("--xray-bin", default="/usr/local/bin/xray")
    parser.add_argument("--output-root", default="/root")
    args = parser.parse_args()
    try:
        run_stage(args)
    except (StagingError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print("STAGING FAILED:", str(exc), file=sys.stderr)
        print("Live configuration and services were not modified by this tool.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
