#!/usr/bin/env python3
"""Belt candidate Docker compatibility rehearsal on an isolated RESTORED snapshot.

Runs the reviewed development HUB against a disposable COPY of Belt's six-router
snapshot. This does NOT start a second reachable HUB, contact live routers,
modify host services/Xray, or perform a production cutover.

The original source-exact rehearsal must succeed first. Do not push private
snapshot data, image state, auth files or UUIDs to GitHub or a registry.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import orchestrator_docker_rehearsal_restore as restore
import orchestrator_belt_docker_migration_gate as gate
import orchestrator_docker_shadow_runner as shadow_runner

REPO = Path(__file__).resolve().parents[1]
DEFAULT_BACKUPS = Path("/opt/router-orchestrator/backups")
UID = 23088
STATIC_EXTENSIONS = {".png", ".ico", ".svg", ".webp", ".css", ".js", ".html", ".txt"}
MAX_STATIC_BYTES = 64 * 1024 * 1024
PRESERVE_SECRETS = ("agent.token", "hub-auth.json", "hub-session.token",
                    "hub-vapid-private.pem")
HEALTH = ("import json,urllib.request;"
          "r=urllib.request.urlopen('http://127.0.0.1:8088/health',timeout=3);"
          "assert json.load(r).get('ok') is True")


class Rejected(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Rejected(message)


def run_cmd(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=REPO, text=True, capture_output=True,
                          timeout=timeout, check=False)


def verify_checkout(expected_sha: str) -> str:
    require(re.fullmatch(r"[0-9a-f]{40}", expected_sha) is not None,
            "an explicit 40-character Git commit SHA is required")
    current = run_cmd("git", "rev-parse", "HEAD")
    require(current.returncode == 0 and current.stdout.strip() == expected_sha,
            "Git checkout differs from the reviewed commit")
    dirty = run_cmd("git", "status", "--porcelain", "--untracked-files=all")
    require(dirty.returncode == 0 and not dirty.stdout.strip(),
            "working tree contains unreviewed or untracked files")
    return current.stdout.strip()


def verify_private_parent(path: Path) -> None:
    require(path.is_dir() and not path.is_symlink(), "private backup root missing")
    st = path.stat()
    require(st.st_uid == os.geteuid() and
            stat.S_IMODE(st.st_mode) & 0o077 == 0,
            "backup root must be owner-only and owned by current user")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def schema_digest(db_path: Path) -> str:
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        records = db.execute(
            "select type,name,tbl_name,sql from sqlite_master "
            "where name not like 'sqlite_%' order by type,name"
        ).fetchall()
    return hashlib.sha256(json.dumps(records).encode()).hexdigest()


def check_portability(before: dict, after: dict, old_ids: list, new_ids: list,
                      old_schema: str, new_schema: str,
                      old_secrets: dict, state: Path) -> dict:
    require(after["active_count"] == before["active_count"] and
            after["tcp"] == before["tcp"] and after["wss"] == before["wss"],
            "candidate changed active router transport inventory")
    require(after["tables"].get("routers") == before["tables"].get("routers") and
            after["tables"].get("router_logs") == before["tables"].get("router_logs"),
            "candidate altered router/history row counts")
    require(old_ids == new_ids,
            "candidate changed router IDs, reverse ports or VLESS identities")
    require(all((state / name).is_file() and digest(state / name) == value
                for name, value in old_secrets.items()),
            "candidate modified a long-lived auth/VAPID/token secret")
    return {"routers": after["active_count"], "wss": after["wss"],
            "tcp": after["tcp"], "router_logs": after["tables"].get("router_logs", 0),
            "schema_unchanged": old_schema == new_schema,
            "protected_secrets": len(old_secrets)}


def transfer_state_ownership(folder: Path) -> None:
    """Only an already verified disposable restore is writable by container UID."""
    require(folder.is_dir() and not folder.is_symlink() and
            folder.name.startswith("orchestrator-shadow-state-"),
            "not a disposable shadow state")
    state = folder / "state"
    require(state.is_dir() and not state.is_symlink(),
            "restored state missing")
    for root, dirs, files in os.walk(state, topdown=True, followlinks=False):
        parent = Path(root)
        require(not parent.is_symlink(), "unsafe state directory")
        for name in dirs:
            p = parent / name
            require(p.is_dir() and not p.is_symlink(), "state symlink/special directory")
        for name in files:
            p = parent / name
            require(p.is_file() and not p.is_symlink(), "state symlink/special file")
            os.chown(p, UID, UID)
            os.chmod(p, 0o600)
        os.chown(parent, UID, UID)
        os.chmod(parent, 0o700)


def candidate_context(destination_parent: Path) -> tuple[Path, str]:
    """Strict minimal Docker build context: no state or credentials."""
    original = REPO / "vps/owrt-remote-hub.py"
    dockerfile = REPO / "docker/hub/Dockerfile"
    lock = REPO / "docker/hub/requirements.lock"
    static = REPO / "vps/static"
    for p in (original, dockerfile, lock):
        require(p.is_file() and not p.is_symlink(), "missing/unsafe reviewed source")
    require(static.is_dir() and not static.is_symlink(), "static assets missing")
    context = Path(tempfile.mkdtemp(prefix="orchestrator-candidate-context-",
                                   dir=destination_parent))
    context.chmod(0o700)
    try:
        for relative in ("vps/owrt-remote-hub.py", "docker/hub/Dockerfile",
                         "docker/hub/requirements.lock"):
            target = context / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / relative, target)
        total = 0
        files = 0
        for root, dirs, names in os.walk(static, topdown=True, followlinks=False):
            for d in dirs:
                p = Path(root) / d
                require(p.is_dir() and not p.is_symlink(), "unsafe static directory")
            for name in names:
                src = Path(root) / name
                require(src.is_file() and not src.is_symlink() and
                        src.suffix.lower() in STATIC_EXTENSIONS,
                        "unsafe static asset")
                total += src.stat().st_size
                require(total <= MAX_STATIC_BYTES, "static assets exceed limit")
                target = context / "vps/static" / src.relative_to(static)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
                files += 1
        require(files > 0, "static assets empty")
        expected = digest(original)
        require(digest(context / "vps/owrt-remote-hub.py") == expected,
                "source changed during staging")
        return context, expected
    except BaseException:
        shutil.rmtree(context)
        raise


def rehearsal(snapshot: Path, backup_root: Path, expected_commit: str,
              attempts: int = 25) -> dict:
    require(os.geteuid() == 0, "run with sudo on Belt")
    verify_private_parent(backup_root)
    verify_checkout(expected_commit)
    require(snapshot.is_dir() and not snapshot.is_symlink() and
            snapshot.resolve().parent == backup_root.resolve(),
            "snapshot must be a direct child of the private backup root")
    manifest = gate.inspect_archive(snapshot)
    require(manifest["kind"] == "REHEARSAL_ONLY_NOT_CUTOVER",
            "snapshot is not a verified offline rehearsal")
    require(run_cmd("systemctl", "is-active", "--quiet", "owrt-remote.service").returncode == 0,
            "host HUB not active; refuse rehearsal")
    require(run_cmd("systemctl", "is-active", "--quiet", "owrt-remote-xray.service").returncode == 0,
            "host Xray not active; refuse rehearsal")

    stage = None
    context = None
    image = ""
    container = ""
    try:
        stage, report = restore.restore(snapshot, backup_root)
        dbpath = stage / "state/hub.db"
        prior = gate.db_inventory(dbpath)
        require(prior["active_count"] == report["routers"],
                "restored router inventory differs from verified snapshot")
        before_ids = shadow_runner.router_credentials(stage / "state")
        before_schema = schema_digest(dbpath)
        secrets = {name: digest(stage / "state" / name)
                   for name in PRESERVE_SECRETS
                   if (stage / "state" / name).is_file()}
        require("agent.token" in secrets and "hub-auth.json" in secrets,
                "restored auth secrets missing")
        context, app_sha = candidate_context(backup_root)
        transfer_state_ownership(stage)

        image = "router-orchestrator-candidate-local:" + app_sha[:12] + "-" + uuid.uuid4().hex[:8]
        build = run_cmd("docker", "build", "--quiet",
                        "--file", str(context / "docker/hub/Dockerfile"),
                        "--build-arg", "HUB_SOURCE_SHA256=" + app_sha,
                        "--tag", image, str(context), timeout=420)
        require(build.returncode == 0, "candidate image build failed")
        name = "router-orchestrator-candidate-" + uuid.uuid4().hex[:10]
        cmd = [
            "docker", "run", "--detach", "--rm", "--name", name,
            "--user", f"{UID}:{UID}", "--network", "none",
            "--log-driver", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "128", "--memory", "384m",
            "--mount", "type=bind,src=" + str((stage / "state").resolve()) +
                       ",dst=/var/lib/owrt-remote",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "--tmpfs", "/run:rw,nosuid,nodev,size=8m",
            "--env", "OWRT_REMOTE_RUNTIME=docker",
            "--env", "OWRT_REMOTE_XRAY_CONTROL_BACKEND=external_readonly",
            "--env", "OWRT_REMOTE_RESTART_XRAY_ON_WAN_RECONNECT=0",
            "--env", "OWRT_REMOTE_STATE_DIR=/var/lib/owrt-remote",
            "--env", "OWRT_REMOTE_BIND=127.0.0.1",
            "--env", "OWRT_REMOTE_PORT=8088",
            image,
        ]
        started = run_cmd(*cmd, timeout=45)
        require(started.returncode == 0, "networkless candidate container failed")
        container = started.stdout.strip()
        require(re.fullmatch(r"[0-9a-f]{64}", container) is not None,
                "invalid candidate container ID")
        healthy = False
        for _ in range(attempts):
            probe = run_cmd("docker", "exec", container, "python", "-c", HEALTH, timeout=9)
            if probe.returncode == 0:
                healthy = True
                break
            status = run_cmd("docker", "inspect", "-f", "{{.State.Running}}",
                             container, timeout=8)
            require(status.returncode == 0 and status.stdout.strip() == "true",
                    "candidate container exited or lost state")
            time.sleep(1)
        require(healthy, "candidate /health timed out")
        after = gate.db_inventory(dbpath)
        result = check_portability(
            prior, after, before_ids,
            shadow_runner.router_credentials(stage / "state"),
            before_schema, schema_digest(dbpath),
            secrets, stage / "state")
        require(run_cmd("systemctl", "is-active", "--quiet",
                        "owrt-remote.service").returncode == 0,
                "production HUB lost active status")
        return result
    finally:
        if container:
            run_cmd("docker", "rm", "--force", container, timeout=25)
        if image:
            run_cmd("docker", "image", "rm", image, timeout=30)
        for p, prefix in ((context, "orchestrator-candidate-context-"),
                          (stage, "orchestrator-shadow-state-")):
            if p is not None:
                require(p.parent.resolve() == backup_root.resolve() and
                        p.name.startswith(prefix) and p.is_dir() and not p.is_symlink(),
                        "refusing unsafe disposable context cleanup")
                shutil.rmtree(p)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--expect-commit", required=True)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUPS)
    args = parser.parse_args()
    try:
        result = rehearsal(args.snapshot, args.backup_root, args.expect_commit)
        print("BELT CANDIDATE DOCKER: PASS")
        print("FLEET: {routers} routers | WSS={wss} TCP={tcp}".format(**result))
        print("HISTORY: router_logs=" + str(result["router_logs"]))
        print("AUTH: long-lived secret files preserved")
        print("SCHEMA:", "UNCHANGED" if result["schema_unchanged"] else
              "CHANGED — old HUB rollback compatibility NOT verified")
        print("NETWORK: NONE | no host ports or live state mounted")
        print("CLEANUP: temporary container, build context and restored state removed")
        print("NO CUTOVER: host HUB and Xray remain in service")
        return 0 if result["schema_unchanged"] else 3
    except (Rejected, gate.GateError, restore.Blocked,
            shadow_runner.PilotError, ValueError, OSError,
            sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print("BELT CANDIDATE DOCKER: BLOCKED — " + type(exc).__name__,
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
