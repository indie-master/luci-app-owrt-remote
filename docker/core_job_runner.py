#!/usr/bin/env python3
"""Root-only Xray maintenance job invoked by a fixed systemd template unit.

The HUB never receives docker.sock and cannot provide arbitrary commands,
image names, paths or Docker flags. Operations are whitelisted.
"""
from __future__ import annotations
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path("/opt/router-orchestrator")
JOBS = ROOT / "data" / "core-jobs"
LOCK = ROOT / "data" / "core-jobs.lock"
SCRIPTS = ROOT / "src" / "docker"
VERSION_RE = re.compile(r"^v[0-9]{1,4}\.[0-9]{1,3}\.[0-9]{1,3}$")
ACTIONS = ("prepare", "upgrade", "restart", "rollback")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def checked_job_id(value):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError):
        raise ValueError("invalid job ID") from None
    require(str(parsed) == value, "non-canonical job ID")
    return value


def update(path, document):
    fd, tmp = tempfile.mkstemp(prefix=".job-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def command_for(action, version):
    require(action in ACTIONS, "unsupported action")
    if action in ("prepare", "upgrade"):
        require(isinstance(version, str) and VERSION_RE.fullmatch(version),
                "invalid core version")
    else:
        require(not version, "version is not allowed for this action")
    if action == "prepare":
        return [sys.executable, str(SCRIPTS / "xray_releases.py"),
                "prepare", "--version", version]
    if action == "upgrade":
        return [sys.executable, str(SCRIPTS / "xray_lifecycle.py"),
                "upgrade", "--image", "router-orchestrator-xray:" + version[1:]]
    return [sys.executable, str(SCRIPTS / "xray_lifecycle.py"), action]


def run_job(job_id):
    require(os.geteuid() == 0, "root required")
    checked_job_id(job_id)
    path = JOBS / (job_id + ".json")
    require(path.is_file() and not path.is_symlink(), "job record absent")
    meta = path.stat()
    require(meta.st_uid == 0 and meta.st_mode & 0o077 == 0, "unsafe job metadata")
    document = json.loads(path.read_text())
    require(document.get("id") == job_id and document.get("status") == "pending",
            "job is already executed or malformed")
    action = document.get("action")
    version = document.get("version")
    command = command_for(action, version)
    for script in (SCRIPTS / "xray_releases.py", SCRIPTS / "xray_lifecycle.py"):
        info = script.stat()
        require(info.st_uid == 0 and info.st_mode & 0o022 == 0,
                "unsafe lifecycle script ownership/permissions")

    LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            document.update(status="failed", error="another core job is running",
                            finished_unix=int(time.time()))
            update(path, document)
            return 1
        document.update(status="running", started_unix=int(time.time()))
        update(path, document)
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=900, check=False, cwd=str(ROOT),
                env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                     "HOME": "/root", "LANG": "C.UTF-8"},
            )
            document.update(
                status="success" if result.returncode == 0 else "failed",
                exit_code=result.returncode,
                output=(result.stdout or "")[-1200:],
                error=(result.stderr or "")[-1200:],
            )
        except subprocess.TimeoutExpired:
            document.update(status="failed", error="operation timed out")
        except Exception as exc:
            document.update(status="failed", error=str(exc)[:160])
        document["finished_unix"] = int(time.time())
        update(path, document)
        return 0 if document["status"] == "success" else 1


if __name__ == "__main__":
    try:
        sys.exit(run_job(sys.argv[1]))
    except Exception as exc:
        print("CORE JOB ERROR:", str(exc), file=sys.stderr)
        sys.exit(1)
