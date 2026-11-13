#!/usr/bin/env python3
"""Run the approved HUB source in a disposable, networkless Docker rehearsal.

Only runs after an explicit operator command. Mounts a VERIFIED COPY of
HUB state (never active Belt state). No host Nginx, Xray config, Xray
sockets, Docker socket, or real network. Docker logs disabled because
the legacy HUB displays AGENT_TOKEN on startup.

NOT a production image runner or a cutover coordinator.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import sys
import time

import orchestrator_docker_rehearsal_restore as shadow
import orchestrator_belt_docker_migration_gate as gate

IMAGE_PATTERN = re.compile(r"^[a-zA-Z0-9_./:@-]{1,200}$")
ENTRY_SOURCE_LABEL = "org.router-orchestrator.hub-source-sha256"
PURPOSE_LABEL = "org.router-orchestrator.purpose"
HEALTH = (
    "import json,urllib.request;"
    "r=urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=3);"
    "assert json.load(r).get('ok') is True"
)


class PilotError(RuntimeError):
    pass


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise PilotError(reason)


def docker(*args: str, timeout: int = 35) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True,
                          text=True, timeout=timeout)


def image_label(image: str, key: str) -> str:
    result = docker("image", "inspect", "--format",
                    '{{ index .Config.Labels "' + key + '" }}', image)
    require(result.returncode == 0, "Docker image inspection failed")
    return result.stdout.strip()


def ensure_image_matches_snapshot(image: str, report: dict) -> None:
    require(IMAGE_PATTERN.fullmatch(image) is not None, "invalid image reference")
    require(image_label(image, PURPOSE_LABEL) == "isolated-hub-rehearsal",
            "image is not an isolated HUB rehearsal build")
    require(image_label(image, ENTRY_SOURCE_LABEL) == report["source_sha256"],
            "image HUB source differs from archived Belt source")


def router_credentials(state: Path) -> list[tuple]:
    conn = sqlite3.connect((state / "hub.db").resolve().as_uri() + "?mode=ro",
                           uri=True, timeout=10)
    try:
        columns = {r[1] for r in conn.execute("pragma table_info(routers)")}
        fallback = "vps_allow_legacy" if "vps_allow_legacy" in columns else "0"
        return conn.execute(
            "select id, vless_uuid, ssh_vless_uuid, entry_port, "
            "ssh_entry_port, vps_transport, vless_port, " +
            fallback + " as vps_allow_legacy from routers order by id").fetchall()
    finally:
        conn.close()


def run_rehearsal(stage: Path, image: str, *, attempts: int = 25) -> dict:
    # Only a fresh, unmodified restore copy. Never run against live state.
    before = shadow.verify_staged(stage)
    require(stage.name.startswith("orchestrator-shadow-state-"),
            "stage must be a restored rehearsal directory")
    require(stage.resolve() != Path("/var/lib/owrt-remote").resolve(),
            "active HUB state cannot be mounted")
    ensure_image_matches_snapshot(image, before)
    previous = router_credentials(stage / "state")
    name = "orchestrator-shadow-" + secrets.token_hex(5)
    mount = ("type=bind,src=" + str((stage / "state").resolve()) +
             ",dst=/var/lib/owrt-remote")
    cmd = [
        "run", "--detach", "--rm",
        "--name", name,
        "--log-driver", "none",  # legacy source prints AGENT_TOKEN
        "--network", "none",
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "128",
        "--memory", "384m",
        "--cpus", "1.0",
        "--mount", mount,
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
        "--env", "OWRT_REMOTE_XRAY_CONTROL_BACKEND=external_readonly",
        "--env", "OWRT_REMOTE_STATE_DIR=/var/lib/owrt-remote",
        "--env", "OWRT_REMOTE_EXTRA_PORTS=",
        "--env", "OWRT_REMOTE_TLS_PORTS=",
        image,
    ]
    # No host ports, no host network, no live state. Internal health is
    # reachable only by docker exec inside the isolated container.
    container = ""
    try:
        launched = docker(*cmd, timeout=45)
        require(launched.returncode == 0, "cannot launch isolated Docker container")
        container = launched.stdout.strip()
        require(re.fullmatch(r"[0-9a-f]{64}", container) is not None,
                "Docker did not return a complete container ID")
        accepted = False
        for _ in range(attempts):
            health = docker("exec", container, "python3", "-c", HEALTH, timeout=8)
            if health.returncode == 0:
                accepted = True
                break
            inspect = docker("inspect", "--format", "{{.State.Running}}",
                             container, timeout=7)
            require(inspect.returncode == 0 and inspect.stdout.strip() == "true",
                    "isolated HUB container exited or became unavailable")
            time.sleep(1)
        require(accepted, "isolated HUB health check did not pass")
        after = gate.db_inventory(stage / "state" / "hub.db")
        require(after["tables"].get("routers") == before["tables"].get("routers")
                and after["tables"].get("router_logs") == before["tables"].get("router_logs"),
                "shadow HUB changed router or historical log row count")
        require(after["active_count"] == before["routers"] and
                after["tcp"] == before["tcp"] and
                after["wss"] == before["wss"],
                "shadow HUB transport inventory drifted")
        require(router_credentials(stage / "state") == previous,
                "shadow HUB modified reverse identities or router settings")
        return {"health": "PASS", "routers": after["active_count"],
                "tcp": after["tcp"], "wss": after["wss"],
                "logs": after["tables"].get("router_logs", -1),
                "note": "Networkless rehearsal; no LuCI/SSH connectivity tested"}
    finally:
        if container:
            docker("rm", "--force", container, timeout=15)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", type=Path, required=True)
    p.add_argument("--image", required=True)
    args = p.parse_args()
    try:
        result = run_rehearsal(args.stage, args.image)
        print("DOCKER SHADOW: PASS")
        print("HEALTH:", result["health"])
        print("FLEET: {routers} routers | WSS={wss} TCP={tcp}".format(**result))
        print("HISTORY: router_logs=" + str(result["logs"]))
        print("SOURCE: exact archived HUB source SHA verified against Docker image label")
        print("BELT: Xray/Nginx/systemd and live SQLite were never mounted or modified")
        print("NO CUTOVER: Docker container stopped; disposable state copy can be discarded")
        return 0
    except PilotError as exc:
        # PilotError messages are fixed operator-facing text; no credential material.
        print("DOCKER SHADOW: BLOCKED — " + str(exc), file=sys.stderr)
        return 2
    except (shadow.Blocked, gate.GateError, OSError,
            ValueError, TypeError, subprocess.TimeoutExpired, sqlite3.Error) as exc:
        print("DOCKER SHADOW: BLOCKED — " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
