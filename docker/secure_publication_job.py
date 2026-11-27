"""Fixed, root-private publication job queue; no arbitrary commands or paths."""
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from secure_profile_store import STORE, connection, load_artifact
from secure_publication_transaction import atomic, read_regular
from transport_profiles import ProfileError
from secure_profile_catalog import _identifier

JOB = STORE / "publication-job.json"
LOCK = STORE / "publication-job.lock"
ENABLE = STORE / "web-publication.enabled"
UNIT = "router-orchestrator-publication-job.service"
WORKER = Path("/opt/router-orchestrator/src/docker/secure_publication_worker.py")


def require(condition, message):
    if not condition:
        raise ProfileError(message)


def read_job():
    if not JOB.exists():
        return None
    data, mode, uid, _ = read_regular(JOB)
    require(uid == os.geteuid() and not mode & 0o077, "unsafe publication job")
    return json.loads(data)


def status():
    job = read_job()
    if job is None:
        return {"status": "none"}
    return {k: job[k] for k in ("id", "profile_id", "revision", "status", "created", "message") if k in job}


def enabled():
    try:
        data, mode, uid, _ = read_regular(ENABLE)
        return data == b"enabled\n" and uid == 0 and not mode & 0o077
    except OSError:
        return False


def lock_fd():
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid()
                and st.st_nlink == 1 and not st.st_mode & 0o077, "unsafe publication queue lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def enqueue(profile_id, revision, digest, confirmed):
    require(enabled(), "Web publication disabled by host operator")
    require(confirmed is True, "explicit maintenance confirmation required")
    _identifier(profile_id)
    require(type(revision) is int and revision > 0 and isinstance(digest,str)
            and re.fullmatch(r"[a-f0-9]{64}", digest), "invalid revision or digest")
    fd = lock_fd()
    try:
        existing = read_job()
        require(not existing or existing["status"] not in ("pending","running","recovery_required"),
                "publication job already pending or requires recovery")
        with connection() as db:
            _, current, published = load_artifact(db,profile_id,revision)
        require(not published and current == digest, "draft changed or already published")
        job = {"id":str(uuid.uuid4()),"profile_id":profile_id,"revision":revision,
               "digest":digest,"status":"pending","created":int(time.time())}
        atomic(JOB,json.dumps(job).encode())
        try:
            result = subprocess.run(["systemctl","start","--no-block",UNIT],
                                    capture_output=True,timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            job.update(status="recovery_required",message="systemd job start could not be verified")
            atomic(JOB,json.dumps(job).encode())
            raise ProfileError("systemd job start could not be verified") from None
        if result.returncode:
            job.update(status="failed",message="systemd job start failed")
            atomic(JOB,json.dumps(job).encode())
            raise ProfileError("systemd publication job start failed")
        return status()
    finally:
        os.close(fd)


def execute():
    require(os.geteuid()==0 and enabled(),"publication disabled")
    # Blocking lock acquisition is safe here: enqueue releases immediately
    # after systemctl --no-block. Retry briefly if service starts first.
    for attempt in range(30):
        try:
            fd=lock_fd()
            break
        except BlockingIOError:
            time.sleep(.2)
    else:
        raise ProfileError("publication queue locked")
    try:
        job=read_job()
        require(job and job["status"]=="pending","no pending job")
        job.update(status="running")
        atomic(JOB,json.dumps(job).encode())
        try:
            result=subprocess.run([sys.executable,str(WORKER),"publish",
                "--profile-id",job["profile_id"],"--revision",str(job["revision"]),
                "--artifact-sha256",job["digest"],"--confirm-maintenance"],
                capture_output=True,timeout=1800)
            with connection() as db:
                _,digest,published=load_artifact(db,job["profile_id"],job["revision"])
            if result.returncode==0 and published and digest==job["digest"]:
                job.update(status="success",message="profile published; routers unchanged")
            else:
                job.update(status="recovery_required",message="publication failed; inspect recovery state on host")
        except Exception:
            job.update(status="recovery_required",message="worker interrupted; inspect recovery state on host")
        atomic(JOB,json.dumps(job).encode())
    finally:
        os.close(fd)


def reconcile():
    """Explicit operator action after systemd/recovery has finished."""
    from secure_publication_worker import locks, tx, STATE
    import xray_lifecycle as life
    require(os.geteuid()==0, "root required")
    fd=lock_fd()
    try:
        unit=subprocess.run(["systemctl","show","-p","ActiveState","--value",UNIT],
                            capture_output=True,text=True,timeout=10)
        require(unit.returncode==0 and unit.stdout.strip() in ("inactive","failed"),
                "publication service may still be running")
        with locks():
            require(not life.TRANSPORT_HOLD.exists() and not life.TRANSPORT_HOLD.is_symlink(),
                    "maintenance hold still present")
            job=read_job()
            require(job and job["status"] in ("pending","running","recovery_required"),
                    "no interrupted job")
            record=tx()._record() if (STATE/"pending.json").exists() else None
            require(record is None or record["phase"] in ("ACCEPTED","ROLLED_BACK"),
                    "publication recovery has not finished")
            with connection() as db:
                _,digest,published=load_artifact(db,job["profile_id"],job["revision"])
            require(digest==job["digest"], "revision digest changed")
            if published:
                binding=(record or {}).get("binding",{})
                require(record and record["phase"]=="ACCEPTED" and
                        (binding.get("profile_id"),binding.get("revision"),binding.get("artifact_sha256"))==
                        (job["profile_id"],job["revision"],job["digest"]),
                        "published job journal does not match")
                job.update(status="success",message="publication recovered and verified")
            else:
                job.update(status="failed",message="publication not committed; recovery checked")
            atomic(JOB,json.dumps(job).encode())
            return status()
    finally:
        os.close(fd)


if __name__=="__main__":
    if sys.argv[1:]==["reconcile"]:
        print(json.dumps(reconcile()))
    elif not sys.argv[1:]:
        execute()
    else:
        raise SystemExit("unsupported action")
