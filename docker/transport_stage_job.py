#!/usr/bin/env python3
"""Fixed root-owned shadow transport stage job; no arbitrary shell or paths."""
from __future__ import annotations

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
QUEUE = ROOT / "data" / "transport-stage-queue"
RUNNER = ROOT / "src" / "docker" / "transport_stage.py"
JOB_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def require(value, message):
    if not value:
        raise RuntimeError(message)


def atomic(path, record):
    fd, tmp = tempfile.mkstemp(prefix=".stage-job-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def run(job_id):
    require(os.geteuid() == 0, "root required")
    require(JOB_ID.fullmatch(job_id) is not None, "invalid job identifier")
    require(QUEUE.is_dir() and not QUEUE.is_symlink(), "queue directory unavailable")
    path = QUEUE / (job_id + ".json")
    require(path.is_file() and not path.is_symlink(), "job not found")
    st = path.stat()
    require(st.st_uid == 0 and st.st_mode & 0o077 == 0, "job permissions unsafe")
    entry = json.loads(path.read_text())
    require(entry.get("id") == job_id and entry.get("status") == "pending",
            "job not pending")
    require(entry.get("action") == "stage", "unsupported action")
    profile = entry.get("profile")
    require(isinstance(profile, dict), "invalid profile")
    for name in ("transport_stage.py", "transport_canary.py", "transport_profiles.py"):
        info = (RUNNER.parent / name).stat()
        require(info.st_uid == 0 and info.st_mode & 0o022 == 0,
                "unsafe source ownership for staged transport")
    profile_file = QUEUE / (job_id + ".profile.json")
    require(not profile_file.exists(), "profile file exists")
    fd = os.open(profile_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(profile, stream, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    entry["status"] = "running"
    entry["started_unix"] = int(time.time())
    atomic(path, entry)
    args = [sys.executable, str(RUNNER), "stage",
            "--router-id", entry["router_id"],
            "--profile", str(profile_file),
            "--entry-port", str(entry["entry_port"]),
            "--ssh-port", str(entry["ssh_port"])]
    try:
        outcome = subprocess.run(
            args, capture_output=True, text=True, timeout=540, check=False,
            cwd=str(ROOT), env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                               "HOME": "/root", "LANG": "C.UTF-8"}
        )
        entry["status"] = "success" if outcome.returncode == 0 else "failed"
        entry["exit_code"] = outcome.returncode
        # Do not publish command stdout/stderr: may echo usernames, keys or config.
        entry["message"] = "shadow listener staged" if outcome.returncode == 0 else "stage failed, inspect root journal"
    except subprocess.TimeoutExpired:
        entry["status"] = "failed"
        entry["message"] = "stage timed out; root recovery journal requires inspection"
    except Exception:
        entry["status"] = "failed"
        entry["message"] = "staging worker failed; inspect root journal"
    finally:
        profile_file.unlink(missing_ok=True)
        entry.pop("profile", None)
        entry["finished_unix"] = int(time.time())
        atomic(path, entry)
    return 0 if entry["status"] == "success" else 1


if __name__ == "__main__":
    try:
        sys.exit(run(sys.argv[1]))
    except Exception as exc:
        print("TRANSPORT STAGE JOB ERROR:", type(exc).__name__, file=sys.stderr)
        sys.exit(1)
