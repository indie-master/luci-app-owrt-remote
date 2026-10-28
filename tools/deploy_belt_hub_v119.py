#!/usr/bin/env python3
"""Guarded Belt Hub v119+WSS code upgrade. The Xray service/binary is never modified.

Modes:
  check     Read-only preflight against production and private staged DB.
  apply     Back up, stop ONLY the Hub, migrate existing WS record, verify
            generated Xray JSON, replace Hub Python, start and health-check.
  rollback  Stop ONLY the Hub, restore pre-upgrade Python and SQLite snapshot,
            start and health-check. Never restarts Xray.

The program is deliberately specific to the verified five-router Belt contract.
It fails closed if the DB and current Xray configuration have drifted.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import fcntl

LEGACY = "owrt-remote-vless"
WS = "owrt-remote-wss"
HUB_SERVICE = "owrt-remote"
XRAY_SERVICE = "owrt-remote-xray"


class UpgradeError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise UpgradeError(message)


def sha256(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def call(command, check=True, timeout=35):
    p = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    if check and p.returncode:
        raise UpgradeError("Command failed: " + " ".join(command[:3]) +
                           " (exit=" + str(p.returncode) + "; see local journal)")
    return p


def is_active(service):
    return call(["systemctl", "is-active", "--quiet", service], check=False).returncode == 0


def pid(service):
    result = call(["systemctl", "show", "--property=MainPID", "--value", service])
    try:
        value = int(result.stdout.strip())
    except ValueError:
        value = 0
    require(value > 0, "No running MainPID for " + service)
    return value


def health(revision=None, timeout=24):
    until = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while time.monotonic() < until:
        try:
            with opener.open("http://127.0.0.1:8088/health?panel-update=1", timeout=2) as resp:
                payload = json.loads(resp.read(4096))
            if payload.get("ok") is True and (revision is None or payload.get("revision") == revision):
                return True
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(1)
    return False


def check_listen(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def stage_module(source):
    path = source.parent.parent / "tools" / "stage_belt_wss.py"
    require(path.is_file(), "Missing companion stage_belt_wss.py in checked-out repository")
    spec = importlib.util.spec_from_file_location("belt_staging", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, path


def active_ws_router(live, stage):
    _, name = stage.check_expected(live)
    return name


def inspect(args):
    source = args.source.resolve()
    installed = args.installed.resolve()
    db_path = args.db.resolve()
    xray_config = args.xray_config.resolve()
    require(source.is_file() and source != installed, "Reviewed candidate Python file is missing or is live")
    require(installed.is_file(), "Installed Hub Python file missing")
    require(db_path.is_file() and xray_config.is_file(), "Hub DB or Xray config missing")
    require(args.xray_bin.is_file(), "Installed Xray executable missing")
    require(os.geteuid() == 0, "Must be run as root")
    require(is_active(XRAY_SERVICE), "Xray service is not active")
    require(is_active(HUB_SERVICE), "Hub service is not active")
    require(health(), "Hub /health is not responding")
    require(check_listen(18443) and check_listen(18444), "TCP or WSS listener is absent")
    stage, stage_script = stage_module(source)
    live = json.loads(xray_config.read_text(encoding="utf-8"))
    ws_router = active_ws_router(live, stage)
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        names = {r[1] for r in conn.execute("pragma table_info(routers)")}
        require({"id", "deleted_at", "vless_port", "vless_uuid", "ssh_vless_uuid"} <= names,
                "Unexpected original SQLite schema")
        rows = conn.execute(
            "select id, vless_port from routers where coalesce(deleted_at,0)=0"
        ).fetchall()
        require(len(rows) == 5 and {r[0] for r in rows}.__contains__(ws_router),
                "Router set changed since Belt staging")
        if "vps_transport" in names:
            for row in conn.execute("select id,vps_transport from routers where coalesce(deleted_at,0)=0"):
                require(row[1] == ("ws" if row[0] == ws_router else "tcp"),
                        "Unexpected transport state in live DB")
        for router_id, port in rows:
            require(port == (443 if router_id == ws_router else 18443) or
                    (router_id == ws_router and port == 18443),
                    "Legacy TCP port unexpectedly changed")
    require(call([sys.executable, "-m", "py_compile", str(source)]).returncode == 0,
            "Candidate has Python syntax errors")
    return {
        "source": source,
        "installed": installed,
        "db": db_path,
        "xray_config": xray_config,
        "stage": stage,
        "stage_script": stage_script,
        "ws_router": ws_router,
        "xray_pid": pid(XRAY_SERVICE),
        "xray_sha": sha256(args.xray_bin),
        "xray_json_sha": sha256(xray_config),
        "live": live,
    }


def perform_staging(args, info):
    command = [
        sys.executable, str(info["stage_script"]),
        "--hub-source", str(info["source"]),
        "--db", str(info["db"]),
        "--live", str(info["xray_config"]),
        "--xray-bin", str(args.xray_bin),
        "--output-root", str(args.backup_root),
        "--simulate-fallback",
    ]
    proc = call(command, timeout=75)
    require("SAFE STAGING PASSED" in proc.stdout,
            "Dry-run staging did not report success")
    print("STAGING: PASS (8 TCP, 2 WSS; fallback validated)", flush=True)


def atomic_install(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".owrt-hub-upgrade-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as out, Path(source).open("rb") as inp:
            shutil.copyfileobj(inp, out)
            out.flush()
            os.fsync(out.fileno())
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o755
        os.chmod(name, mode)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sqlite_backup(source, destination):
    with sqlite3.connect(source) as read:
        with sqlite3.connect(destination) as write:
            read.backup(write)
    os.chmod(destination, 0o600)


def verify_xray_untouched(args, info):
    require(is_active(XRAY_SERVICE), "Xray service stopped unexpectedly")
    require(pid(XRAY_SERVICE) == info["xray_pid"], "Xray process PID changed")
    require(sha256(args.xray_bin) == info["xray_sha"], "Xray executable changed")
    require(sha256(info["xray_config"]) == info["xray_json_sha"],
            "Live Xray JSON was unexpectedly overwritten")
    require(check_listen(18443) and check_listen(18444),
            "One of the reverse listeners is no longer available")


def migrate_db(db_path, ws_router):
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute("begin immediate")
        cols = {row[1] for row in conn.execute("pragma table_info(routers)")}
        if "vps_transport" not in cols:
            conn.execute("alter table routers add column vps_transport text not null default 'tcp'")
        if "vps_allow_legacy" not in cols:
            conn.execute("alter table routers add column vps_allow_legacy integer not null default 0")
        row = conn.execute("select id, vless_port from routers where id=? and coalesce(deleted_at,0)=0",
                           (ws_router,)).fetchone()
        require(row is not None and row[1] in (18443, 443), "Current WSS router DB entry is unexpected")
        conn.execute("update routers set vps_transport='ws',vless_port=443,vps_allow_legacy=0 where id=?",
                     (ws_router,))
        conn.execute("update routers set vps_transport='tcp',vps_allow_legacy=0 where id<>? and coalesce(deleted_at,0)=0",
                     (ws_router,))
        conn.commit()


def validate_render(args, info, dest):
    live = info["live"]
    legacy = next(x for x in live["inbounds"] if x["tag"] == LEGACY)
    command = [
        sys.executable, str(info["source"]), "--db", str(info["db"]), "render-xray",
        "--listen-host", legacy.get("listen", "0.0.0.0"),
        "--listen-port", str(legacy["port"]), "--out", str(dest),
    ]
    result = call(command, timeout=35)
    require(dest.is_file(), "New Hub did not generate a candidate Xray JSON")
    candidate = json.loads(dest.read_text(encoding="utf-8"))
    stage = info["stage"]
    require(stage.normalized(candidate) == stage.normalized(live),
            "New Hub generated different Xray JSON; refusing deployment")
    test = call([str(args.xray_bin), "run", "-test", "-config", str(dest)], timeout=30)
    require(test.returncode == 0, "Xray rejected render")


def backup_files(args, info, folder):
    folder.mkdir(mode=0o700)
    shutil.copy2(info["installed"], folder / "hub-before.py")
    shutil.copy2(info["xray_config"], folder / "xray-before.json")
    shutil.copy2(args.xray_bin, folder / "xray-before.bin")
    sqlite_backup(info["db"], folder / "db-before.sqlite")
    (folder / "owrt-hub-unit.txt").write_text(
        call(["systemctl", "cat", HUB_SERVICE]).stdout, encoding="utf-8"
    )
    (folder / "owrt-xray-unit.txt").write_text(
        call(["systemctl", "cat", XRAY_SERVICE]).stdout, encoding="utf-8"
    )
    metadata = {
        "installed": str(info["installed"]),
        "db": str(info["db"]),
        "xray_config": str(info["xray_config"]),
        "xray_sha": info["xray_sha"],
        "xray_json_sha": info["xray_json_sha"],
        "xray_pid": info["xray_pid"],
        "hub_before_sha": sha256(info["installed"]),
        "source_sha": sha256(info["source"]),
    }
    (folder / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    for name in ("manifest.json", "owrt-hub-unit.txt", "owrt-xray-unit.txt",
                 "xray-before.json", "xray-before.bin", "db-before.sqlite", "hub-before.py"):
        os.chmod(folder / name, 0o600)


def restore_from_backup(args, folder):
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    require(is_active(XRAY_SERVICE), "Refusing rollback: Xray is already stopped")
    call(["systemctl", "stop", HUB_SERVICE])
    require(not is_active(HUB_SERVICE), "Hub did not stop for rollback")
    atomic_install(folder / "hub-before.py", Path(manifest["installed"]))
    with sqlite3.connect(folder / "db-before.sqlite") as src:
        with sqlite3.connect(manifest["db"]) as dest:
            src.backup(dest)
    call(["systemctl", "start", HUB_SERVICE])
    require(is_active(HUB_SERVICE) and health(timeout=25),
            "Rollback restarted Hub but health check failed")
    require(sha256(manifest["xray_config"]) == manifest["xray_json_sha"],
            "Xray JSON differs from pre-upgrade backup")
    require(sha256(args.xray_bin) == manifest["xray_sha"],
            "Xray executable differs from pre-upgrade backup")
    print("ROLLBACK: previous Hub Python and database restored.", flush=True)


def apply(args):
    info = inspect(args)
    perform_staging(args, info)
    require(args.backup_root.is_dir(), "Backup root missing")
    backup_dir = Path(tempfile.mkdtemp(prefix="owrt-hub-rollback-", dir=args.backup_root))
    backup_dir.chmod(0o700)
    print("Backup directory:", backup_dir, flush=True)
    changed = False
    try:
        # Quiesce Hub writers before making the production DB snapshot.
        call(["systemctl", "stop", HUB_SERVICE])
        changed = True
        require(not is_active(HUB_SERVICE), "Hub did not stop")
        verify_xray_untouched(args, info)
        backup_files(args, info, backup_dir / "files")
        files = backup_dir / "files"
        migrate_db(info["db"], info["ws_router"])
        validate_render(args, info, files / "candidate.json")
        atomic_install(info["source"], info["installed"])
        require(sha256(info["installed"]) == sha256(info["source"]),
                "Installed Hub Python differs from source")
        call(["systemctl", "start", HUB_SERVICE])
        require(health(revision=sha256(info["source"]), timeout=30),
                "New Hub revision did not report expected /health")
        verify_xray_untouched(args, info)
        print("UPGRADE: PASS. HUB v119+WSS is running.", flush=True)
        print("Core unchanged. Router UUIDs unchanged. No Xray restart requested.", flush=True)
        print("Rollback snapshot:", files, flush=True)
    except BaseException:
        if changed:
            files = backup_dir / "files"
            if (files / "manifest.json").exists():
                print("Upgrade failed; restoring previous Hub and DB", file=sys.stderr, flush=True)
                restore_from_backup(args, files)
            else:
                call(["systemctl", "start", HUB_SERVICE], check=False)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["check", "apply", "rollback"])
    parser.add_argument("--source", type=Path, required=True,
                        help="Path to reviewed v119+WSS Hub source in a pinned checkout")
    parser.add_argument("--backup", type=Path, default=None,
                        help="For rollback: path to the upgrade backup's files directory")
    parser.add_argument("--backup-root", type=Path, default=Path("/root"))
    parser.add_argument("--installed", type=Path, default=Path("/opt/owrt-remote/owrt-remote-hub.py"))
    parser.add_argument("--db", type=Path, default=Path("/var/lib/owrt-remote/hub.db"))
    parser.add_argument("--xray-config", type=Path, default=Path("/etc/xray/owrt-remote.json"))
    parser.add_argument("--xray-bin", type=Path, default=Path("/usr/local/bin/xray"))
    args = parser.parse_args()
    lock_path = Path("/run/lock/owrt-wss-hub-upgrade.lock")
    try:
        require(os.geteuid() == 0, "Run as root")
        lock_path.parent.mkdir(exist_ok=True, parents=True)
        with lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.mode == "rollback":
                require(args.backup is not None and args.backup.is_dir(),
                        "Supply an existing --backup folder")
                restore_from_backup(args, args.backup)
            else:
                info = inspect(args)
                if args.mode == "check":
                    perform_staging(args, info)
                    print("CHECK: PASS. Nothing installed or restarted.", flush=True)
                else:
                    # Avoid duplicated preflight between main and apply, but
                    # revalidate immediately before touching the service.
                    apply(args)
    except (UpgradeError, OSError, ValueError, RuntimeError, subprocess.TimeoutExpired,
            sqlite3.Error, BlockingIOError) as exc:
        print("ABORT:", str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
