#!/usr/bin/env python3
"""Legacy Xray status and recovery helpers for the existing watchdog."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import math
import sqlite3
import socket
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
TRANSPORT_HOLD = ROOT / "data" / ".xray-maintenance-hold"
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
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def docker(*args, timeout=90):
    from manager import inherited_lock, local_docker_env

    return subprocess.run(("/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args),
                          capture_output=True, text=True, timeout=timeout,
                          env=local_docker_env(), pass_fds=inherited_lock())


def compose(*args, timeout=120):
    return docker("compose", "--project-directory", str(ROOT),
                  "--env-file", str(ENV), "-f", str(COMPOSE), *args, timeout=timeout)


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
    key = "RO_XRAY_IMAGE="
    return "".join(
        key + image + line[len(key + old):] if line.rstrip("\r\n") == key + old else line
        for line in original.splitlines(keepends=True)
    ).encode()


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


def active_config():
    result = docker("inspect", "--format", "{{json .Mounts}}", CONTAINER)
    require(result.returncode == 0, "unable to inspect managed Xray mounts")
    targets = {"/etc/xray", "/usr/local/share/xray", "/etc/router-orchestrator/tls"}
    mounts = {}
    for mount in json.loads(result.stdout):
        target = mount.get("Destination")
        if target not in targets:
            continue
        source = mount.get("Source", "")
        require(mount.get("Type") == "bind" and mount.get("RW") is False
                and Path(source).is_absolute() and "," not in source and target not in mounts,
                "unexpected managed Xray mount")
        mounts[target] = source
    require({"/etc/xray", "/usr/local/share/xray"} <= mounts.keys(),
            "managed Xray config or assets mount is missing")
    return mounts


def candidate_test(image):
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image), "invalid candidate image ID")
    mounts = active_config()
    args = []
    for target, source in mounts.items():
        args.extend(("--mount", f"type=bind,src={source},dst={target},readonly"))
    user = docker("inspect", "--format", "{{.Config.User}}", CONTAINER)
    require(user.returncode == 0, "unable to inspect managed Xray container user")
    if user.stdout.strip():
        args.extend(("--user", user.stdout.strip()))
    result = docker("run", "--rm", "--pull", "never", "--network", "none", "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
                    "-e", "XRAY_LOCATION_ASSET=/usr/local/share/xray",
                    *args, image, "run", "-test",
                    "-config", "/etc/xray/owrt-remote.json", timeout=45)
    require(result.returncode == 0, "candidate Xray config test failed")


def config_digest(config):
    return hashlib.sha256(Path(config).read_bytes()).hexdigest()


def write_journal(transaction, phase):
    atomic(JOURNAL, (json.dumps({
        **transaction, "phase": phase, "updated_unix": int(time.time()),
    }, indent=2) + "\n").encode())


def swap(image, before, *, expected_id):
    require(image_id(image) == expected_id, "Xray image changed before recreation")
    atomic(ENV, replace_image(before, image))
    result = compose("up", "-d", "--no-deps", "--no-build",
                     "--force-recreate", "xray")
    require(result.returncode == 0, "Xray container recreation failed")
    require(wait_healthy(), "Docker Xray did not recover both inbounds")
    state = container_state()
    running = docker("inspect", "--format", "{{.Image}}", CONTAINER)
    require(state and state[1] == image and running.returncode == 0
            and running.stdout.strip() == expected_id, "new Xray image mismatch")


def online_router_baseline(env_content):
    state_dir = None
    for line in env_content.decode().splitlines():
        if line.startswith("RO_STATE_DIR="):
            state_dir = line.split("=", 1)[1]
    require(state_dir is not None, "RO_STATE_DIR missing")
    database = Path(state_dir) / "hub.db"
    require(database.is_file(), "HUB database unavailable")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        rows = db.execute(
            "select id, last_seen, ssh_entry_port from routers where deleted_at = 0"
        ).fetchall()
    now = int(time.time())
    return database, {row[0]: int(row[2] or 0) for row in rows
                      if int(row[1] or 0) >= now - 180 and int(row[2] or 0) > 0}


def canary_confirmed(database, baseline, since):
    if not baseline:
        return False
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        heartbeat = dict(db.execute(
            "select id, last_seen from routers where deleted_at = 0"
        ).fetchall())
    healthy = 0
    for router_id, ssh_port in baseline.items():
        if int(heartbeat.get(router_id) or 0) < since:
            continue
        try:
            with socket.create_connection(("127.0.0.1", ssh_port), timeout=1) as sock:
                sock.settimeout(2)
                banner = sock.recv(32)
                if banner.startswith(b"SSH-"):
                    healthy += 1
        except OSError:
            continue
    return healthy >= max(1, math.ceil(len(baseline) * 0.8))


def wait_canary(database, baseline, since):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if canary_confirmed(database, baseline, since):
            return True
        time.sleep(1)
    return False


def retain_versions(history, *verified):
    retained = []
    for version in (*verified, *history):
        if all(item["image_id"] != version["image_id"] for item in retained):
            retained.append(version)
        if len(retained) == 3:
            break
    return retained


def terminal_journal():
    if not JOURNAL.exists():
        return {}
    previous = json.loads(JOURNAL.read_text())
    require(previous.get("phase") in ("committed", "rolled-back"),
            "unfinished upgrade; recover from journal first")
    return previous


def apply(image):
    require(ENV.is_file() and COMPOSE.is_file(), "unified deployment missing")
    require(not (TRANSPORT_HOLD.exists() or TRANSPORT_HOLD.is_symlink()), "shadow transport migration active: lifecycle mutation blocked")
    require(run("systemctl", "is-active", "--quiet", SERVICE).returncode != 0,
            "host systemd Xray must remain inactive")
    previous = terminal_journal()
    before = ENV.read_bytes()
    old_image = env_image(before)
    require(active(), "current Xray not healthy: use audited manual recovery")
    old_id = image_id(old_image)
    new_id = image_id(image)
    require(old_image != image and old_id != new_id, "candidate matches current image")
    # Reject manually retagged, non-verified or mutable candidates.
    tag = image.rsplit(":", 1)[1]
    manifest_path = ROOT / "data" / "xray-candidates" / (tag + ".json")
    require(manifest_path.is_file(), "stable release manifest is missing")
    manifest = json.loads(manifest_path.read_text())
    pinned = manifest.get("official_digest_ref", "")
    require(manifest.get("image") == image and manifest.get("image_id") == new_id
            and manifest.get("validation") == "config-only"
            and isinstance(pinned, str)
            and re.fullmatch(r"ghcr\.io/xtls/xray-core@sha256:[0-9a-f]{64}", pinned)
            and image_id(pinned) == new_id,
            "candidate image digest differs from verified release")
    database, baseline = online_router_baseline(before)
    require(baseline, "no recently online routers available for canary validation")
    config = str(Path(active_config()["/etc/xray"]) / "owrt-remote.json")
    config_before = config_digest(config)
    candidate_test(new_id)
    require(config_digest(config) == config_before, "Xray configuration changed during validation")
    # Do not silently overwrite an in-progress or crash-interrupted journal.
    transaction = {
        "known_good": retain_versions(previous.get("known_good", []),
                                      {"image": old_image, "image_id": old_id}),
        "old_image": old_image, "old_image_id": old_id,
        "new_image": image, "new_image_id": new_id,
        "config_path": config, "config_sha256": config_before,
        "old_env_sha256": hashlib.sha256(before).hexdigest(),
        "new_env_sha256": hashlib.sha256(replace_image(before, image)).hexdigest(),
    }
    write_journal(transaction, "switching")
    started = int(time.time())
    try:
        swap(image, before, expected_id=new_id)
        require(wait_canary(database, baseline, started),
                "candidate failed live router heartbeat and SSH canary")
    except BaseException:
        try:
            rollback()
            print("XRAY ROLLBACK: PASS", file=sys.stderr)
        except Exception as error:
            print("CRITICAL ROLLBACK ERROR:", str(error), file=sys.stderr)
        raise
    transaction["known_good"] = retain_versions(
        transaction["known_good"], {"image": image, "image_id": new_id})
    write_journal(transaction, "committed")
    print("XRAY UPGRADE: PASS")
    print("RUNNING IMAGE:", image)


def rollback():
    """Restore the known-good image after failure, interruption or explicit rollback."""
    require(JOURNAL.is_file(), "no lifecycle journal")
    transaction = json.loads(JOURNAL.read_text())
    phase = transaction.get("phase")
    require(phase in ("committed", "switching", "rolling-back"),
            "journal is not in recoverable state")
    old = transaction["old_image"]
    new = transaction["new_image"]
    require(IMAGE_RE.fullmatch(old) is not None and IMAGE_RE.fullmatch(new) is not None,
            "invalid journal image name")
    if "config_sha256" in transaction:
        require(config_digest(transaction["config_path"]) == transaction["config_sha256"],
                "Xray configuration changed since core transaction")
    before = ENV.read_bytes()
    deployed = env_image(before)
    if "old_env_sha256" in transaction:
        expected = {"committed": (transaction["new_env_sha256"],),
                    "switching": (transaction["old_env_sha256"], transaction["new_env_sha256"]),
                    "rolling-back": (transaction["old_env_sha256"], transaction["new_env_sha256"])}
        require(hashlib.sha256(before).hexdigest() in expected[phase],
                "deployment environment changed since core transaction")
    else:
        # Earlier committed journals can still be rolled back. An old
        # switching journal has no evidence that its environment is intact.
        require(phase != "switching" and deployed == new,
                "deployment environment cannot be verified against legacy journal")
    require(deployed in (old, new), "deployed image does not match journal")
    require(run("systemctl", "is-active", "--quiet", SERVICE).returncode != 0,
            "legacy systemd Xray is active")
    require(image_id(old) == transaction["old_image_id"],
            "known-good image digest changed")
    write_journal(transaction, "rolling-back")
    swap(old, before, expected_id=transaction["old_image_id"])
    transaction["known_good"] = retain_versions(
        transaction.get("known_good", []),
        {"image": old, "image_id": transaction["old_image_id"]})
    write_journal(transaction, "rolled-back")
    print("XRAY ROLLBACK: PASS")
    print("RUNNING IMAGE:", old)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "restart", "upgrade", "rollback"))
    parser.add_argument("--image", help="pre-verified local Xray image for upgrade")
    args = parser.parse_args()
    require(os.geteuid() == 0, "root required")
    require(args.action == "status", "Core maintenance requires a managed unified Docker stack")
    print(json.dumps({"running": active(), "container": container_state(),
                      "journal": str(JOURNAL)}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("XRAY LIFECYCLE ERROR:", str(exc), file=sys.stderr)
        sys.exit(1)
