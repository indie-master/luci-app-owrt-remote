#!/usr/bin/env python3
"""Prepare (never deploy) the unified Belt Docker stack from a Git checkout.

Reads the existing Docker production state and produces a separate, private
candidate. No running container, systemd unit or nginx config is changed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

ROOT = Path("/opt/router-orchestrator")
REPO = Path(__file__).resolve().parents[1]
BASE = ROOT / "release"
CANDIDATE = ROOT / "unified-candidate"
ENV_SOURCE = BASE / ".env"
CONFIG_SOURCE = REPO / "deploy" / "docker-compose.yml"

def check(ok, message):
    if not ok:
        raise RuntimeError(message)

def cmd(*args, timeout=300):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

def read_settings(data):
    settings = {}
    for line in data.decode().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        settings[key] = value
    return settings

def inspect(name, fmt):
    response = cmd("docker", "inspect", "--format", fmt, name)
    check(response.returncode == 0, "cannot inspect " + name)
    return response.stdout.strip()

def main():
    check(os.geteuid() == 0, "root required")
    check(not CANDIDATE.exists(), "candidate already exists")
    check(ENV_SOURCE.is_file() and CONFIG_SOURCE.is_file(), "source files absent")
    check((ROOT / ".cutover.json").is_file(), "Belt migration journal missing")
    phase = json.loads((ROOT / ".cutover.json").read_text()).get("phase")
    check(phase == "DOCKER_ACTIVE", "Belt HUB Docker cutover not active")
    check(inspect("router-orchestrator-hub", "{{.State.Health.Status}}") == "healthy",
          "HUB is not healthy")
    check(inspect("router-orchestrator-xray", "{{.State.Running}}") == "true",
          "Xray is not running")
    check(cmd("systemctl", "is-active", "--quiet",
              "owrt-remote-xray.service").returncode != 0,
          "legacy host Xray must remain stopped")

    old = read_settings(ENV_SOURCE.read_bytes())
    hub = inspect("router-orchestrator-hub", "{{.Config.Image}}")
    xray = inspect("router-orchestrator-xray", "{{.Config.Image}}")
    check(hub.startswith("router-orchestrator-hub:")
          and xray.startswith("router-orchestrator-xray:"),
          "unexpected current Docker images")
    check("RO_STATE_DIR" in old and "RO_PUBLIC_URL" in old,
          "state/public URL missing from active environment")

    key = ROOT / "host-terminal" / "id_ed25519"
    known = ROOT / "host-terminal" / "known_hosts"
    check(key.is_file() and known.is_file(), "pinned VPS terminal SSH keys absent")
    check(key.stat().st_mode & 0o077 == 0, "terminal private key permissions unsafe")
    db = Path(old["RO_STATE_DIR"]) / "hub.db"
    check(db.is_file(), "HUB database missing")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        result = connection.execute("PRAGMA quick_check").fetchone()[0]
        check(result == "ok", "HUB database integrity check failed")
        router_count = connection.execute(
            "select count(*) from routers where deleted_at = 0").fetchone()[0]

    image_sha = hashlib.sha256((REPO / "vps" / "owrt-remote-hub.py").read_bytes()).hexdigest()
    git_commit = cmd("git", "-C", str(REPO), "rev-parse", "--short=12", "HEAD")
    check(git_commit.returncode == 0, "not a Git checkout")
    tag = "router-orchestrator-hub:unified-" + git_commit.stdout.strip()

    print("===== BUILD CANDIDATE HUB =====", flush=True)
    result = cmd("docker", "build", "-f", str(REPO / "docker/hub/Dockerfile"),
                 "--build-arg", "HUB_SOURCE_SHA256=" + image_sha,
                 "-t", tag, str(REPO), timeout=600)
    check(result.returncode == 0, "candidate HUB build failed: " + result.stderr[-400:])

    candidate = {
        "RO_HUB_IMAGE": tag,
        "RO_XRAY_IMAGE": xray,
        "RO_NGINX_IMAGE": "nginx:1.29-alpine",
        "RO_PUBLIC_URL": old["RO_PUBLIC_URL"],
        "RO_PORT": old.get("RO_PORT", "8088"),
        "RO_STATE_DIR": old["RO_STATE_DIR"],
        "RO_XRAY_CONFIG_DIR": "/etc/xray",
        "RO_XRAY_ASSET_DIR": "/usr/local/share/xray",
        "RO_TERMINAL_KEY_PATH": str(key),
        "RO_TERMINAL_KNOWN_HOSTS": str(known),
        "RO_NGINX_CONF": str(REPO / "deploy/nginx/embedded.conf"),
        "RO_NGINX_HEALTH_PORT": "18089",
        "RO_TLS_DIR": str(ROOT / "tls"),
    }

    # Separate candidate Compose and env; no active files are changed.
    CANDIDATE.mkdir(mode=0o700)
    env_file = CANDIDATE / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in candidate.items()))
    os.chmod(env_file, 0o600)
    compose = CANDIDATE / "docker-compose.yml"
    shutil.copyfile(CONFIG_SOURCE, compose)
    os.chmod(compose, 0o600)
    backup_dir = CANDIDATE / "backup"
    backup_dir.mkdir(mode=0o700)
    with sqlite3.connect(str(db)) as live, sqlite3.connect(
            str(backup_dir / "hub.db")) as copy:
        live.backup(copy)
    os.chmod(backup_dir / "hub.db", 0o600)
    (backup_dir / "live-images.json").write_text(
        json.dumps({"hub": hub, "xray": xray,
                    "router_count": router_count,
                    "git_commit": git_commit.stdout.strip()}, indent=2) + "\n"
    )
    os.chmod(backup_dir / "live-images.json", 0o600)

    validate = cmd("docker", "compose",
                   "--project-directory", str(CANDIDATE),
                   "--env-file", str(env_file),
                   "-f", str(compose), "config", "--quiet")
    check(validate.returncode == 0, "candidate Compose invalid: " + validate.stderr[-300:])

    print("CANDIDATE COMPOSE: PASS")
    print("HUB IMAGE BUILT:", tag)
    print("Xray IMAGE PRESERVED:", xray)
    print("ROUTERS IN DB:", router_count)
    print("SQLITE BACKUP: PASS")
    print("CANDIDATE:", str(CANDIDATE))
    print("LIVE HUB, XRAY, NGINX, SYSTEMD: UNCHANGED")
    print("DEPLOYMENT: NOT STARTED")


if __name__ == "__main__":
    main()
