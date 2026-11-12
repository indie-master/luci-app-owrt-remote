#!/usr/bin/env python3
"""Guarded onboarding of ONE new WSS router already created in HUB SQLite.

Dry-run by default; --install is explicit and restarts ONLY owrt-remote-xray.
No credentials are displayed. Never removes HUB's Xray maintenance hold.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile

from belt_wss_db_reconcile_stage import canonical

ID = re.compile(r"^[A-Za-z0-9_.-]+$")
XRAY_SERVICE = "owrt-remote-xray.service"


class Blocked(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def active_inventory(db_path):
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro",
                         uri=True, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            select id, vless_uuid, ssh_vless_uuid, vps_transport, vless_port,
                   vps_allow_legacy, entry_port, ssh_entry_port,
                   admin_host, admin_port, ssh_host, ssh_port,
                   vless_flow, deleted_at
            from routers order by id
        """
        return {str(x["id"]): dict(x) for x in conn.execute(query)
                if not x["deleted_at"]}


def server_inbound(config, tag):
    result = [x for x in config.get("inbounds", []) if x.get("tag") == tag]
    require(len(result) == 1, "expected exactly one " + tag + " inbound")
    return result[0]


def target_removed(candidate, router_id, row):
    """Prune *only* the two WSS clients, inbounds and routes of one router.

    The remaining config must exactly equal deployed Xray after safe
    normalization; any unrelated drift or partial registration is blocked.
    """
    config = copy.deepcopy(candidate)
    tcp = server_inbound(config, "owrt-remote-vless")
    wss = server_inbound(config, "owrt-remote-wss")
    require(tcp.get("port") == 18443 and wss.get("port") == 18444
            and wss.get("listen") == "127.0.0.1",
            "unexpected legacy/WSS server listener")
    require(wss.get("streamSettings", {}).get("network") == "ws"
            and wss.get("streamSettings", {}).get("wsSettings", {}).get("path") == "/owrt-ws/",
            "unexpected WSS server transport")
    names = {
        router_id + "@owrt-remote": row["vless_uuid"],
        router_id + "-ssh@owrt-remote": row["ssh_vless_uuid"],
    }
    for inbound in config["inbounds"]:
        pool = (inbound.get("settings") or {}).get("clients")
        if not isinstance(pool, list):
            continue
        selected = [c for c in pool if c.get("email") in names]
        if inbound is wss:
            require(len(selected) == 2, "candidate WSS registration is incomplete")
            for client in selected:
                require(client.get("id") == names[client["email"]],
                        "WSS UUID does not match HUB DB")
        else:
            require(not selected, "new WSS router unexpectedly registered elsewhere")
    wss["settings"]["clients"] = [
        c for c in wss["settings"]["clients"] if c.get("email") not in names
    ]
    tags = ("entry-" + router_id, "ssh-" + router_id)
    outbound_tags = ("reverse-" + router_id, "reverse-" + router_id + "-ssh")
    specs = (
        (tags[0], row["entry_port"], row["admin_host"], row["admin_port"]),
        (tags[1], row["ssh_entry_port"], row["ssh_host"], row["ssh_port"]),
    )
    for tag, port, host, destination in specs:
        item = server_inbound(config, tag)
        require(item.get("listen") == "127.0.0.1"
                and item.get("port") == port
                and item.get("protocol") == "tunnel",
                "unexpected new reverse listener")
        settings = item.get("settings") or {}
        require(settings.get("allowedNetwork") == "tcp"
                and settings.get("rewriteAddress") == host
                and settings.get("rewritePort") == destination,
                "new reverse tunnel target mismatch")
    config["inbounds"] = [
        x for x in config["inbounds"] if x.get("tag") not in tags
    ]
    rules = config["routing"]["rules"]
    for tag, outbound in zip(tags, outbound_tags):
        found = [x for x in rules if x.get("inboundTag") == [tag]]
        require(len(found) == 1 and found[0].get("outboundTag") == outbound,
                "new reverse routing mismatch")
    config["routing"]["rules"] = [
        x for x in rules if x.get("inboundTag") not in ([tags[0]], [tags[1]])
    ]
    return config


def check_candidate(live, candidate, row, router_id):
    require(row["vps_transport"] == "ws" and row["vless_port"] == 443
            and row["vps_allow_legacy"] == 0,
            "router must be a fresh WSS/443 record without TCP fallback")
    require(row["entry_port"] != row["ssh_entry_port"]
            and 1 <= int(row["entry_port"]) <= 65535
            and 1 <= int(row["ssh_entry_port"]) <= 65535,
            "invalid/duplicate reverse ports")
    require(row["vless_uuid"] and row["ssh_vless_uuid"]
            and row["vless_uuid"] != row["ssh_vless_uuid"],
            "invalid/duplicate HUB credentials")
    # Already registered: don't change or restart anything.
    if canonical(live) == canonical(candidate):
        return "already-registered"
    previous = target_removed(candidate, router_id, row)
    require(canonical(previous) == canonical(live),
            "live Xray differs beyond this one new router; refusing enrollment")
    return "new"


def copy_db_live(src, dst):
    require(not dst.exists(), "backup destination already exists")
    with sqlite3.connect(src.resolve().as_uri() + "?mode=ro", uri=True,
                         timeout=10) as source:
        with sqlite3.connect(dst) as target:
            source.backup(target)
    dst.chmod(0o600)


def service_up():
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", XRAY_SERVICE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=6)
    require(result.returncode == 0, "dedicated Xray service not active")
    for port in (18443, 18444):
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass


def make_hold(path):
    if path.exists():
        require(path.is_file() and not path.is_symlink(),
                "unsafe existing maintenance hold")
        return "existing"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("single WSS router enrollment; keep until acceptance\n")
        f.flush()
        os.fsync(f.fileno())
    return "created"


def restart_xray():
    result = subprocess.run(["systemctl", "restart", XRAY_SERVICE],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=30, check=False)
    require(result.returncode == 0, "dedicated Xray restart failed")
    service_up()


def install_candidate(candidate, live, backup, target_hash):
    require(digest(live) == target_hash, "live Xray config changed during stage")
    require(not backup.exists(), "backup already exists")
    shutil.copy2(live, backup)
    backup.chmod(0o600)
    fd, tmp_path = tempfile.mkstemp(prefix=".new-wss-", suffix=".json",
                                   dir=str(live.parent))
    tmp = Path(tmp_path)
    installed = False
    try:
        tmp.write_bytes(candidate.read_bytes())
        os.fchmod(fd, live.stat().st_mode & 0o777)
        os.fchown(fd, live.stat().st_uid, live.stat().st_gid)
        os.close(fd)
        fd = -1
        require(digest(live) == target_hash, "live Xray modified concurrently")
        os.replace(tmp, live)
        installed = True
        try:
            restart_xray()
        except Exception:
            # Restore exact previous JSON and try to bring back old service.
            rollback = live.parent / (".new-wss-rollback-" + str(os.getpid()) + ".json")
            try:
                shutil.copy2(backup, rollback)
                os.replace(rollback, live)
                restart_xray()
                print("ROLLBACK: previous server Xray restored; hold retained")
            except Exception:
                print("CRITICAL: restore service/config manually from backup", file=sys.stderr)
            raise
    finally:
        if fd != -1:
            os.close(fd)
        if tmp.exists():
            tmp.unlink()
    require(installed, "failed to install candidate")


def run(args):
    require(os.geteuid() == 0, "run as root on Belt")
    require(ID.fullmatch(args.router_id) is not None, "invalid router ID")
    for path in (args.db, args.live, args.hub_source, args.xray_bin):
        require(path.is_file() and not path.is_symlink(), "missing/unsafe input")
    require(os.access(args.xray_bin, os.X_OK), "Xray binary not executable")
    service_up()
    lock_path = Path("/run/lock/owrt-new-wss-enroll.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        live_hash = digest(args.live)
        hub_hash = digest(args.hub_source)
        live = json.loads(args.live.read_bytes())
        root = Path(tempfile.mkdtemp(prefix="owrt-new-wss-", dir="/root"))
        root.chmod(0o700)
        snapshot = root / "snapshot.sqlite"
        copy_db_live(args.db, snapshot)
        inventory = active_inventory(snapshot)
        require(args.router_id in inventory, "router not found in active HUB DB")
        row = inventory[args.router_id]
        candidate = root / "candidate.json"
        state = root / "isolated-state"
        state.mkdir(mode=0o700)
        env = dict(os.environ)
        env["OWRT_REMOTE_STATE_DIR"] = str(state)
        env["OWRT_REMOTE_DB"] = str(snapshot)
        tcp = server_inbound(live, "owrt-remote-vless")
        result = subprocess.run([
            sys.executable, str(args.hub_source), "--db", str(snapshot),
            "render-xray", "--listen-host", str(tcp.get("listen", "0.0.0.0")),
            "--listen-port", str(tcp["port"]), "--out", str(candidate),
        ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
           cwd=str(root), timeout=50)
        require(result.returncode == 0 and candidate.is_file(),
                "installed HUB renderer failed")
        candidate.chmod(0o600)
        generated = json.loads(candidate.read_bytes())
        status = check_candidate(live, generated, row, args.router_id)
        syntax = subprocess.run(
            [str(args.xray_bin), "run", "-test", "-config", str(candidate)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        require(syntax.returncode == 0, "candidate Xray syntax rejected")
        require(digest(args.live) == live_hash and
                digest(args.hub_source) == hub_hash,
                "server Xray or HUB source changed during stage")
        print("PREFLIGHT: PASS | target=" + args.router_id + " | " + status)
        print("CANDIDATE: validated against full existing router fleet")
        print("STAGE: " + str(root))
        if status == "already-registered":
            print("NOOP: both reverse channels already enrolled")
            return 0
        if not args.install:
            print("DRY RUN: no live Xray config or service changed")
            return 0
        require(active_inventory(args.db) == inventory,
                "HUB router inventory changed during stage")
        hold_state = make_hold(args.db.parent / ".xray-maintenance-hold")
        backup = root / "server-before.json"
        install_candidate(candidate, args.live, backup, live_hash)
        require(canonical(json.loads(args.live.read_bytes())) ==
                canonical(generated), "post-install server config mismatch")
        print("ENROLLED: " + args.router_id + " (WSS/443), two reverse channels")
        print("XRAY: dedicated service restarted and listeners checked")
        print("BACKUP: " + str(backup))
        print("HOLD: " + hold_state + " (retained; never removed automatically)")
        print("CHECK: new router stays offline until it connects; verify LuCI and SSH")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--router-id", required=True)
    p.add_argument("--db", type=Path, default=Path("/var/lib/owrt-remote/hub.db"))
    p.add_argument("--live", type=Path, default=Path("/etc/xray/owrt-remote.json"))
    p.add_argument("--hub-source", type=Path, default=Path("/opt/owrt-remote/owrt-remote-hub.py"))
    p.add_argument("--xray-bin", type=Path, default=Path("/usr/local/bin/xray"))
    p.add_argument("--install", action="store_true")
    args = p.parse_args()
    try:
        return run(args)
    except (Blocked, OSError, ValueError, KeyError, TypeError,
            sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print("BLOCKED: " + str(exc) if isinstance(exc, Blocked)
              else "BLOCKED: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
