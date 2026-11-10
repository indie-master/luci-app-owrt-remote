#!/usr/bin/env python3
"""Guarded, explicit HUB SQLite multi-WSS reconciliation.

Only updates transport, port and legacy-authorization for nominated routers.
Requires an independently verified private stage with an exact Xray generator
match. Does NOT write server Xray, restart services, touch routers or remove
the Xray maintenance hold. Durable backup + guarded targeted rollback.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request

import belt_wss_db_reconcile_stage as stage

TRANSIENT = {"last_seen", "status_json", "updated_at"}
HUB_SERVICE = "owrt-remote"
XRAY_SERVICE = "owrt-remote-xray"


class Blocked(Exception):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise Blocked(message)


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def systemctl(*args: str) -> str:
    p = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=8)
    require(p.returncode == 0, "systemd service is unavailable or inactive")
    return p.stdout.strip()


def service_pid(service: str) -> int:
    require(systemctl("is-active", "--quiet", service) == "", "service inactive")
    pid = int(systemctl("show", "--property=MainPID", "--value", service))
    require(pid > 1, "service has no MainPID")
    return pid


def health() -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8088/health", timeout=3) as response:
            obj = json.loads(response.read(4096))
        require(obj.get("ok") is True, "HUB health is not OK")
    except (OSError, ValueError) as exc:
        raise Blocked("HUB health endpoint unavailable") from exc


def guard_services(db: Path, live: Path, hub: Path) -> tuple[int, int, str, str]:
    health()
    hp = service_pid(HUB_SERVICE)
    xp = service_pid(XRAY_SERVICE)
    # Ensure HUB uses this state directory before placing its protection hold.
    environ = Path(f"/proc/{hp}/environ").read_bytes().split(b"\x00")
    state = next((v.split(b"=", 1)[1] for v in environ
                  if v.startswith(b"OWRT_REMOTE_STATE_DIR=")),
                 b"/var/lib/owrt-remote")
    require(Path(os.fsdecode(state)).resolve() == db.parent.resolve(),
            "HUB state directory differs from SQLite parent")
    return hp, xp, file_sha(live), file_sha(hub)


def private_stage(folder: Path) -> None:
    require(folder.name.startswith("ro-hub-db-parity-"), "unexpected stage name")
    require(folder.parent.resolve() == Path("/root"), "stage must be under /root")
    require(folder.is_dir() and not folder.is_symlink(), "stage directory unavailable")
    st = folder.stat()
    require(st.st_uid == 0 and (st.st_mode & 0o077) == 0,
            "stage must be root-owned and inaccessible to other users")
    for name in ("db-before.sqlite", "scenario-legacy-1.sqlite",
                 "render-legacy-1.json"):
        path = folder / name
        require(path.is_file() and not path.is_symlink() and path.stat().st_uid == 0,
                "stage contains missing/untrusted artefact")


def config_rows(path: Path) -> dict[str, dict]:
    with sqlite3.connect(path, timeout=10) as db:
        db.row_factory = sqlite3.Row
        cols = [c[1] for c in db.execute("pragma table_info(routers)")]
        required = {"id", "vps_transport", "vless_port", "vps_allow_legacy",
                    "vless_uuid", "ssh_vless_uuid", "entry_port", "ssh_entry_port"}
        require(required.issubset(cols), "HUB schema lacks required columns")
        rows = db.execute("select * from routers order by id").fetchall()
        return {str(row["id"]): {k: row[k] for k in cols if k not in TRANSIENT}
                for row in rows}


def baseline_matches(actual: dict, expected: dict) -> None:
    require(actual == expected, "HUB SQLite configuration changed after staging")


def changed_rows(before: dict, after: dict, targets: set[str]) -> None:
    require(set(before) == set(after), "staged router set differs from backup")
    for rid, old in before.items():
        desired = dict(old)
        if rid in targets:
            require(old["vps_transport"] == "tcp"
                    and old["vless_port"] == 18443, "unexpected old transport")
            desired.update(vps_transport="ws", vless_port=443, vps_allow_legacy=1)
        require(after[rid] == desired,
                "stage changes fields outside approved WSS migration")


def validate_stage(folder: Path, db: Path, live: Path, targets: set[str],
                   existing_wss: set[str], existing_tcp: set[str],
                   xray_bin: Path) -> dict:
    baseline = config_rows(folder / "db-before.sqlite")
    staged = config_rows(folder / "scenario-legacy-1.sqlite")
    changed_rows(baseline, staged, targets)
    baseline_matches(config_rows(db), baseline)
    source = json.loads(live.read_bytes())
    expected = json.loads((folder / "render-legacy-1.json").read_bytes())
    require(stage.canonical(source) == stage.canonical(expected),
            "staged Xray render no longer agrees with live config")
    rows = stage.db_rows(folder / "db-before.sqlite")
    stage.check_inventory(rows, source, targets, existing_wss, existing_tcp)
    p = subprocess.run([str(xray_bin), "run", "-test", "-config",
                        str(folder / "render-legacy-1.json")],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=30)
    require(p.returncode == 0, "staged Xray candidate syntax invalid")
    return baseline


def backup(db: Path, dest: Path) -> None:
    require(not dest.exists(), "existing backup must not be overwritten")
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as src:
        with sqlite3.connect(dest) as out:
            src.backup(out)
    dest.chmod(0o600)


def update_transport(db: Path, before: dict, after: dict,
                     targets: set[str]) -> None:
    # Preserve heartbeats, last_seen and all unrelated configuration.
    with sqlite3.connect(db, timeout=10, isolation_level=None) as conn:
        conn.execute("begin immediate")
        try:
            baseline_matches(config_rows_in_tx(conn), before)
            for rid in sorted(targets):
                old_row = before[rid]
                next_row = after[rid]
                old = (old_row["vps_transport"], old_row["vless_port"],
                       old_row["vps_allow_legacy"])
                new = (next_row["vps_transport"], next_row["vless_port"],
                       next_row["vps_allow_legacy"])
                cur = conn.execute(
                    """update routers set vps_transport=?, vless_port=?,
                       vps_allow_legacy=? where id=?
                       and vps_transport=? and vless_port=?
                       and vps_allow_legacy=? and coalesce(deleted_at,0)=0""",
                    (*new, rid, *old),
                )
                require(cur.rowcount == 1, "concurrent router change detected")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def config_rows_in_tx(conn: sqlite3.Connection) -> dict[str, dict]:
    conn.row_factory = sqlite3.Row
    cols = [c[1] for c in conn.execute("pragma table_info(routers)")]
    return {str(row["id"]): {k: row[k] for k in cols if k not in TRANSIENT}
            for row in conn.execute("select * from routers order by id")}


def render(hub: Path, db: Path, live: Path, folder: Path,
           xray_bin: Path) -> None:
    live_config = json.loads(live.read_bytes())
    output = folder / "render-post-apply.json"
    state_dir = folder / "isolated-post-apply"
    state_dir.mkdir(mode=0o700, exist_ok=True)
    env = dict(os.environ)
    env["OWRT_REMOTE_STATE_DIR"] = str(state_dir)
    env["OWRT_REMOTE_DB"] = str(db)
    p = subprocess.run(
        [sys.executable, str(hub), "--db", str(db), "render-xray",
         "--listen-host", str(next(
             i["listen"] for i in live_config["inbounds"]
             if i.get("tag") == "owrt-remote-vless")),
         "--listen-port", "18443", "--out", str(output)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=str(folder), timeout=40)
    require(p.returncode == 0 and output.is_file(), "installed HUB renderer failed")
    output.chmod(0o600)
    require(stage.canonical(json.loads(output.read_bytes())) ==
            stage.canonical(live_config), "post-commit generated Xray differs from live")
    p = subprocess.run([str(xray_bin), "run", "-test", "-config", str(output)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    require(p.returncode == 0, "post-commit generated Xray rejected")


def make_hold(path: Path) -> None:
    if path.exists():
        require(path.is_file() and not path.is_symlink(), "unexpected existing maintenance hold")
        print("HOLD: existing hold preserved")
        return
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise Blocked("maintenance hold created concurrently")
    with os.fdopen(fd, "w") as f:
        f.write("multi-WSS SQLite reconciliation; do not remove before operator verification\n")
        f.flush()
        os.fsync(f.fileno())
    print("HOLD: created to prevent HUB Xray regeneration; remains after completion")


def run(args: argparse.Namespace) -> int:
    require(os.geteuid() == 0, "root required")
    targets, ws, tcp = set(args.migrate), set(args.existing_wss), set(args.leave_tcp)
    require(targets and ws and tcp, "all three router sets required")
    require(len(targets) == len(args.migrate) and len(ws) == len(args.existing_wss)
            and len(tcp) == len(args.leave_tcp), "duplicate router identity")
    require(not (targets & ws or targets & tcp or ws & tcp), "router sets overlap")
    for p in (args.db, args.live, args.hub_source, args.xray_bin):
        require(p.is_file() and not p.is_symlink(), "required live path is missing")
    require(args.db.parent.resolve() == Path("/var/lib/owrt-remote"),
            "unexpected HUB state location")
    require(args.live.resolve() == Path("/etc/xray/owrt-remote.json"),
            "unexpected Xray configuration location")
    private_stage(args.stage)

    lock_path = Path("/run/lock/ro-wss-db-reconcile.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        hp, xp, live_hash, hub_hash = guard_services(
            args.db, args.live, args.hub_source)
        baseline = validate_stage(
            args.stage, args.db, args.live, targets, ws, tcp, args.xray_bin)
        print("PREFLIGHT: PASS — live SQLite and Xray agree with staged baseline")
        if not args.apply:
            print("READ ONLY: append --apply after review; no changes")
            return 0

        # Existing working config must never be regenerated while data is updated.
        make_hold(args.db.parent / ".xray-maintenance-hold")
        saved = args.stage / "apply-before.sqlite"
        backup(args.db, saved)
        require(config_rows(saved) == baseline,
                "live DB changed while creating backup")
        require(file_sha(args.live) == live_hash and
                file_sha(args.hub_source) == hub_hash,
                "production source changed during preflight")

        committed = False
        try:
            expected = config_rows(args.stage / "scenario-legacy-1.sqlite")
            update_transport(args.db, baseline, expected, targets)
            committed = True
            baseline_matches(config_rows(args.db), expected)
            render(args.hub_source, args.db, args.live, args.stage, args.xray_bin)
            require(file_sha(args.live) == live_hash and
                    file_sha(args.hub_source) == hub_hash,
                    "Xray JSON or HUB source changed unexpectedly")
            require(service_pid(HUB_SERVICE) == hp and
                    service_pid(XRAY_SERVICE) == xp,
                    "HUB/Xray process changed during DB reconciliation")
            health()
        except Exception:
            if committed:
                try:
                    # Revert ONLY the three fields for the nominated routers.
                    now = config_rows(args.db)
                    expected = config_rows(args.stage / "scenario-legacy-1.sqlite")
                    baseline_matches(now, expected)
                    update_transport(args.db, expected, baseline, targets)
                    print("ROLLBACK: restored previous transport fields; hold retained")
                except Exception:
                    print("CRITICAL: automatic targeted rollback blocked; inspect private backup",
                          file=sys.stderr)
            raise
        print("APPLIED: three WSS rows committed; HUB and Xray PID unchanged")
        print("PARITY: HUB renderer matches live Xray; TCP fallback retained")
        print("BACKUP: " + str(saved))
        print("HOLD: retained; do not remove until outside-in LuCI/SSH acceptance")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", type=Path, required=True)
    ap.add_argument("--db", type=Path, default=Path("/var/lib/owrt-remote/hub.db"))
    ap.add_argument("--live", type=Path, default=Path("/etc/xray/owrt-remote.json"))
    ap.add_argument("--hub-source", type=Path, default=Path("/opt/owrt-remote/owrt-remote-hub.py"))
    ap.add_argument("--xray-bin", type=Path, default=Path("/usr/local/bin/xray"))
    ap.add_argument("--migrate", nargs="+", required=True)
    ap.add_argument("--existing-wss", nargs="+", required=True)
    ap.add_argument("--leave-tcp", nargs="+", required=True)
    ap.add_argument("--apply", action="store_true", help="explicitly commit guarded DB changes")
    args = ap.parse_args()
    try:
        return run(args)
    except (Blocked, OSError, ValueError, KeyError, TypeError, sqlite3.Error,
            subprocess.TimeoutExpired, BlockingIOError) as exc:
        print("BLOCKED: " + type(exc).__name__ + "; no server Xray/service changes made",
              file=sys.stderr)
        if isinstance(exc, Blocked):
            print("REASON: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
