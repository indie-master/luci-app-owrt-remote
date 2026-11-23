#!/usr/bin/env python3
"""Read-only Belt host-adapter acceptance. Never starts or changes services.

preflight: inspect pinned Git checkout, systemd/Xray health, reviewed unit.
probe: after separately installing read-only unit, prove UID-23088 socket
access, reject UID-0, compare live Xray SHA without printing secrets.

NO Xray restart/apply API requests are ever sent by this tool.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
SOCKET = Path("/run/router-orchestrator/control.sock")
UNIT = Path("/etc/systemd/system/router-orchestrator-host-adapter.service")
PINNED = Path("/opt/router-orchestrator/host-adapter/host_adapter.py")
XRAY = Path("/etc/xray/owrt-remote.json")
EXPECTED_UID = 23088
OPS = ("health", "xray.status", "host.metrics", "xray.current")
MAX_RESPONSE = 2 * 1024 * 1024


class Blocked(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Blocked(message)


def run_command(*command: str):
    return subprocess.run(command, capture_output=True, text=True,
                          timeout=12, check=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def check_checkout(expected: str) -> None:
    require(re.fullmatch(r"[0-9a-f]{40}", expected) is not None,
            "expected commit must be a complete SHA")
    head = run_command("git", "-C", str(REPO), "rev-parse", "HEAD")
    dirty = run_command("git", "-C", str(REPO), "status", "--porcelain",
                        "--untracked-files=all")
    require(head.returncode == 0 and head.stdout.strip() == expected,
            "release Git HEAD differs from reviewed commit")
    require(dirty.returncode == 0 and not dirty.stdout.strip(),
            "release checkout has unreviewed changes")


def check_source_service() -> None:
    source = (REPO / "docker/host_adapter.service").read_text()
    require("ProtectSystem=strict" in source and
            "ReadWritePaths=/run/router-orchestrator" in source,
            "read-only service filesystem policy changed")
    require("CapabilityBoundingSet=CAP_CHOWN CAP_FOWNER" in source,
            "read-only service capability policy changed")
    exec_lines = [line for line in source.splitlines()
                  if line.strip().startswith("ExecStart=")]
    require(len(exec_lines) == 1 and
            str(PINNED) in exec_lines[0] and
            "--peer-uid 23088" in exec_lines[0] and
            "--allow-xray-apply" not in exec_lines[0] and
            "--allow-xray-restart" not in exec_lines[0],
            "read-only host adapter command changed")
    require(not (REPO / "docker/host_adapter.py").is_symlink(),
            "adapter source is symlink")
    if UNIT.exists():
        require(not UNIT.is_symlink() and UNIT.read_text() == source,
                "installed unit differs from reviewed read-only source")
        for root in (Path("/etc/systemd/system"), Path("/run/systemd/system")):
            require(not (root / (UNIT.name + ".d")).exists(),
                    "host adapter unit override directory exists")
    if PINNED.exists():
        require(not PINNED.is_symlink() and
                sha256(PINNED) == sha256(REPO / "docker/host_adapter.py"),
                "installed privileged adapter differs from pinned source")


def check_systemd(name: str) -> None:
    result = run_command("systemctl", "is-active", "--quiet", name)
    require(result.returncode == 0, name + " is not active")


def preflight(expected: str) -> None:
    require(os.geteuid() == 0, "Belt host-adapter acceptance requires root")
    check_checkout(expected)
    check_source_service()
    check_systemd("owrt-remote.service")
    check_systemd("owrt-remote-xray.service")
    require(XRAY.is_file() and not XRAY.is_symlink(),
            "host Xray configuration is missing/unsafe")
    print("HOST ADAPTER PREFLIGHT: PASS")
    print("RELEASE: reviewed, clean Git checkout")
    print("PRODUCTION: systemd HUB and host Xray active")
    print("HOST ADAPTER: read-only service definition verified")
    print("NO CHANGE: no service or Xray action executed")


def read_one(operation: str) -> dict:
    require(operation in OPS, "unsupported read-only probe operation")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(4)
        channel.connect(str(SOCKET))
        channel.sendall(json.dumps({"op": operation}).encode() + b"\n")
        with channel.makefile("rb") as reader:
            raw = reader.readline(MAX_RESPONSE + 1)
        require(raw.endswith(b"\n") and len(raw) <= MAX_RESPONSE,
                "adapter response incomplete")
    response = json.loads(raw)
    require(isinstance(response, dict) and response.get("ok") is True,
            "host adapter read request rejected")
    return response


def peer_summaries() -> dict:
    """Run only in the UID-restricted fork: never return Xray config JSON."""
    health = read_one("health")
    require(health.get("adapter") == "host-control-v1",
            "adapter protocol mismatch")
    status = read_one("xray.status")
    require(status.get("active") is True and
            status.get("service") == "owrt-remote-xray",
            "dedicated host Xray not healthy")
    metrics = read_one("host.metrics")
    require(metrics.get("scope") == "host-adapter" and
            isinstance(metrics.get("memory"), dict),
            "host metrics invalid")
    current = read_one("xray.current")
    require(isinstance(current.get("config"), dict) and
            re.fullmatch(r"[0-9a-f]{64}", str(current.get("sha256"))) is not None,
            "Xray current response invalid")
    return {"xray_sha256": current["sha256"], "metrics": "PASS",
            "xray_status": "active"}


def restricted_probe() -> dict:
    """Fork as the container's numeric UID; no credentials on parent pipe."""
    require(os.geteuid() == 0, "root required for numeric UID probe")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            os.setgroups([])
            os.setgid(EXPECTED_UID)
            os.setuid(EXPECTED_UID)
            result = {"ok": True, "summary": peer_summaries()}
        except BaseException as exc:
            result = {"ok": False, "reason": type(exc).__name__}
        try:
            os.write(write_fd, json.dumps(result).encode())
            os.close(write_fd)
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        raw = os.read(read_fd, 2048)
    finally:
        os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    require(os.WIFEXITED(status), "UID-restricted probe terminated abnormally")
    data = json.loads(raw)
    require(data.get("ok") is True,
            "numeric UID-23088 probe failed: " + str(data.get("reason", "unknown")))
    return data["summary"]


def root_peer_rejected() -> bool:
    """A root client cannot bypass the explicit peer-UID policy."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(3)
        channel.connect(str(SOCKET))
        # The server must close immediately without reading the request.
        return channel.recv(16) == b""


def probe(expected: str) -> None:
    preflight(expected)
    check_systemd("router-orchestrator-host-adapter.service")
    require(UNIT.is_file() and PINNED.is_file(),
            "read-only host adapter has not been installed")
    show = run_command("systemctl", "show",
                       "router-orchestrator-host-adapter.service",
                       "-p", "DropInPaths", "-p", "FragmentPath")
    require(show.returncode == 0 and
            "DropInPaths=\n" in show.stdout and
            "FragmentPath=" + str(UNIT) in show.stdout,
            "systemd unit has an unexpected override or source")
    require(SOCKET.is_socket() and not SOCKET.is_symlink(),
            "read-only adapter UNIX socket is not active")
    require(root_peer_rejected(), "socket incorrectly authorizes root peer")
    summary = restricted_probe()
    require(summary["xray_sha256"] == sha256(XRAY),
            "socket Xray state differs from current host config SHA")
    print("HOST ADAPTER READONLY PROBE: PASS")
    print("AUTH: root peer denied; numeric UID-23088 accepted")
    print("READ: health, Xray status, host metrics, Xray SHA verified")
    print("WRITE: no mutation API requests sent")
    print("PRODUCTION: no HUB/Xray restart or cutover executed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "probe"))
    parser.add_argument("--expect-commit", required=True)
    args = parser.parse_args()
    try:
        if args.mode == "preflight":
            preflight(args.expect_commit)
        else:
            probe(args.expect_commit)
        return 0
    except (Blocked, OSError, ValueError, json.JSONDecodeError,
            subprocess.TimeoutExpired) as exc:
        # Avoid printing raw Xray contents or socket payloads from exceptions.
        print("HOST ADAPTER ACCEPTANCE: BLOCKED — " +
              (str(exc)[:180] if isinstance(exc, Blocked)
               else type(exc).__name__), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
