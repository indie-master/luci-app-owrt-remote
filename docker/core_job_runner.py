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
    except (ValueError, TypeError, AttributeError):
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
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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
    for script in (SCRIPTS / "xray_releases.py", SCRIPTS / "xray_lifecycle.py"):
        info = script.stat()
        require(info.st_uid == 0 and info.st_mode & 0o022 == 0,
                "unsafe lifecycle script ownership/permissions")

    LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(path.is_file() and not path.is_symlink(), "job record absent")
        meta = path.stat()
        require(meta.st_uid == 0 and meta.st_mode & 0o077 == 0, "unsafe job metadata")
        document = json.loads(path.read_text())
        require(document.get("id") == job_id and document.get("status") == "pending",
                "job is already executed or malformed")
        action = document.get("action")
        version = document.get("version")
        command = command_for(action, version)
        document.update(status="running", started_unix=int(time.time()))
        update(path, document)
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=900, check=False, cwd=str(ROOT), pass_fds=(lock.fileno(),),
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


def unified_directory(stack):
    return stack.path.parent / (".core-jobs-" + stack.name)


def read_unified_job(stack, job_id):
    from unified_xray_state import read_private

    path = unified_directory(stack) / (checked_job_id(job_id) + ".json")
    job = json.loads(read_private(path, 8192))
    require(job["id"] == job_id and job["status"] in {"pending", "running", "success", "failed"},
            "Invalid core job record")
    return path, job


def job_with_receipt(job, record):
    receipt = record.get("core_receipt", {})
    pending = record["pending"]
    if pending and pending.get("kind") == "core" and pending["id"] == job["id"]:
        job = dict(job, status="running")
        job.pop("finished_unix", None)
    elif receipt.get("id") == job["id"]:
        job = dict(job, status=receipt["status"])
    return job


def unified_job_status(stack, job_id):
    from unified_core_manager import load_record

    _, job = read_unified_job(stack, job_id)
    record = load_record(stack)
    image = record["current"]["config"]["RO_XRAY_IMAGE"]
    return {"job": job_with_receipt(job, record), "running_image": None if record["pending"] else image}


def settle_unified_job(stack, job_id, *, record=None):
    from unified_core_manager import load_record

    if record is None:
        record = load_record(stack)
    pending = record["pending"]
    if pending and pending.get("kind") == "core" and pending["id"] == job_id:
        return
    path, persisted = read_unified_job(stack, job_id)
    job = job_with_receipt(persisted, record)
    if persisted["status"] in ("pending", "running") or persisted["status"] != job["status"]:
        if job["status"] in ("pending", "running"):
            job.update(status="failed", error="Core job interrupted")
        update(path, dict(job, finished_unix=int(time.time())))


def settle_unified_jobs(stack):
    from stack_runtime import protected
    from unified_core_manager import load_record

    directory = unified_directory(stack)
    if not directory.exists() and not directory.is_symlink():
        return
    require(protected(directory).is_dir() and not directory.stat().st_mode & 0o077,
            "Unsafe core-job directory")
    record = load_record(stack)
    for path in directory.glob("*.json"):
        settle_unified_job(stack, path.stem, record=record)


def recover_unified(adapter):
    """Reconcile abandoned jobs while no deployment or legacy worker can act."""
    import manager as m
    from unified_core_manager import CoreManager, legacy_exclusion, load_record

    with m.deployment_lock(), legacy_exclusion():
        stack, _, _ = adapter.open_project(allow_core_pending=True)
        core = CoreManager(adapter)
        record = load_record(stack)
        if record["pending"] is not None:
            core.recover(stack, record)
        else:
            core.guard(stack)
        settle_unified_jobs(stack)


def queue_unified(adapter, stack, args):
    import manager as m
    from unified_core_manager import CoreManager, load_record

    fd, = m.inherited_lock()
    action, version = args["action"], args["version"]
    command_for(action, version)
    require(isinstance(version, str) and type(args["confirmed"]) is bool and
            (action == "prepare" or args["confirmed"]), "Invalid core confirmation or version")
    CoreManager(adapter).guard(stack)
    record = load_record(stack)
    require(record["pending"] is None, "Pending stack transaction")
    settle_unified_jobs(stack)
    directory = unified_directory(stack)
    directory.mkdir(mode=0o700, exist_ok=True)
    from stack_runtime import protected
    require(protected(directory).is_dir() and not directory.stat().st_mode & 0o077,
            "Unsafe core-job directory")
    job_id = str(uuid.uuid4())
    job = {"id": job_id, "status": "pending", "action": action,
           "version": version, "created_unix": int(time.time())}
    path = directory / (job_id + ".json")
    update(path, job)
    try:
        adapter.core_worker = subprocess.Popen(
            [sys.executable, __file__, "--unified-worker", str(stack.path), job_id, str(fd), str(m.LOCK)],
            cwd=m.ROOT, pass_fds=(fd,), start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        update(path, dict(job, status="failed", error="Core worker did not start"))
        raise
    return {"job_id": job_id, "status": "pending", "action": action}


def run_unified(env, job_id, fd, lock_path):
    import manager as m
    from unified_core_manager import CoreManager, legacy_exclusion, load_record
    from unified_xray_adapter import Adapter

    m.require_root()
    lock = os.fstat(fd)
    expected = lock_path.lstat()
    require((lock.st_dev, lock.st_ino) == (expected.st_dev, expected.st_ino),
            "Deployment lock descriptor mismatch")
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    m.HUB_DEPLOY_ROOT = lock_path.parent
    token = m.LOCK_FD.set(fd)
    try:
        adapter = Adapter(env)
        stack, _, _ = adapter.open_project(allow_core_pending=True)
        job = unified_job_status(stack, job_id)["job"]
        require(job["status"] == "pending", "Job already attempted")
        path = unified_directory(stack) / (job_id + ".json")
        job.update(status="running", started_unix=int(time.time()))
        update(path, job)
        try:
            with legacy_exclusion():
                core = CoreManager(adapter)
                record = load_record(stack)
                require(record["pending"] is None, "Pending stack transaction")
                core.execute(stack, record, job)
            job.update(status="success")
        except Exception as exc:
            record = load_record(stack)
            pending = record["pending"]
            if pending and pending.get("id") == job_id:
                # The watchdog will finish recovery and write the terminal receipt.
                return 1
            receipt = record.get("core_receipt", {})
            status = receipt["status"] if receipt.get("id") == job_id else "failed"
            job.update(status=status, error=str(exc)[:160] if isinstance(exc, m.Blocked) else
                       "Core operation failed; inspect the stack journal")
        update(path, dict(job, finished_unix=int(time.time())))
        return 0 if job["status"] == "success" else 1
    finally:
        m.LOCK_FD.reset(token)
        os.close(fd)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 6 and sys.argv[1] == "--unified-worker":
            sys.exit(run_unified(Path(sys.argv[2]), sys.argv[3], int(sys.argv[4]), Path(sys.argv[5])))
        sys.exit(run_job(sys.argv[1]))
    except Exception as exc:
        print("CORE JOB ERROR:", str(exc), file=sys.stderr)
        sys.exit(1)
