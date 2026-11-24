#!/usr/bin/env python3
"""Guarded, short-lived Docker Xray shadow transport transaction.

Stages exactly ONE new reverse pilot for a single existing OpenWrt router.
Never changes the router UCI, UUIDs, primary reverse listeners, HUB DB,
or the Docker Xray image. Pilot automatically rolls back after 10 minutes
unless independently promoted by a future, separate two-phase operation.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

import xray_lifecycle as lifecycle
from transport_canary import build_shadow_canary, assert_baseline_retained

ROOT = Path("/opt/router-orchestrator")
CONFIG = Path("/etc/xray/owrt-remote.json")
JOURNAL_DIR = ROOT / "data" / "transport-stage"
STATE = JOURNAL_DIR / "stage.json"
ORIGINAL = JOURNAL_DIR / "previous-xray.json"
HOLD = ROOT / "data" / ".xray-maintenance-hold"
LIFETIME = 600


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def docker(*args, timeout=80):
    return subprocess.run(("docker", *args), capture_output=True,
                          text=True, timeout=timeout)


def write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ro-stage-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def journal(data):
    write_atomic(STATE, (json.dumps(data, sort_keys=True) + "\n").encode())


def current_image():
    detail = docker("inspect", "--format", "{{.State.Running}}|{{.Config.Image}}|{{.HostConfig.NetworkMode}}",
                    "router-orchestrator-xray")
    require(detail.returncode == 0, "Xray Docker container unavailable")
    running, name, network = detail.stdout.strip().split("|")
    require(running == "true" and network == "host" and
            name.startswith("router-orchestrator-xray:"),
            "unexpected Docker Xray identity")
    return name


def xray_test(candidate, image):
    fd, path = tempfile.mkstemp(prefix=".ro-stage-test-", suffix=".json",
                                dir=CONFIG.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(candidate)
        args = ["run", "--rm", "--network", "none", "--pull", "never",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "-v", "/etc/xray:/etc/xray:ro",
                "-v", "/usr/local/share/xray:/usr/local/share/xray:ro"]
        tls = ROOT / "tls"
        if tls.is_dir():
            args += ["-v", str(tls) + ":/etc/router-orchestrator/tls:ro"]
        args += [image, "run", "-test", "-config", path]
        tested = docker(*args, timeout=45)
        require(tested.returncode == 0, "current Xray core rejected shadow config")
    finally:
        Path(path).unlink(missing_ok=True)


def live_router(router_id):
    require(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", router_id) is not None,
            "invalid router ID")
    path = ROOT / "data" / "hub.db"
    require(path.is_file(), "HUB database missing")
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "select id,vless_uuid,ssh_vless_uuid,admin_host,admin_port,"
            "ssh_host,ssh_port from routers where id=? and deleted_at=0",
            (router_id,)
        ).fetchone()
    require(row is not None, "router not found")
    return dict(row)


def restart():
    result = docker("restart", "--time", "5", "router-orchestrator-xray")
    require(result.returncode == 0, "Docker Xray restart failed")
    require(lifecycle.wait_healthy(30), "Docker Xray did not recover")


def rollback_impl(record):
    require(ORIGINAL.is_file() and not ORIGINAL.is_symlink(),
            "baseline backup unavailable")
    original = ORIGINAL.read_bytes()
    require(sha(original) == record["baseline_sha256"],
            "baseline backup integrity mismatch")
    actual = CONFIG.read_bytes()
    if sha(actual) == record["baseline_sha256"]:
        # Crash before the candidate atomic replace: baseline already in place.
        require(lifecycle.active(), "baseline Xray is not running")
        journal({**record, "phase": "ROLLED_BACK", "finished_unix": int(time.time())})
        HOLD.unlink(missing_ok=True)
        print("SHADOW TRANSPORT ROLLBACK: BASELINE PRESERVED")
        return
    require(sha(actual) == record["candidate_sha256"],
            "Xray config changed externally; refusing overwrite")
    write_atomic(CONFIG, original)
    try:
        restart()
    except Exception:
        # Leave the maintenance hold in place until an operator restores Xray.
        journal({**record, "phase": "ROLLBACK_FAILED", "failed_unix": int(time.time())})
        raise
    journal({**record, "phase": "ROLLED_BACK", "finished_unix": int(time.time())})
    HOLD.unlink(missing_ok=True)
    print("SHADOW TRANSPORT ROLLBACK: PASS")


def stage(router_id, profile_path, entry_port, ssh_port):
    require(not HOLD.exists(), "another Xray maintenance hold is present")
    if STATE.exists():
        previous_stage = json.loads(STATE.read_text())
        require(previous_stage.get("phase") == "ROLLED_BACK",
                "previous shadow transaction is still active")
        archived = JOURNAL_DIR.with_name(
            "transport-stage.previous-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        )
        require(not archived.exists(), "shadow archive path exists")
        JOURNAL_DIR.rename(archived)
    require(lifecycle.active(), "production Docker Xray unhealthy")
    path = Path(profile_path)
    require(path.is_file() and not path.is_symlink(),
            "profile JSON file absent or symlinked")
    require(path.stat().st_uid == 0 and path.stat().st_mode & 0o077 == 0,
            "transport profile must be root-owned mode 0600")
    profile = json.loads(path.read_text())
    router = live_router(router_id)
    previous = CONFIG.read_bytes()
    live = json.loads(previous)
    candidate_config, pilot = build_shadow_canary(
        live, profile, router,
        admin_test_port=entry_port, ssh_test_port=ssh_port,
        listen="0.0.0.0"
    )
    assert_baseline_retained(live, candidate_config)
    candidate = (json.dumps(candidate_config, ensure_ascii=False, indent=2) + "\n").encode()
    image = current_image()
    xray_test(candidate, image)
    database, baseline = lifecycle.online_router_baseline(lifecycle.ENV.read_bytes())
    require(baseline, "no online routers for rollback safety checks")
    JOURNAL_DIR.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(JOURNAL_DIR, 0o700)
    write_atomic(ORIGINAL, previous)
    record = {
        "phase": "STAGING",
        "router_id": router_id, "pilot": pilot,
        "image": image, "baseline_sha256": sha(previous),
        "candidate_sha256": sha(candidate),
        "started_unix": int(time.time()),
        "expires_unix": int(time.time()) + LIFETIME
    }
    journal(record)
    write_atomic(HOLD, b"shadow transport transaction\n")
    started = int(time.time())
    try:
        write_atomic(CONFIG, candidate)
        restart()
        require(lifecycle.wait_canary(database, baseline, started),
                "old reverse tunnels did not return after staged Xray restart")
        record["phase"] = "STAGED"
        journal(record)
        print("SHADOW TRANSPORT: STAGED")
        print("CANARY:", router_id)
        print("TEST PORTS:", entry_port, ssh_port)
        print("DEADLINE UNIX:", record["expires_unix"])
        print("PRIMARY ROUTER UCI: UNCHANGED")
        print("WARNING: candidate ingress must be reachable through firewall")
    except BaseException:
        if sha(CONFIG.read_bytes()) in (sha(candidate), sha(previous)):
            rollback_impl(record)
        raise


def status():
    if not STATE.exists():
        print("SHADOW TRANSPORT: NONE")
        return
    record = json.loads(STATE.read_text())
    print(json.dumps({
        "phase": record.get("phase"),
        "router_id": record.get("router_id"),
        "expires_unix": record.get("expires_unix"),
        "pilot": {k:v for k,v in record.get("pilot", {}).items()
                  if k not in ("vless_uuid", "ssh_vless_uuid")},
    }, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "rollback", "expire", "status"))
    parser.add_argument("--router-id")
    parser.add_argument("--profile")
    parser.add_argument("--entry-port", type=int, default=21080)
    parser.add_argument("--ssh-port", type=int, default=22080)
    args = parser.parse_args()
    require(os.geteuid() == 0, "root required")
    lifecycle.LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lifecycle.LOCK.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "status":
            status()
        elif args.action == "stage":
            require(args.router_id and args.profile, "--router-id and --profile required")
            stage(args.router_id, args.profile, args.entry_port, args.ssh_port)
        else:
            if args.action == "expire" and not STATE.exists():
                print("SHADOW TRANSPORT: no active transaction")
                return
            require(STATE.exists(), "no transaction to recover")
            record = json.loads(STATE.read_text())
            require(record["phase"] in ("STAGING","STAGED","VERIFIED","ROLLBACK_FAILED"),
                    "transaction is not active")
            if args.action == "expire":
                if int(time.time()) < int(record["expires_unix"]):
                    print("SHADOW TRANSPORT: lease still valid")
                    return
            rollback_impl(record)


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print("SHADOW TRANSACTION ERROR:", str(exc), file=sys.stderr)
        sys.exit(1)
