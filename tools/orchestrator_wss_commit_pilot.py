#!/usr/bin/env python3
"""Supervised ONE-router WSS Xray transaction for Belt (DRAFT, no auto install).

preview: read-only live Xray/SQLite check and Xray -test; print only hashes.
apply: explicit maintenance approval and exact SHA, backup, atomic apply,
       restart ONLY dedicated host Xray; journal as APPLIED_PENDING_ROUTER.
rollback: after failure/interruption restore exact prior Xray bytes; keep
          HUB SQLite record for owner retry; retain maintenance hold.

The existing host-adapter service remains READ ONLY. This separate operator
utility is not a socket write endpoint and MUST NOT run automatically from
the Docker HUB. It is an initial production-pilot gate, not final onboarding.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
CONFIG = Path("/etc/xray/owrt-remote.json")
DB = Path("/var/lib/owrt-remote/hub.db")
HOLD = Path("/var/lib/owrt-remote/.xray-maintenance-hold")
SOURCE = REPO / "vps/owrt-remote-hub.py"
ADAPTER = REPO / "docker/host_adapter.py"
XRAY = Path("/usr/local/bin/xray")
XRAY_SERVICE = "owrt-remote-xray.service"
HUB_SERVICE = "owrt-remote.service"
ADAPTER_SERVICE = "router-orchestrator-host-adapter.service"
CONTROL = Path("/var/lib/router-orchestrator-wss-pilot")
BACKUP = CONTROL / "xray-before.json"
JOURNAL = CONTROL / "transaction.json"
LOCK = Path("/run/lock/router-orchestrator-wss-pilot.lock")
ROUTER_ID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")
MAX_CONFIG = 2 * 1024 * 1024


class Blocked(RuntimeError):
    pass


def require(ok, reason: str):
    if not ok:
        raise Blocked(reason)


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def check_regular(path: Path):
    require(path.is_file() and not path.is_symlink(), "required file missing or unsafe")


def service_active(service: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", service],
                           capture_output=True, timeout=9, check=False)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def ensure_services():
    require(service_active(HUB_SERVICE), "original production HUB is not active")
    require(service_active(XRAY_SERVICE), "host Xray is not active")
    require(service_active(ADAPTER_SERVICE), "read-only host adapter is not active")


def checkout(expected: str):
    require(COMMIT.fullmatch(expected) is not None, "full reviewed source SHA required")
    result = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=8, check=False)
    require(result.returncode == 0 and result.stdout.strip() == expected,
            "Git revision differs from reviewed SHA")
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain",
                            "--untracked-files=all"], capture_output=True,
                           text=True, timeout=8, check=False)
    require(dirty.returncode == 0 and not dirty.stdout.strip(),
            "Git checkout contains unreviewed changes")


def preconditions(expected: str, router_id: str):
    require(os.geteuid() == 0, "must run as root on Belt")
    require(ROUTER_ID.fullmatch(router_id) is not None, "invalid router ID")
    checkout(expected)
    for item in (CONFIG, DB, HOLD, SOURCE, ADAPTER, XRAY):
        check_regular(item)
    require(os.access(XRAY, os.X_OK), "host Xray is not executable")
    ensure_services()
    require(not JOURNAL.exists() and not JOURNAL.is_symlink(),
            "another WSS pilot transaction exists; review/rollback before retry")


@contextlib.contextmanager
def transaction_lock():
    require(os.geteuid() == 0, "root required")
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked("another WSS pilot operation is running") from exc
        yield
    finally:
        os.close(fd)


def _import_module(path: Path, name: str):
    module_spec = importlib.util.spec_from_file_location(name, path)
    require(module_spec is not None and module_spec.loader is not None,
            "reviewed module could not be imported")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    return module


def snapshot_inventory(db: Path, tmp: Path):
    """The production database is opened read-only, not mutated."""
    snapshot = tmp / "hub.db"
    with contextlib.closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro",
                                            uri=True, timeout=10)) as source:
        with contextlib.closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
    snapshot.chmod(0o600)
    return snapshot


def build_candidate(router_id: str, live_bytes: bytes) -> bytes:
    require(0 < len(live_bytes) < MAX_CONFIG, "host Xray config is too large")
    with tempfile.TemporaryDirectory(prefix="ro-wss-pilot-", dir="/root") as td:
        tmp = Path(td)
        db_snapshot = snapshot_inventory(DB, tmp)
        previous_env = {key: os.environ.get(key) for key in (
            "OWRT_REMOTE_STATE_DIR", "OWRT_REMOTE_DB", "OWRT_REMOTE_RUNTIME")}
        try:
            os.environ.update({
                "OWRT_REMOTE_STATE_DIR": str(tmp),
                "OWRT_REMOTE_DB": str(db_snapshot),
                "OWRT_REMOTE_RUNTIME": "docker",
            })
            hub = _import_module(SOURCE, "ro_wss_pilot_" + uuid.uuid4().hex)
            adapter = _import_module(ADAPTER, "ro_wss_guard_" + uuid.uuid4().hex)
            with hub.connect(db_snapshot) as conn:
                hub.init_db(conn)  # touches the disposable copy only
                rows = hub.list_router_rows(conn)
            candidate = hub.build_additive_wss_candidate(
                json.loads(live_bytes), rows, expected_new_id=router_id)
            require(candidate != json.loads(live_bytes),
                    "target already registered or no pending WSS changes")
            adapter.validate_transition(json.loads(live_bytes), candidate)
            return (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode()
        finally:
            for key, prior in previous_env.items():
                if prior is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = prior


def syntax_test(candidate: bytes):
    """Xray own syntax test, no live config replacement or restart."""
    require(len(candidate) < MAX_CONFIG, "candidate exceeds size limit")
    fd, path = tempfile.mkstemp(prefix=".wss-check-", suffix=".json",
                                dir="/run/router-orchestrator")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(candidate)
            stream.flush()
        result = subprocess.run([str(XRAY), "run", "-test", "-config", path],
                                capture_output=True, timeout=30, check=False)
        require(result.returncode == 0, "host Xray rejected WSS candidate")
    finally:
        Path(path).unlink(missing_ok=True)


def ready_xray(attempts: int = 15) -> bool:
    for _ in range(attempts):
        if service_active(XRAY_SERVICE):
            try:
                for port in (18443, 18444):
                    with socket.create_connection(("127.0.0.1", port), timeout=1):
                        pass
                return True
            except OSError:
                pass
        time.sleep(1)
    return False


def preview(router_id: str, expected: str) -> dict:
    preconditions(expected, router_id)
    original = CONFIG.read_bytes()
    candidate = build_candidate(router_id, original)
    syntax_test(candidate)
    require(sha(CONFIG.read_bytes()) == sha(original),
            "host Xray changed while building preview")
    return {"router_id": router_id, "current_sha": sha(original),
            "candidate_sha": sha(candidate), "candidate": candidate,
            "original": original}


def secure_transaction_dir():
    require(not CONTROL.is_symlink(), "unsafe transaction directory")
    if not CONTROL.exists():
        CONTROL.mkdir(mode=0o700)
    st = CONTROL.stat()
    require(CONTROL.is_dir() and st.st_uid == 0 and
            stat.S_IMODE(st.st_mode) == 0o700,
            "transaction directory must be root-owned mode 0700")


def atomic_write(path: Path, data: bytes, *, preserve_from: Path | None = None):
    fd, temp = tempfile.mkstemp(prefix=".pilot-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            if preserve_from is not None:
                source = preserve_from.stat()
                os.fchown(stream.fileno(), source.st_uid, source.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(source.st_mode))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        Path(temp).unlink(missing_ok=True)


def save_journal(doc: dict):
    atomic_write(JOURNAL, (json.dumps(doc, sort_keys=True) + "\n").encode())


def read_journal():
    check_regular(JOURNAL)
    require(JOURNAL.stat().st_uid == 0, "journal not owned by root")
    doc = json.loads(JOURNAL.read_bytes())
    require(doc.get("schema") == 1 and doc.get("state") in
            {"PREPARED", "APPLYING", "APPLIED_PENDING_ROUTER",
             "RECOVERING", "ROLLED_BACK"},
            "unrecognized transaction state")
    return doc


def restart_xray():
    result = subprocess.run(["systemctl", "restart", XRAY_SERVICE],
                            capture_output=True, timeout=45, check=False)
    require(result.returncode == 0 and ready_xray(),
            "Xray restart failed or TCP/WSS listeners are unavailable")


def rollback_locked(doc: dict):
    check_regular(BACKUP)
    original = BACKUP.read_bytes()
    require(sha(original) == doc["original_sha"],
            "backup checksum mismatch; manual intervention required")
    current = CONFIG.read_bytes()
    require(sha(current) in {doc["original_sha"], doc["candidate_sha"]},
            "host config changed outside transaction; refuse overwrite")
    doc["state"] = "RECOVERING"
    save_journal(doc)
    if sha(current) == doc["candidate_sha"]:
        atomic_write(CONFIG, original, preserve_from=CONFIG)
        restart_xray()
    else:
        require(ready_xray(attempts=2),
                "original config is present but Xray listeners are unhealthy")
    require(sha(CONFIG.read_bytes()) == doc["original_sha"],
            "original config not restored")
    doc["state"] = "ROLLED_BACK"
    save_journal(doc)


def apply(router_id: str, expected: str, approved_candidate_sha: str):
    require(SHA.fullmatch(approved_candidate_sha) is not None,
            "explicit candidate SHA256 is required")
    with transaction_lock():
        stage = preview(router_id, expected)
        require(stage["candidate_sha"] == approved_candidate_sha,
                "candidate differs from reviewed preview SHA256")
        require(sha(CONFIG.read_bytes()) == stage["current_sha"],
                "Xray config changed before commit")
        secure_transaction_dir()
        require(not BACKUP.exists() and not JOURNAL.exists(),
                "pilot artifacts already exist; refuse overwrite")
        fd = os.open(BACKUP, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY,
                     0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(stage["original"])
            output.flush()
            os.fsync(output.fileno())
        doc = {"schema": 1, "state": "PREPARED", "router_id": router_id,
               "git_commit": expected, "original_sha": stage["current_sha"],
               "candidate_sha": stage["candidate_sha"],
               "created_utc": dt.datetime.now(dt.timezone.utc).isoformat()}
        save_journal(doc)
        try:
            require(sha(CONFIG.read_bytes()) == doc["original_sha"],
                    "Xray config changed during transaction staging")
            doc["state"] = "APPLYING"
            save_journal(doc)
            atomic_write(CONFIG, stage["candidate"], preserve_from=CONFIG)
            restart_xray()
            require(sha(CONFIG.read_bytes()) == doc["candidate_sha"],
                    "host Xray config changed after restart")
            require(service_active(HUB_SERVICE) and service_active(ADAPTER_SERVICE),
                    "production HUB/adapter not active after Xray restart")
            doc["state"] = "APPLIED_PENDING_ROUTER"
            save_journal(doc)
        except BaseException:
            try:
                rollback_locked(doc)
            except BaseException as exc:
                print("CRITICAL: operator recovery required; private backup and "
                      "journal retained (" + type(exc).__name__ + ")",
                      file=sys.stderr)
            raise
        return doc


def rollback(confirm: bool):
    require(confirm, "rollback requires --confirm")
    require(os.geteuid() == 0, "rollback requires root")
    with transaction_lock():
        secure_transaction_dir()
        doc = read_journal()
        require(doc["state"] in {"PREPARED", "APPLYING",
                                 "APPLIED_PENDING_ROUTER", "RECOVERING"},
                "no active pilot transaction to roll back")
        rollback_locked(doc)
        return doc


def main():
    p = argparse.ArgumentParser(description=__doc__)
    modes = p.add_subparsers(dest="mode", required=True)
    for verb in ("preview", "apply"):
        cmd = modes.add_parser(verb)
        cmd.add_argument("--router-id", required=True)
        cmd.add_argument("--expect-commit", required=True)
        if verb == "apply":
            cmd.add_argument("--expect-candidate-sha256", required=True)
            cmd.add_argument("--approve-maintenance", action="store_true")
    recover = modes.add_parser("rollback")
    recover.add_argument("--confirm", action="store_true")
    args = p.parse_args()
    try:
        if args.mode == "preview":
            result = preview(args.router_id, args.expect_commit)
            print("WSS COMMIT PILOT PREVIEW: PASS")
            print("ROUTER:", result["router_id"])
            print("CURRENT XRAY SHA256:", result["current_sha"])
            print("CANDIDATE SHA256:", result["candidate_sha"])
            print("XRAY: candidate syntax valid; NO config changes or restart")
        elif args.mode == "apply":
            require(args.approve_maintenance,
                    "explicit --approve-maintenance is required")
            doc = apply(args.router_id, args.expect_commit,
                        args.expect_candidate_sha256)
            print("WSS PILOT: APPLIED_PENDING_ROUTER")
            print("ROUTER:", doc["router_id"])
            print("XRAY: dedicated service restarted; existing listeners up")
            print("IMPORTANT: install OpenWrt agent and verify heartbeat/LuCI/SSH")
            print("ROLLBACK: sudo python3 tools/orchestrator_wss_commit_pilot.py "
                  "rollback --confirm")
        else:
            doc = rollback(args.confirm)
            print("WSS PILOT: ROLLED_BACK")
            print("ROUTER:", doc["router_id"])
            print("HOST: original Xray restored; HUB DB row retained")
        return 0
    except (Blocked, OSError, ValueError, sqlite3.Error, KeyError,
            subprocess.TimeoutExpired) as exc:
        print("WSS PILOT BLOCKED — " +
              (str(exc)[:160] if isinstance(exc, Blocked)
               else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
