#!/usr/bin/env python3
"""Independent offline-rescuable Belt Xray rollback, with no HUB/Git imports.

Install a root-owned copy BEFORE any operator-approved WSS Xray commit.
This utility does not register routers, manage Docker or need the HUB.
It handles only the private journal emitted by orchestrator_wss_commit_pilot.py.

status: read-only; never obtains a write lock or invokes systemctl restart.
restore: requires two explicit acknowledgements (confirm + candidate SHA).
         verifies transaction metadata and exact prior file hash, restores
         only /etc/xray/owrt-remote.json, restarts only owrt-remote-xray.service.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time

CONFIG = Path("/etc/xray/owrt-remote.json")
HOLD = Path("/var/lib/owrt-remote/.xray-maintenance-hold")
ROOT = Path("/var/lib/router-orchestrator-wss-pilot")
BACKUP = ROOT / "xray-before.json"
JOURNAL = ROOT / "transaction.json"
LOCK = Path("/run/lock/router-orchestrator-wss-pilot.lock")
SERVICE = "owrt-remote-xray.service"
ALLOWED_STATES = frozenset({
    "PREPARED", "APPLYING", "APPLIED_PENDING_ROUTER",
    "RECOVERING", "ROLLED_BACK",
})
SHA = re.compile(r"[0-9a-f]{64}\Z")
MAX_JOURNAL = 16384
MAX_XRAY = 2 * 1024 * 1024


class Blocked(RuntimeError):
    pass


def require(ok: bool, message: str):
    if not ok:
        raise Blocked(message)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def secure_file(path: Path, *, private: bool = False):
    try:
        entry = path.lstat()
    except FileNotFoundError as exc:
        raise Blocked("required rescue file missing") from exc
    require(stat.S_ISREG(entry.st_mode) and entry.st_uid == 0,
            "rescue file is not a root-owned regular file")
    if private:
        require(stat.S_IMODE(entry.st_mode) & 0o077 == 0,
                "private rescue file has unsafe permissions")
    return entry


def secure_parent(path: Path, *, private: bool, allow_sticky: bool = False):
    try:
        entry = path.lstat()
    except FileNotFoundError as exc:
        raise Blocked("rescue directory missing") from exc
    require(stat.S_ISDIR(entry.st_mode) and entry.st_uid == 0,
            "rescue directory is not root-owned or is symlink")
    mode = stat.S_IMODE(entry.st_mode)
    if private:
        require((mode & 0o077) == 0,
                "private rescue directory has unsafe permissions")
    else:
        require((mode & 0o022) == 0 or
                (allow_sticky and (mode & stat.S_ISVTX) and
                 not (mode & 0o020 and not (mode & stat.S_ISVTX))),
                "rescue directory is writable by an untrusted user")


def inspected_journal() -> dict:
    secure_parent(ROOT, private=True)
    entry = secure_file(JOURNAL, private=True)
    require(0 < entry.st_size <= MAX_JOURNAL, "invalid rescue journal length")
    document = json.loads(JOURNAL.read_bytes())
    require(isinstance(document, dict) and document.get("schema") == 1,
            "unsupported rescue journal schema")
    require(document.get("state") in ALLOWED_STATES,
            "invalid transaction state")
    require(isinstance(document.get("original_sha"), str) and
            SHA.fullmatch(document["original_sha"]) is not None,
            "invalid original SHA in journal")
    require(isinstance(document.get("candidate_sha"), str) and
            SHA.fullmatch(document["candidate_sha"]) is not None and
            document["original_sha"] != document["candidate_sha"],
            "invalid candidate SHA in journal")
    require(isinstance(document.get("router_id"), str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", document["router_id"]),
            "journal router identity malformed")
    return document


def checked_config() -> bytes:
    secure_parent(CONFIG.parent, private=False)
    entry = secure_file(CONFIG)
    require(0 < entry.st_size <= MAX_XRAY,
            "host Xray config is missing or unreasonably large")
    return CONFIG.read_bytes()


def checked_backup(doc: dict) -> bytes:
    entry = secure_file(BACKUP, private=True)
    require(0 < entry.st_size <= MAX_XRAY,
            "rescue backup has invalid size")
    original = BACKUP.read_bytes()
    require(digest(original) == doc["original_sha"],
            "private Xray backup SHA mismatch")
    return original


def atomic_bytes(path: Path, payload: bytes):
    """Durable atomic replace, retaining original file UID/GID and mode."""
    old = secure_file(path, private=(path == JOURNAL))
    fd, temp = tempfile.mkstemp(prefix=".xray-rescue-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), old.st_uid, old.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(old.st_mode))
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        Path(temp).unlink(missing_ok=True)


def record(doc: dict, state: str):
    doc["state"] = state
    atomic_bytes(JOURNAL, (json.dumps(doc, sort_keys=True) + "\n").encode())


def listener_ready(*, attempts=12) -> bool:
    for index in range(attempts):
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", SERVICE],
            capture_output=True, check=False, timeout=7)
        if result.returncode == 0:
            for port in (18443, 18444):
                try:
                    with socket.create_connection(("127.0.0.1", port),
                                                  timeout=1):
                        pass
                except OSError:
                    break
            else:
                return True
        if index < attempts - 1:
            time.sleep(1)
    return False


def restart_xray():
    outcome = subprocess.run(["systemctl", "restart", SERVICE],
                             capture_output=True, check=False, timeout=50)
    require(outcome.returncode == 0 and listener_ready(),
            "Xray failed to restart with original config; emergency console required")


@contextlib.contextmanager
def exclusive():
    secure_parent(LOCK.parent, private=False, allow_sticky=True)
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        entry = os.fstat(fd)
        require(stat.S_ISREG(entry.st_mode) and entry.st_uid == 0,
                "existing deployment lock is not root-owned")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked("another WSS Xray transaction holds the lock") from exc
        yield
    finally:
        os.close(fd)


def status() -> dict:
    require(os.geteuid() == 0, "run as root on Belt")
    require(HOLD.is_file() and not HOLD.is_symlink(),
            "Xray maintenance hold must remain present")
    if not JOURNAL.exists() and not JOURNAL.is_symlink():
        return {"transaction": "NONE", "config_sha": digest(checked_config())}
    doc = inspected_journal()
    return {"transaction": doc["state"],
            "config_sha": digest(checked_config()),
            "backup_verified": digest(checked_backup(doc)) == doc["original_sha"]}


def restore(*, confirm: bool, expected_candidate: str):
    require(confirm, "--confirm required for recovery")
    require(os.geteuid() == 0, "run as root on Belt")
    require(isinstance(expected_candidate, str) and
            SHA.fullmatch(expected_candidate) is not None,
            "full approved candidate SHA256 required")
    with exclusive():
        require(HOLD.is_file() and not HOLD.is_symlink(),
                "Xray maintenance hold missing; stop for manual review")
        doc = inspected_journal()
        require(doc["candidate_sha"] == expected_candidate,
                "operator candidate SHA does not match journal")
        original = checked_backup(doc)
        current = checked_config()
        current_sha = digest(current)
        require(current_sha in (doc["original_sha"], doc["candidate_sha"]),
                "unexpected Xray config change; refuse to overwrite external edits")
        # Re-entry after an interrupted restore is valid. The exact old bytes
        # are already installed if config SHA equals original_sha.
        if doc["state"] == "ROLLED_BACK":
            require(current_sha == doc["original_sha"],
                    "journal claims rollback but host config differs")
            require(listener_ready(attempts=2),
                    "rolled-back Xray listeners unhealthy; operator review needed")
            return {"state": "ROLLED_BACK", "restored": False}
        record(doc, "RECOVERING")
        if current_sha != doc["original_sha"]:
            require(digest(checked_config()) == current_sha,
                    "Xray config changed between verification and replacement")
            atomic_bytes(CONFIG, original)
        require(digest(checked_config()) == doc["original_sha"],
                "original Xray bytes were not restored")
        # If the old config is already in place and Xray still runs the pilot
        # config, a restart is mandatory. Never infer the running config
        # from the on-disk checksum alone.
        restart_xray()
        require(digest(checked_config()) == doc["original_sha"],
                "host Xray config changed during recovery")
        record(doc, "ROLLED_BACK")
        return {"state": "ROLLED_BACK", "restored": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="command", required=True)
    actions.add_parser("status")
    fix = actions.add_parser("restore")
    fix.add_argument("--confirm", action="store_true")
    fix.add_argument("--expect-candidate-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "status":
            report = status()
            print("WSS RESCUE STATUS: PASS")
            print("TRANSACTION:", report["transaction"])
            print("XRAY SHA256:", report["config_sha"])
            print("NO CHANGE: status is read-only")
        else:
            report = restore(confirm=args.confirm,
                             expected_candidate=args.expect_candidate_sha256)
            print("WSS RESCUE:", report["state"])
            print("XRAY: original configuration verified")
            print("NOTICE: listener health is not a substitute for router LuCI/SSH")
        return 0
    except (Blocked, OSError, ValueError, json.JSONDecodeError,
            subprocess.TimeoutExpired) as exc:
        print("WSS RESCUE BLOCKED — " +
              (str(exc)[:170] if isinstance(exc, Blocked)
               else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
