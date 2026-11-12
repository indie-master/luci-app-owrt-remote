#!/usr/bin/env python3
"""Read-only Belt/Router Orchestrator Docker migration inventory and rehearsal snapshot.

NEVER stops/starts anything, never changes the live SQLite DB, Xray JSON,
HUB files, HTTP endpoint or Nginx. Snapshot is for a staging rehearsal,
NOT a live database replacement after active writes resume.

Protected state includes ALL regular files under --state-dir (not just hub.db)
so sessions, auth, notifications, VAPID, traffic counters and unrecognized
future state are preserved. SQLite is copied with the online backup API.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tarfile
import tempfile
from typing import Any

DB_NAME = "hub.db"
SQLITE_TRANSIENT = {"hub.db-wal", "hub.db-shm", "hub.db-journal"}
SERVICE_FILES = {
    "hub_systemd": "/etc/systemd/system/owrt-remote.service",
    "xray_systemd": "/etc/systemd/system/owrt-remote-xray.service",
}
MUST_KEEP = ("hub-auth.json", "hub-sessions.json", "agent.token",
             "hub-vapid-private.pem", "hub-notifications.json",
             ".xray-maintenance-hold")
MAX_MANIFEST = 4 * 1024 * 1024


class GateError(RuntimeError):
    pass


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise GateError(reason)


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for part in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def _ro_connection(path: Path) -> sqlite3.Connection:
    require(path.is_file() and not path.is_symlink(), "SQLite source missing/unsafe")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                           uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma query_only=on")
    return conn


def db_inventory(path: Path) -> dict:
    with _ro_connection(path) as conn:
        require(conn.execute("pragma integrity_check").fetchone()[0] == "ok",
                "SQLite integrity_check failed")
        tables = [r[0] for r in conn.execute(
            "select name from sqlite_master where type='table' "
            "and name not like 'sqlite_%' order by name")]
        require("routers" in tables, "HUB DB lacks routers table")
        columns = {r[1] for r in conn.execute("pragma table_info(routers)")}
        needed = {"id", "vless_uuid", "ssh_vless_uuid", "entry_port",
                  "ssh_entry_port", "vps_transport", "vless_port", "deleted_at"}
        require(needed <= columns, "HUB schema missing WSS/router identity columns")
        fallback_expr = "vps_allow_legacy" if "vps_allow_legacy" in columns else "0"
        rows = [dict(row) for row in conn.execute(
            "select id, vless_uuid, ssh_vless_uuid, entry_port, ssh_entry_port, "
            f"vps_transport, vless_port, deleted_at, {fallback_expr} as vps_allow_legacy "
            "from routers order by id")]
        active = [row for row in rows if not row.get("deleted_at")]
        counts = {}
        for name in tables:
            safe_name = '"' + name.replace('"', '""') + '"'
            counts[name] = conn.execute("select count(*) from " + safe_name).fetchone()[0]
    require(active, "no active routers")
    require(len({r["id"] for r in active}) == len(active), "duplicate router IDs")
    require(all(r["vps_transport"] in ("ws", "tcp") for r in active),
            "unsupported transport in active HUB DB")
    return {"tables": counts, "routers": active,
            "active_count": len(active),
            "tcp": sum(r["vps_transport"] == "tcp" for r in active),
            "wss": sum(r["vps_transport"] == "ws" for r in active),
            "deleted_count": len(rows) - len(active)}


def xray_parity(inventory: dict, config: dict) -> None:
    """Require exact two reverse identities and two loopback portals per router."""
    inbounds = config.get("inbounds") or []
    rules = (config.get("routing") or {}).get("rules") or []
    require(isinstance(inbounds, list) and isinstance(rules, list),
            "Xray inbounds/rules must be lists")
    tags = [x.get("tag") for x in inbounds]
    require(len(tags) == len(set(tags)), "Xray duplicate inbound tags")
    by_tag = {i["tag"]: i for i in inbounds}

    def clients(tag):
        inbound = by_tag.get(tag)
        require(inbound is not None, "missing server transport inbound: " + tag)
        return (inbound.get("settings") or {}).get("clients") or []

    for row in inventory["routers"]:
        rid = str(row["id"])
        mode = row["vps_transport"]
        required = "owrt-remote-wss" if mode == "ws" else "owrt-remote-vless"
        expected_port = 443 if mode == "ws" else 18443
        require(int(row["vless_port"]) == expected_port,
                "HUB DB transport/port mismatch: " + rid)
        user_entries = (
            (rid + "@owrt-remote", row["vless_uuid"]),
            (rid + "-ssh@owrt-remote", row["ssh_vless_uuid"]),
        )
        require(all(v and isinstance(v, str) for _, v in user_entries),
                "empty credentials for: " + rid)
        require(user_entries[0][1] != user_entries[1][1],
                "LuCI and SSH credentials equal: " + rid)
        for email, identifier in user_entries:
            matches = [c for c in clients(required)
                       if c.get("email") == email and c.get("id") == identifier]
            require(len(matches) == 1,
                    "missing/duplicate live Xray identity: " + email)
        endpoints = (("entry-" + rid, "reverse-" + rid, row["entry_port"]),
                     ("ssh-" + rid, "reverse-" + rid + "-ssh",
                      row["ssh_entry_port"]))
        for tag, outbound, p in endpoints:
            require(tag in by_tag, "missing live reverse listener: " + tag)
            inbound = by_tag[tag]
            require(inbound.get("protocol") == "tunnel"
                    and inbound.get("listen") == "127.0.0.1"
                    and int(inbound.get("port") or 0) == int(p),
                    "unexpected reverse listener: " + tag)
            matching = [r for r in rules if tag in (r.get("inboundTag") or [])
                        and r.get("outboundTag") == outbound]
            require(len(matching) == 1, "missing reverse route: " + tag)

    require("owrt-remote-vless" in by_tag and
            "owrt-remote-wss" in by_tag,
            "original TCP and WSS inbounds must both be retained")
    # Explicitly reject credentials and reverse endpoints that do not
    # correspond to active SQLite rows. A re-generated Docker config
    # would silently drop these, potentially cutting off old routers.
    expected = {"owrt-remote-vless": {}, "owrt-remote-wss": {}}
    expected_tunnel_tags = set()
    all_uuids = set()
    for row in inventory["routers"]:
        rid = str(row["id"])
        selected = "owrt-remote-wss" if row["vps_transport"] == "ws" else "owrt-remote-vless"
        for suffix, uuid in (("", row["vless_uuid"]), ("-ssh", row["ssh_vless_uuid"])):
            require(uuid not in all_uuids, "duplicate identity across active router fleet")
            all_uuids.add(uuid)
            email = rid + suffix + "@owrt-remote"
            expected[selected][email] = uuid
            if selected == "owrt-remote-wss" and row["vps_allow_legacy"]:
                # A manually preserved transitional TCP fallback is allowed,
                # but is NOT required to exist after the successful WSS switch.
                expected["owrt-remote-vless"][email] = uuid
        expected_tunnel_tags.update(("entry-" + rid, "ssh-" + rid))
    for transport in expected:
        client_rows = clients(transport)
        identities = {}
        for client in client_rows:
            email = client.get("email")
            require(isinstance(email, str) and email not in identities,
                    "duplicate/nameless authentication in " + transport)
            identities[email] = client.get("id")
        for email, uuid in identities.items():
            require(expected[transport].get(email) == uuid,
                    "unexpected/old/changed identity in " + transport + ": " + email)
        # All mandatory identities were checked per router earlier.
    reverse_tags = {tag for tag in tags if tag.startswith("entry-") or tag.startswith("ssh-")}
    require(reverse_tags == expected_tunnel_tags,
            "unexpected or missing reverse tunnel listeners in live Xray")



def analyze(state: Path, xray: Path) -> tuple[dict, dict]:
    require(state.is_dir() and not state.is_symlink(), "state dir missing/unsafe")
    require(xray.is_file() and not xray.is_symlink(), "Xray JSON missing/unsafe")
    inventory = db_inventory(state / DB_NAME)
    config = json.loads(xray.read_text(encoding="utf-8"))
    xray_parity(inventory, config)
    return inventory, config


def record_state_files(state: Path) -> list[tuple[Path, str]]:
    """All file content, including nested future state; no symlink traversals."""
    found = []
    for root, dirs, files in os.walk(state, topdown=True, followlinks=False):
        parent = Path(root)
        for name in dirs:
            p = parent / name
            require(stat.S_ISDIR(p.lstat().st_mode) and not p.is_symlink(),
                    "state tree contains link/special directory")
        for name in files:
            p = parent / name
            relative = p.relative_to(state).as_posix()
            require(stat.S_ISREG(p.lstat().st_mode) and not p.is_symlink(),
                    "state tree contains link/socket/special file")
            if relative in ({DB_NAME} | SQLITE_TRANSIENT):
                continue
            # All other database-looking files must be identified and
            # copied transactionally; do not archive an unsafe raw SQLite.
            require(not relative.lower().endswith((".db", ".sqlite", ".sqlite3")),
                    "unexpected nested SQLite file requires custom backup: " + relative)
            found.append((p, "state/" + relative))
    return sorted(found, key=lambda x: x[1])


def online_db_backup(source: Path, output: Path) -> None:
    with _ro_connection(source) as src:
        with sqlite3.connect(str(output)) as dst:
            src.backup(dst, pages=128, sleep=0.05)
    output.chmod(0o600)
    require(db_inventory(output)["active_count"] > 0,
            "rehearsal DB snapshot failed integrity/inventory checks")


def _entry(tar: tarfile.TarFile, path: Path, name: str,
           digests: dict[str, str]) -> None:
    require(path.is_file() and not path.is_symlink(),
            "archive member missing/unsafe")
    info = tarfile.TarInfo(name)
    st = path.stat()
    info.size = st.st_size
    info.mtime = int(st.st_mtime)
    info.mode = stat.S_IMODE(st.st_mode) & 0o777
    with path.open("rb") as stream:
        tar.addfile(info, stream)
    digests[name] = file_sha(path)


def snapshot(state: Path, xray: Path, hub_source: Path,
             output_root: Path) -> tuple[Path, dict]:
    """Private staging snapshot while services remain RUNNING. Not final cutover."""
    inventory, _ = analyze(state, xray)
    require(hub_source.is_file() and not hub_source.is_symlink(),
            "installed HUB source missing/unsafe")
    require(output_root.is_dir() and not output_root.is_symlink(),
            "existing snapshot root required")
    files = record_state_files(state)
    dest = Path(tempfile.mkdtemp(prefix="orchestrator-docker-rehearsal-",
                                 dir=output_root))
    dest.chmod(0o700)
    snap_db = dest / "hub.db.snapshot.sqlite"
    online_db_backup(state / DB_NAME, snap_db)
    again = db_inventory(snap_db)
    # The live source may receive heartbeats during online backup; the
    # snapshot itself must be logically consistent.
    require(again["active_count"] == inventory["active_count"],
            "active fleet changed while building snapshot; retry later")
    require({r["id"] for r in again["routers"]} ==
            {r["id"] for r in inventory["routers"]},
            "router inventory changed while backing up")
    archive = dest / "state-and-references.tar.gz"
    manifest = {
        "schema": "orchestrator-docker-rehearsal/1",
        "kind": "REHEARSAL_ONLY_NOT_CUTOVER",
        "database": {"tables": again["tables"], "routers": again["active_count"],
                     "wss": again["wss"], "tcp": again["tcp"],
                     "deleted": again["deleted_count"]},
        "required_files_present": {name: (state / name).is_file()
                                   for name in MUST_KEEP},
        "files_sha256": {},
        "no_live_service_changes": True,
        "instructions": "Do not restore this archive over a live database after any new writes",
    }
    require(all(manifest["required_files_present"].values()),
            "required auth/session/token/notification/hold file is missing: "
            + ", ".join(k for k, v in manifest["required_files_present"].items()
                        if not v))
    fd = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as raw:
        with tarfile.open(fileobj=raw, mode="w|gz") as tar:
            _entry(tar, snap_db, "state/hub.db", manifest["files_sha256"])
            for path, name in files:
                _entry(tar, path, name, manifest["files_sha256"])
            _entry(tar, xray, "reference/xray.json", manifest["files_sha256"])
            _entry(tar, hub_source, "reference/hub-source.py",
                   manifest["files_sha256"])
    snap_db.unlink()
    manifest_file = dest / "manifest.json"
    manifest_file.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_file.chmod(0o600)
    return dest, manifest


def inspect_archive(folder: Path) -> dict:
    """Validate tar contents without extracting or writing to a live path."""
    manifest_path = folder / "manifest.json"
    archive = folder / "state-and-references.tar.gz"
    require(manifest_path.is_file() and archive.is_file(), "snapshot files absent")
    require(manifest_path.stat().st_size <= MAX_MANIFEST, "manifest too large")
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("schema") == "orchestrator-docker-rehearsal/1",
            "unexpected backup schema")
    expected = manifest.get("files_sha256")
    require(isinstance(expected, dict) and "state/hub.db" in expected
            and "reference/xray.json" in expected, "incomplete manifest")
    seen: dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            require(member.isfile() and not member.name.startswith("/")
                    and ".." not in member.name.split("/")
                    and member.name not in seen,
                    "unexpected/special/duplicate archive member")
            stream = tar.extractfile(member)
            require(stream is not None, "archive member cannot be read")
            digest = hashlib.sha256()
            for buf in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(buf)
            seen[member.name] = digest.hexdigest()
    require(seen == expected, "archive differs from snapshot manifest")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for mode in ("audit", "snapshot"):
        cmd = sub.add_parser(mode)
        cmd.add_argument("--state-dir", type=Path,
                         default=Path("/var/lib/owrt-remote"))
        cmd.add_argument("--xray-config", type=Path,
                         default=Path("/etc/xray/owrt-remote.json"))
        if mode == "snapshot":
            cmd.add_argument("--hub-source", type=Path,
                             default=Path("/opt/owrt-remote/owrt-remote-hub.py"))
            cmd.add_argument("--output-root", type=Path, default=Path("/root"))
    check_cmd = sub.add_parser("verify")
    check_cmd.add_argument("folder", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "verify":
            m = inspect_archive(args.folder)
            print("VERIFY: PASS | archived files:", len(m["files_sha256"]))
            print("FLEET:", m["database"]["routers"], "active routers")
        elif args.command == "audit":
            d, _ = analyze(args.state_dir, args.xray_config)
            print(f"AUDIT: PASS | routers={d['active_count']} "
                  f"WSS={d['wss']} TCP={d['tcp']} "
                  f"deleted={d['deleted_count']} | tables={len(d['tables'])}")
            print("NO SECRETS PRINTED; ALL LIVE FILES UNCHANGED")
        else:
            folder, m = snapshot(args.state_dir, args.xray_config,
                                 args.hub_source, args.output_root)
            inspect_archive(folder)
            print("REHEARSAL: PASS | private archive:", folder)
            print(f"ACTIVE ROUTERS: {m['database']['routers']} | "
                  f"TABLES: {len(m['database']['tables'])} | "
                  f"FILES: {len(m['files_sha256'])}")
            print("DO NOT RESTORE OVER AN ACTIVE DATABASE; NO SERVICE STOP/RESTART")
        return 0
    except (GateError, OSError, ValueError, TypeError, KeyError, sqlite3.Error,
            tarfile.TarError, json.JSONDecodeError) as exc:
        print("BLOCKED: " + str(exc) if isinstance(exc, GateError)
              else "BLOCKED: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
