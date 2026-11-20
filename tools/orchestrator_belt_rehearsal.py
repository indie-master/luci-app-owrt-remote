#!/usr/bin/env python3
"""One-command, source-exact Belt -> Docker shadow rehearsal.

This NEVER stops/restarts systemd, touches live HUB state, changes host Xray,
publishes ports, or connects to routers. It archives a live-state rehearsal
snapshot and then uses a disposable private copy in networkless Docker.

The snapshot contains real credentials; keep it in a private local directory.
This is NOT a migration cutover/restore command.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid

import orchestrator_belt_docker_migration_gate as gate
import orchestrator_docker_rehearsal_restore as shadow
import orchestrator_docker_image_context as image_context
import orchestrator_docker_shadow_runner as shadow_runner

ROOT = Path("/opt/router-orchestrator")
STATE = Path("/var/lib/owrt-remote")
XRAY = Path("/etc/xray/owrt-remote.json")
SOURCE = Path("/opt/owrt-remote/owrt-remote-hub.py")
STATIC = Path("/opt/owrt-remote/static")
LOCK = ROOT / ".rehearsal.lock"


class RehearsalError(RuntimeError):
    pass


def require(ok: bool, msg: str) -> None:
    if not ok:
        raise RehearsalError(msg)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service_active(unit: str) -> bool:
    result = subprocess.run(["systemctl", "is-active", "--quiet", unit],
                            capture_output=True, timeout=10, check=False)
    return result.returncode == 0


def private_root(root: Path) -> None:
    require(root.is_dir() and not root.is_symlink(), "private backup directory missing")
    mode = root.stat()
    require(mode.st_uid == os.geteuid() and stat.S_IMODE(mode.st_mode) & 0o077 == 0,
            "backup directory must be owner-only and owned by this user")


def private_cleanup(path: Path | None, root: Path, prefix: str) -> None:
    if path is None:
        return
    require(path.parent.resolve() == root.resolve() and path.name.startswith(prefix)
            and path.is_dir() and not path.is_symlink(),
            "refusing unsafe temporary path cleanup")
    shutil.rmtree(path)


def rehearsal(*, output_root: Path, dockerfile: Path,
              state: Path = STATE, xray: Path = XRAY, source: Path = SOURCE,
              static: Path = STATIC) -> tuple[Path, dict]:
    require(os.geteuid() == 0, "Belt shadow rehearsal requires root")
    require(service_active("owrt-remote.service"), "production HUB not running")
    require(service_active("owrt-remote-xray.service"), "host Xray not running")
    private_root(output_root)
    require(dockerfile.is_file() and not dockerfile.is_symlink(),
            "reviewed rehearsal Dockerfile missing")
    require(source.is_file() and not source.is_symlink(),
            "production HUB source missing")
    require(static.is_dir() and not static.is_symlink(), "production static assets missing")
    require(shutil.disk_usage(output_root).free >= 512 * 1024 * 1024,
            "insufficient space for private rehearsal")

    live_xray = file_digest(xray)
    live_source = file_digest(source)
    gate.analyze(state, xray)  # Exact DB/router/Xray parity or fail closed.
    snapshot, _manifest = gate.snapshot(state, xray, source, output_root)
    gate.inspect_archive(snapshot)

    staged = None
    context = None
    image = None
    result = None
    try:
        staged, report = shadow.restore(snapshot, output_root)
        context, digest = image_context.build_context(
            staged, static, dockerfile, output_root)
        require(digest == live_source,
                "installed HUB source changed during snapshot")
        image = "router-orchestrator-shadow-local:" + digest[:12] + "-" + uuid.uuid4().hex[:8]
        build = subprocess.run(
            ["docker", "build", "--quiet", "--file", str(context / "Dockerfile"),
             "--build-arg", "SOURCE_SHA256=" + digest, "--tag", image, str(context)],
            capture_output=True, text=True, timeout=360, check=False)
        require(build.returncode == 0,
                "isolated Docker image build failed; inspect private build environment")
        result = shadow_runner.run_rehearsal(staged, image)
        require(result["health"] == "PASS", "isolated HUB failed health")
        require(file_digest(xray) == live_xray and file_digest(source) == live_source,
                "production Xray config or HUB source changed during rehearsal")
        require(service_active("owrt-remote.service") and
                service_active("owrt-remote-xray.service"),
                "production service changed during rehearsal")
        return snapshot, result
    finally:
        if image is not None:
            subprocess.run(["docker", "image", "rm", image],
                           capture_output=True, timeout=25, check=False)
        private_cleanup(context, output_root, "orchestrator-shadow-image-")
        private_cleanup(staged, output_root, "orchestrator-shadow-state-")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root", type=Path, default=ROOT / "backups")
    p.add_argument("--dockerfile", type=Path,
                   default=Path(__file__).resolve().parents[1] /
                   "docker/hub/Dockerfile.rehearsal")
    # Some production HUB installations inline their UI and have no
    # /opt/owrt-remote/static directory. Use the pinned repository assets
    # for this isolated smoke test only; source code remains source-exact.
    repo_static = Path(__file__).resolve().parents[1] / "vps/static"
    existing_static = STATIC.is_dir() and any(p.is_file() for p in STATIC.rglob("*"))
    p.add_argument("--static", type=Path,
                   default=STATIC if existing_static else repo_static)
    args = p.parse_args()
    try:
        require(ROOT.is_dir() and not ROOT.is_symlink(),
                "Router Orchestrator root missing")
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RehearsalError("another rehearsal is running") from exc
            snapshot, result = rehearsal(
                output_root=args.output_root, dockerfile=args.dockerfile,
                static=args.static)
        finally:
            os.close(fd)
        print("BELT DOCKER REHEARSAL: PASS")
        print("FLEET: {routers} routers | WSS={wss} TCP={tcp}".format(**result))
        print("HISTORY: router_logs=" + str(result["logs"]))
        print("HEALTH: " + result["health"])
        print("PRIVATE BACKUP:", snapshot)
        print("CLEANUP: disposable state, image and build context removed")
        print("PRODUCTION: services and live files unchanged")
        print("NOT A CUTOVER: LuCI/SSH real connectivity still needs acceptance")
        return 0
    except (RehearsalError, gate.GateError, shadow.Blocked,
            subprocess.TimeoutExpired, OSError, ValueError,
            image_context.shadow.Blocked, shadow_runner.PilotError) as exc:
        print("BELT DOCKER REHEARSAL: BLOCKED — " + type(exc).__name__,
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
