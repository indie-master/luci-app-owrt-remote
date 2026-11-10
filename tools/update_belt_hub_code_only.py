#!/usr/bin/env python3
"""Guarded code-only update of an already migrated Belt HUB (v119+WSS).

Never changes the Xray binary, Xray JSON, service unit, or maintenance HOLD.
Never calls the Hub's Xray reload method. The SQLite DB is *not migrated*;
a private backup is retained solely for emergency rollback.

Modes: check, apply, rollback (--backup <private files directory>).
"""
import argparse
import fcntl
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

HUB_SERVICE = "owrt-remote"
XRAY_SERVICE = "owrt-remote-xray"
HOLD = ".xray-maintenance-hold"


class GuardError(Exception):
    pass


def need(ok, reason):
    if not ok:
        raise GuardError(reason)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as inp:
        for part in iter(lambda: inp.read(1048576), b""):
            h.update(part)
    return h.hexdigest()


def execute(*cmd, timeout=35, check=True):
    result = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise GuardError("Failed: " + " ".join(cmd[:3]) + " (rc=" + str(result.returncode) + ")")
    return result


def active(service):
    return execute("systemctl", "is-active", "--quiet", service, check=False).returncode == 0


def pid(service):
    raw = execute("systemctl", "show", "--property=MainPID", "--value", service).stdout.strip()
    return int(raw or "0")


def health(revision=None, seconds=25):
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    limit = time.monotonic() + seconds
    while time.monotonic() < limit:
        try:
            with client.open("http://127.0.0.1:8088/health?panel-update=1", timeout=2) as resp:
                data = json.loads(resp.read(4096))
            if data.get("ok") is True and (revision is None or data.get("revision") == revision):
                return True
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(1)
    return False


def listen(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def snapshot_db(db, target):
    with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(target) as dest:
            src.backup(dest)
    target.chmod(0o600)


def restore_db(backup, target):
    with sqlite3.connect(backup) as src:
        with sqlite3.connect(target) as dest:
            src.backup(dest)


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".owrt-code-swap-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as dst, Path(source).open("rb") as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(tmp, stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o755)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def stage_module(source):
    file = source.parent.parent / "tools/stage_belt_wss.py"
    need(file.is_file(), "Missing stage_belt_wss.py from reviewed repo")
    spec = importlib.util.spec_from_file_location("code_only_stage", file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def assert_unchanged(args, baseline):
    need(active(XRAY_SERVICE), "Xray service no longer active")
    need(pid(XRAY_SERVICE) == baseline["xray_pid"], "Xray PID changed; stop for review")
    need(digest(args.xray_bin) == baseline["xray_binary_sha"], "Xray binary was changed")
    need(digest(args.xray_config) == baseline["xray_json_sha"], "Live Xray JSON was changed")
    need(digest(Path(baseline["unit_copy"])) == baseline["xray_unit_sha"],
         "Backup of Xray unit altered; stop for review")
    need(execute("systemctl", "cat", XRAY_SERVICE).stdout == baseline["xray_unit_text"],
         "Xray service unit changed")
    need(listen(18443) and listen(18444), "TCP or WSS listener missing")


def inspect(args, folder):
    need(args.source is not None, "Provide --source from pinned Git commit")
    source = args.source.resolve()
    installed = args.installed.resolve()
    need(source.is_file() and installed.is_file() and source != installed,
         "Candidate source and installed Hub must be different existing files")
    need(args.db.is_file() and args.xray_config.is_file() and args.xray_bin.is_file(),
         "Missing production DB, Xray JSON or binary")
    need((args.db.parent / HOLD).is_file(), "Xray maintenance HOLD is absent")
    need(active(HUB_SERVICE) and active(XRAY_SERVICE), "Hub or Xray service not active")
    need(health(), "Hub health endpoint is not responding")
    need(listen(18443) and listen(18444), "Required legacy/WSS listener absent")

    installed_sha = digest(installed)
    candidate_sha = digest(source)
    execute(sys.executable, "-m", "py_compile", str(source))

    code = source.read_text(encoding="utf-8")
    need("def xray_configs_equivalent_for_reload(existing, generated):" in code and
         "if out.exists() and xray_configs_equivalent_for_reload(existing, config):" in code,
         "Candidate must contain safe no-restart Xray equality protection")

    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot = folder / "hub-snapshot.sqlite"
    snapshot_db(args.db, snapshot)
    with sqlite3.connect(snapshot) as conn:
        rows = conn.execute(
            "select vps_transport, vless_port, vps_allow_legacy "
            "from routers where coalesce(deleted_at,0)=0"
        ).fetchall()
        need(len(rows) == 5, "Expected exactly 5 router records")
        need(sum(t == "tcp" and p == 18443 and not fallback for t, p, fallback in rows) == 4,
             "Legacy TCP distribution changed")
        need(sum(t == "ws" and p == 443 and not fallback for t, p, fallback in rows) == 1,
             "WSS distribution changed")

    stage = stage_module(source)
    live = json.loads(args.xray_config.read_text(encoding="utf-8"))
    stage.check_expected(live)
    tcp_in = next(item for item in live["inbounds"] if item["tag"] == "owrt-remote-vless")
    candidate = folder / "candidate.json"
    env = os.environ.copy()
    env["OWRT_REMOTE_STATE_DIR"] = str(folder / "state")
    env["OWRT_REMOTE_XRAY_CONFIG"] = str(folder / "NO_LIVE_CHANGES.json")
    result = subprocess.run([
        sys.executable, str(source), "--db", str(snapshot), "render-xray",
        "--listen-host", tcp_in.get("listen", "0.0.0.0"),
        "--listen-port", str(tcp_in["port"]), "--out", str(candidate),
    ], env=env, capture_output=True, text=True, timeout=40)
    need(result.returncode == 0 and candidate.is_file(), "New source failed to render Xray JSON")
    candidate.chmod(0o600)
    rendered = json.loads(candidate.read_text(encoding="utf-8"))
    need(stage.normalized(live) == stage.normalized(rendered),
         "New source would change current Xray routes/UUIDs; refuse upgrade")
    # Never apply candidate JSON: Xray remains untouched.
    execute(str(args.xray_bin), "run", "-test", "-config", str(candidate), timeout=35)
    xray_unit_text = execute("systemctl", "cat", XRAY_SERVICE).stdout
    unit_copy = folder / "xray-unit.txt"
    unit_copy.write_text(xray_unit_text, encoding="utf-8")
    unit_copy.chmod(0o600)
    baseline = {
        "installed_sha": installed_sha,
        "candidate_sha": candidate_sha,
        "xray_pid": pid(XRAY_SERVICE),
        "xray_json_sha": digest(args.xray_config),
        "xray_binary_sha": digest(args.xray_bin),
        "xray_unit_sha": digest(unit_copy),
        "xray_unit_text": xray_unit_text,
        "unit_copy": str(unit_copy),
    }
    need(health(), "Hub ceased responding during preflight")
    assert_unchanged(args, baseline)
    print("GUARD: 4 TCP + 1 WSS records; unchanged reverse config; Xray syntax OK")
    return baseline


def save_backup(args, folder, baseline):
    data = folder / "files"
    data.mkdir(mode=0o700)
    for path, name in [(args.installed, "hub-before.py"),
                       (args.xray_config, "xray-before.json"),
                       (args.xray_bin, "xray-before.bin")]:
        shutil.copy2(path, data / name)
        (data / name).chmod(0o600)
    snapshot_db(args.db, data / "db-before.sqlite")
    unit = execute("systemctl", "cat", HUB_SERVICE).stdout
    (data / "hub-unit.txt").write_text(unit, encoding="utf-8")
    (data / "xray-unit.txt").write_text(baseline["xray_unit_text"], encoding="utf-8")
    manifest = {
        "installed": str(args.installed.resolve()),
        "db": str(args.db.resolve()),
        "xray_config": str(args.xray_config.resolve()),
        "xray_bin": str(args.xray_bin.resolve()),
        "hub_before_sha": baseline["installed_sha"],
        "source_sha": baseline["candidate_sha"],
        "xray_binary_sha": baseline["xray_binary_sha"],
        "xray_json_sha": baseline["xray_json_sha"],
        "xray_unit_text": baseline["xray_unit_text"],
        "xray_pid": baseline["xray_pid"],
    }
    (data / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    for p in data.iterdir():
        p.chmod(0o600)
    return data


def rollback(args, files, auto=False):
    manifest = json.loads((files / "manifest.json").read_text(encoding="utf-8"))
    need((args.db.parent / HOLD).is_file(), "Cannot rollback without Xray maintenance HOLD")
    need(active(XRAY_SERVICE), "Xray stopped; operator investigation required")
    need(digest(args.xray_bin) == manifest["xray_binary_sha"], "Xray binary drifted")
    need(digest(args.xray_config) == manifest["xray_json_sha"], "Xray JSON drifted")
    need(pid(XRAY_SERVICE) == manifest["xray_pid"], "Xray process changed")
    execute("systemctl", "stop", HUB_SERVICE)
    atomic_copy(files / "hub-before.py", args.installed)
    restore_db(files / "db-before.sqlite", args.db)
    execute("systemctl", "start", HUB_SERVICE)
    need(health() and digest(args.installed) == manifest["hub_before_sha"],
         "Rollback could not restore healthy previous Hub")
    print("ROLLBACK: restored previous Hub code + SQLite; Xray untouched.")


def apply(args):
    stage_root = Path(tempfile.mkdtemp(prefix="owrt-hub-code-stage-", dir=args.backup_root))
    stage_root.chmod(0o700)
    baseline = inspect(args, stage_root)
    need(digest(args.installed) != baseline["candidate_sha"], "Candidate already installed")
    files = save_backup(args, stage_root, baseline)
    print("Rollback snapshot:", files, flush=True)
    touched = False
    try:
        touched = True
        execute("systemctl", "stop", HUB_SERVICE)
        need(not active(HUB_SERVICE), "Hub did not stop")
        assert_unchanged(args, baseline)
        atomic_copy(args.source, args.installed)
        need(digest(args.installed) == baseline["candidate_sha"], "Candidate copy mismatch")
        execute("systemctl", "start", HUB_SERVICE)
        need(health(revision=baseline["candidate_sha"]), "New Hub did not reach expected revision")
        assert_unchanged(args, baseline)
        need((args.db.parent / HOLD).is_file(), "Maintenance HOLD unexpectedly removed")
        print("CODE UPDATE: PASS. Hub updated; Xray PID/JSON/unit/binary unchanged.", flush=True)
        print("Rollback snapshot:", files, flush=True)
    except BaseException:
        if touched:
            print("CODE UPDATE FAILED: attempting restoration", file=sys.stderr, flush=True)
            rollback(args, files, auto=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply", "rollback"))
    parser.add_argument("--source", type=Path)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--backup-root", type=Path, default=Path("/root"))
    parser.add_argument("--installed", type=Path, default=Path("/opt/owrt-remote/owrt-remote-hub.py"))
    parser.add_argument("--db", type=Path, default=Path("/var/lib/owrt-remote/hub.db"))
    parser.add_argument("--xray-config", type=Path, default=Path("/etc/xray/owrt-remote.json"))
    parser.add_argument("--xray-bin", type=Path, default=Path("/usr/local/bin/xray"))
    args = parser.parse_args()
    try:
        need(os.geteuid() == 0, "Must run as root")
        with open("/run/lock/owrt-v119-code-update.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.mode == "check":
                directory = Path(tempfile.mkdtemp(prefix="owrt-hub-code-check-", dir=args.backup_root))
                directory.chmod(0o700)
                inspect(args, directory)
                print("CHECK: PASS. No services restarted or live DB/Xray files changed.")
            elif args.mode == "apply":
                apply(args)
            else:
                need(args.backup is not None and args.backup.is_dir(), "Provide --backup DIR")
                rollback(args, args.backup)
    except (GuardError, OSError, ValueError, KeyError, RuntimeError,
            sqlite3.Error, subprocess.TimeoutExpired, BlockingIOError) as exc:
        print("ABORT:", str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
