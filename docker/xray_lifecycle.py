#!/usr/bin/env python3
"""Operator-run, fixed-container Xray lifecycle with automatic local rollback.

This tool never downloads arbitrary remote code. Candidate images must already
be available locally, verified separately, and use the approved repository.
The command is intentionally not invoked directly by web-origin data.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path("/opt/router-orchestrator")
ENV = ROOT / ".env"
COMPOSE = ROOT / "docker-compose.yml"
JOURNAL = ROOT / "data" / "xray-lifecycle.json"
LOCK = ROOT / "data" / "xray-lifecycle.lock"
SERVICE = "owrt-remote-xray.service"
CONTAINER = "router-orchestrator-xray"
IMAGE_RE = re.compile(r"^router-orchestrator-xray:[0-9A-Za-z][0-9A-Za-z._-]{0,99}$")
LISTENERS = (18443, 18444)


def run(*args, timeout=90):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".xray-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def docker(*args, timeout=90):
    return run("/usr/bin/docker", *args, timeout=timeout)


def compose(*args, timeout=120):
    return docker("compose", "--project-directory", str(ROOT),
                  "-f", str(COMPOSE), *args, timeout=timeout)


def env_image(data):
    values = [line.partition("=")[2] for line in data.decode().splitlines()
              if line.startswith("RO_XRAY_IMAGE=")]
    require(len(values) == 1, "expected exactly one RO_XRAY_IMAGE")
    require(IMAGE_RE.fullmatch(values[0]) is not None, "unapproved Xray image")
    return values[0]


def replace_image(data, image):
    require(IMAGE_RE.fullmatch(image) is not None, "unapproved image name")
    original = data.decode()
    old = env_image(data)
    lines = original.splitlines()
    updated = [f"RO_XRAY_IMAGE={image}" if line == f"RO_XRAY_IMAGE={old}"
               else line for line in lines]
    return ("\n".join(updated) + "\n").encode()


def container_state():
    result = docker("inspect", "--format",
                    "{{.State.Running}}|{{.Config.Image}}|{{.HostConfig.NetworkMode}}",
                    CONTAINER)
    if result.returncode:
        return None
    parts = result.stdout.strip().split("|")
    if len(parts) != 3 or parts[2] != "host":
        return None
    return parts[0] == "true", parts[1]


def image_id(image):
    result = docker("image", "inspect", "--format", "{{.Id}}", image)
    require(result.returncode == 0, "candidate image does not exist locally")
    return result.stdout.strip()


def active():
    status = container_state()
    if not status or not status[0]:
        return False
    if run("systemctl", "is-active", "--quiet", SERVICE).returncode == 0:
        return False
    import socket
    try:
        for port in LISTENERS:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        return True
    except OSError:
        return False


def wait_healthy(attempts=30):
    for _ in range(attempts):
        if active():
            return True
        time.sleep(1)
    return False


def candidate_test(image):
    require(IMAGE_RE.fullmatch(image) is not None, "unapproved candidate image")
    image_id(image)
    result = docker("run", "--rm", "--network", "none",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "-v", "/etc/xray:/etc/xray:ro",
                    "-v", "/usr/local/share/xray:/usr/local/share/xray:ro",
                    image, "run", "-test",
                    "-config", "/etc/xray/owrt-remote.json", timeout=45)
    require(result.returncode == 0, "candidate Xray config test failed")


def write_journal(old_image, old_id, new_image, new_id, phase):
    atomic(JOURNAL, (json.dumps({
        "old_image": old_image, "old_image_id": old_id,
        "new_image": new_image, "new_image_id": new_id,
        "phase": phase, "updated_unix": int(time.time()),
    }, indent=2) + "\n").encode())


def swap(image, before, *, rollback_image=None):
    atomic(ENV, replace_image(before, image))
    result = compose("up", "-d", "--no-deps", "--no-build",
                     "--force-recreate", "xray")
    require(result.returncode == 0, "Xray container recreation failed")
    require(wait_healthy(), "Docker Xray did not recover both inbounds")
    state = container_state()
    require(state and state[1] == image, "new Xray image mismatch")


def apply(image):
    require(ENV.is_file() and COMPOSE.is_file(), "unified deployment missing")
    require(run("systemctl", "is-active", "--quiet", SERVICE).returncode != 0,
            "host systemd Xray must remain inactive")
    before = ENV.read_bytes()
    old_image = env_image(before)
    require(active(), "current Xray not healthy: use audited manual recovery")
    old_id = image_id(old_image)
    new_id = image_id(image)
    require(old_image != image and old_id != new_id, "candidate matches current image")
    candidate_test(image)
    # Do not silently overwrite an in-progress or crash-interrupted journal.
    if JOURNAL.exists():
        previous = json.loads(JOURNAL.read_text())
        require(previous.get("phase") in ("committed", "rolled-back"),
                "unfinished upgrade; recover from journal first")
    write_journal(old_image, old_id, image, new_id, "switching")
    try:
        swap(image, before)
        write_journal(old_image, old_id, image, new_id, "committed")
        print("XRAY UPGRADE: PASS")
        print("RUNNING IMAGE:", image)
        print("WARNING: confirm live router reverse access before pruning old images")
    except BaseException:
        write_journal(old_image, old_id, image, new_id, "rolling-back")
        try:
            # Do NOT require new container health to perform recovery.
            atomic(ENV, before)
            restore = compose("up", "-d", "--no-deps", "--no-build",
                              "--force-recreate", "xray")
            require(restore.returncode == 0 and wait_healthy(),
                    "rollback failed: requires operator recovery")
            write_journal(old_image, old_id, image, new_id, "rolled-back")
            print("XRAY ROLLBACK: PASS", file=sys.stderr)
        except Exception as error:
            print("CRITICAL ROLLBACK ERROR:", str(error), file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "restart", "upgrade"))
    parser.add_argument("--image", help="pre-verified local Xray image for upgrade")
    args = parser.parse_args()
    require(os.geteuid() == 0, "root required")
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "status":
            print(json.dumps({"running": active(), "container": container_state(),
                              "journal": str(JOURNAL)}, indent=2))
        elif args.action == "restart":
            require(active(), "Xray unhealthy; restart requires manual recovery")
            result = compose("restart", "xray")
            require(result.returncode == 0 and wait_healthy(),
                    "restart failed; operator should inspect container")
            print("XRAY RESTART: PASS")
        else:
            require(args.image, "--image is required")
            apply(args.image)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("XRAY LIFECYCLE ERROR:", str(exc), file=sys.stderr)
        sys.exit(1)
