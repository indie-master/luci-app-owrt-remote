#!/usr/bin/env python3
"""Guarded Belt unified Compose cutover. Does not restart Xray or edit outer nginx.

Requires prepare_belt.py candidate and a clean pinned Git worktree at
/opt/router-orchestrator/src. Creates a durable pre-cutover backup and restores
the previous HUB + adapter if any health gate fails.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path("/opt/router-orchestrator")
SRC = ROOT / "src"
RELEASE = ROOT / "release"
STAGE = ROOT / "unified-candidate"
ENV = ROOT / ".env"
COMPOSE = ROOT / "docker-compose.yml"
UNIT = Path("/etc/systemd/system/router-orchestrator-host-adapter.service")
OVERRIDE = Path("/etc/systemd/system/router-orchestrator-host-adapter.service.d/50-docker-xray.conf")
CORE_UNIT = Path("/etc/systemd/system/router-orchestrator-core-job@.service")
BACKUP = STAGE / "cutover-rollback"
SYSTEMD = "router-orchestrator-host-adapter.service"


def run(*args, timeout=120, cwd=None):
    return subprocess.run(args, cwd=cwd or ROOT, capture_output=True,
                          text=True, timeout=timeout)


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def health(container):
    response = run("docker", "inspect", "--format",
                   "{{.State.Health.Status}}", container)
    return response.returncode == 0 and response.stdout.strip() == "healthy"


def check_new():
    for _ in range(35):
        if health("router-orchestrator-hub") and health("router-orchestrator-nginx"):
            ready = run("curl", "--fail", "--silent", "--show-error",
                        "--max-time", "3",
                        "http://127.0.0.1:18089/healthz")
            if ready.returncode == 0:
                return True
        time.sleep(1)
    return False


def restore_file(path, backup_file, existed):
    if existed:
        shutil.copy2(backup_file, path)
    else:
        path.unlink(missing_ok=True)


def main():
    require(os.geteuid() == 0, "root required")
    require(SRC.resolve() == Path(__file__).resolve().parents[1],
            "script must run from the pinned src worktree")
    require((SRC / ".git").exists(), "src is not a Git checkout")
    require(STAGE.is_dir() and not BACKUP.exists(),
            "candidate absent or previous cutover backup present")
    require(not ENV.exists() and not COMPOSE.exists(),
            "root compose/env already exists; refuse overwrite")
    require(UNIT.is_file() and not UNIT.is_symlink(),
            "existing host-adapter systemd unit absent")
    require(run("systemctl", "is-active", "--quiet", SYSTEMD).returncode == 0,
            "current host-adapter not running")
    require(run("systemctl", "is-active", "--quiet",
                "owrt-remote-xray.service").returncode != 0,
            "legacy Xray is active")
    require(health("router-orchestrator-hub"), "old HUB unhealthy")
    xray_before = run("docker", "inspect", "--format",
                      "{{.Id}}", "router-orchestrator-xray")
    require(xray_before.returncode == 0, "Docker Xray missing")
    xray_id = xray_before.stdout.strip()
    image_before = run("docker", "inspect", "--format",
                       "{{.Config.Image}}", "router-orchestrator-hub").stdout.strip()
    require(image_before.startswith("router-orchestrator-hub:"),
            "unexpected prior HUB image")
    require((STAGE / ".env").is_file() and
            (STAGE / "docker-compose.yml").is_file(),
            "candidate artifacts missing")
    require((STAGE / "backup/hub.db").is_file(), "database backup missing")
    require((SRC / "deploy/systemd/router-orchestrator-host-adapter.service").is_file(),
            "new host-adapter unit missing")
    require((SRC / "deploy/systemd/router-orchestrator-core-job@.service").is_file(),
            "new core worker unit missing")

    git_clean = run("git", "-C", str(SRC), "diff", "--quiet")
    require(git_clean.returncode == 0, "worktree has uncommitted edits")
    config = run("docker", "compose", "--project-directory", str(STAGE),
                 "--env-file", str(STAGE / ".env"),
                 "-f", str(STAGE / "docker-compose.yml"), "config", "--quiet")
    require(config.returncode == 0, "candidate Compose invalid")

    BACKUP.mkdir(mode=0o700)
    shutil.copy2(UNIT, BACKUP / "host-adapter.service")
    had_override = OVERRIDE.is_file()
    if had_override:
        require(not OVERRIDE.is_symlink(), "unsafe override symlink")
        shutil.copy2(OVERRIDE, BACKUP / "50-docker-xray.conf")
    had_core_unit = CORE_UNIT.is_file()
    if had_core_unit:
        shutil.copy2(CORE_UNIT, BACKUP / "core-job.service")
    (BACKUP / "images.json").write_text(json.dumps({
        "hub": image_before, "xray_id": xray_id
    }, indent=2) + "\n")
    os.chmod(BACKUP / "images.json", 0o600)
    (BACKUP / "cutover-state").write_text("PREPARED\n")
    os.chmod(BACKUP / "cutover-state", 0o600)

    live_changed = False
    try:
        print("===== DEPLOY HOST ADAPTER =====", flush=True)
        shutil.copy2(SRC / "deploy/systemd/router-orchestrator-host-adapter.service", UNIT)
        if had_override:
            OVERRIDE.unlink()
        shutil.copy2(SRC / "deploy/systemd/router-orchestrator-core-job@.service", CORE_UNIT)
        jobs = ROOT / "data/core-jobs"
        jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chown(jobs, 0, 0)
        os.chmod(jobs, 0o700)
        require(run("systemctl", "daemon-reload").returncode == 0,
                "systemd reload failed")
        require(run("systemctl", "restart", SYSTEMD).returncode == 0,
                "new adapter failed to restart")
        require(run("systemctl", "is-active", "--quiet", SYSTEMD).returncode == 0,
                "new adapter inactive")

        print("===== INSTALL SINGLE COMPOSE =====", flush=True)
        shutil.copy2(STAGE / ".env", ENV)
        os.chmod(ENV, 0o600)
        COMPOSE.symlink_to(SRC / "deploy/docker-compose.yml")
        live_changed = True

        result = run("docker", "compose", "up", "-d",
                     "--no-deps", "--no-build", "hub", "nginx", timeout=180)
        require(result.returncode == 0,
                "HUB/nginx Compose upgrade failed: " + result.stderr[-400:])
        require(check_new(), "new HUB or project nginx unhealthy")

        xray_after = run("docker", "inspect", "--format",
                         "{{.Id}}", "router-orchestrator-xray")
        require(xray_after.returncode == 0 and xray_after.stdout.strip() == xray_id,
                "Xray container ID changed unexpectedly; refusing cutover")

        # The new HUB must access the credential-scoped adapter socket.
        probe = (
            "import socket,json;"
            "s=socket.socket(socket.AF_UNIX);"
            "s.settimeout(5);s.connect('/run/router-orchestrator/control.sock');"
            "s.sendall(b'{\\\"op\\\":\\\"xray.core.info\\\"}\\n');"
            "data=b'';"
            "\nwhile not data.endswith(b'\\n'):\n data+=s.recv(4096)"
            "\nr=json.loads(data);assert r.get('ok') and r.get('active')"
        )
        result = run("docker", "exec", "router-orchestrator-hub",
                     "python", "-c", probe, timeout=20)
        require(result.returncode == 0, "new HUB cannot reach Xray host adapter")

        (BACKUP / "cutover-state").write_text("DOCKER_THREE_SERVICE_ACTIVE\n")
        print("UNIFIED COMPOSE: PASS")
        print("HUB, XRAY, NGINX: PRESENT")
        print("HOST ADAPTER: ACTIVE")
        print("XRAY CONTAINER ID: UNCHANGED")
        print("OUTER NGINX: UNCHANGED")
        print("STAGING AND OLD HUB IMAGE: RETAINED")
        print("MIGRATION: NEEDS FLEET AND REBOOT ACCEPTANCE")

    except BaseException as error:
        print("CUTOVER ERROR:", error, flush=True)
        print("===== RESTORE OLD HUB AND HOST ADAPTER =====", flush=True)
        if live_changed:
            # Restore the root Compose entry point before reusing old overlays.
            COMPOSE.unlink(missing_ok=True)
            ENV.unlink(missing_ok=True)
            run("docker", "rm", "-f", "router-orchestrator-nginx", timeout=35)
        shutil.copy2(BACKUP / "host-adapter.service", UNIT)
        if had_override:
            OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(BACKUP / "50-docker-xray.conf", OVERRIDE)
        else:
            OVERRIDE.unlink(missing_ok=True)
        restore_file(CORE_UNIT, BACKUP / "core-job.service", had_core_unit)
        run("systemctl", "daemon-reload")
        run("systemctl", "restart", SYSTEMD)
        old = run("docker", "compose", "up", "-d", "--no-build",
                  "--no-deps", "--force-recreate", "hub",
                  cwd=RELEASE, timeout=180)
        if old.returncode == 0 and health("router-orchestrator-hub"):
            print("ROLLBACK TO OLD HUB: PASS")
        else:
            print("CRITICAL: old HUB rollback not confirmed", flush=True)
        raise


if __name__ == "__main__":
    main()
