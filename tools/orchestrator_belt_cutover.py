#!/usr/bin/env python3
"""Single-writer Belt HUB-only Docker cutover coordinator.

The default 'preflight' is READ ONLY and cannot stop services.
'prepare' builds a pinned local candidate image, leaving production untouched.
'activate' requires TWO explicit operator acknowledgements and performs a
controlled single-writer switch, with best-effort immediate host recovery.
'rollback' preserves the LATEST Docker state before restarting the host HUB.

Host Xray, Nginx, TLS and router configurations are NEVER modified by this tool.
Requires an independent, successful candidate compatibility rehearsal.
Do not use for Xray migration or a fleet-wide router update.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, closing
import datetime
import fcntl
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
import urllib.error
import urllib.request

import orchestrator_belt_docker_migration_gate as gate
import orchestrator_docker_candidate_rehearsal as candidate

ROOT = Path("/opt/router-orchestrator")
REPO = ROOT / "release"
STATE = Path("/var/lib/owrt-remote")
DATA = ROOT / "data"
BACKUPS = ROOT / "backups"
ENV = REPO / ".env"
JOURNAL = ROOT / ".cutover.json"
LOCK = ROOT / ".deploy.lock"
XRAY = Path("/etc/xray/owrt-remote.json")
HOST_SERVICE = "owrt-remote.service"
XRAY_SERVICE = "owrt-remote-xray.service"
DOCKER_NAME = "router-orchestrator-hub"
PORT = 8088
UID = GID = 23088
TRANSIENT = {"hub.db-wal", "hub.db-shm", "hub.db-journal"}
ALL_REQUIRED = {"hub.db", "agent.token", "hub-auth.json", ".xray-maintenance-hold"}


class Blocked(RuntimeError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise Blocked(message)


def command(*args: str, timeout: int = 45) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          cwd=REPO, check=False)


def command_ok(*args: str, timeout: int = 45) -> None:
    result = command(*args, timeout=timeout)
    require(result.returncode == 0, "command failed: " + " ".join(args[:3]))


def service_active(service: str) -> bool:
    return command("systemctl", "is-active", "--quiet", service).returncode == 0


def docker_running() -> bool:
    result = command("docker", "inspect", "-f", "{{.State.Running}}", DOCKER_NAME)
    return result.returncode == 0 and result.stdout.strip() == "true"


def file_sha(path: Path) -> str:
    return gate.file_sha(path)


def timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def require_root() -> None:
    require(os.geteuid() == 0, "this operation requires root")


@contextmanager
def exclusive_lock():
    require_root()
    require(ROOT.is_dir() and not ROOT.is_symlink(), "Router Orchestrator root missing")
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked("deployment lock already held") from exc
        yield
    finally:
        os.close(fd)


def secure_dir(path: Path) -> None:
    require(path.is_dir() and not path.is_symlink(), "private directory missing")
    st = path.stat()
    require(st.st_uid == 0 and stat.S_IMODE(st.st_mode) & 0o077 == 0,
            "private directory is not root-only")


def atomic_json(path: Path, document: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=".cutover-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(document, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_journal() -> dict:
    require(JOURNAL.is_file() and not JOURNAL.is_symlink(), "no valid cutover journal")
    document = json.loads(JOURNAL.read_text())
    require(isinstance(document, dict) and document.get("schema") == 1,
            "unrecognized cutover journal")
    return document


def mark(document: dict, state: str) -> None:
    document["phase"] = state
    document["updated_at"] = timestamp()
    atomic_json(JOURNAL, document)


def identity_digest(inventory: dict) -> str:
    rows = [{key: row[key] for key in
             ("id", "vless_uuid", "ssh_vless_uuid", "vps_transport",
              "vless_port", "entry_port", "ssh_entry_port",
              "vps_allow_legacy", "deleted_at")}
            for row in inventory["routers"]]
    raw = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def check_url(public_url: str) -> None:
    require(re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]{2,5})?", public_url)
            is not None, "require explicit HTTPS HUB public URL")


def source_checkout(expected_commit: str) -> None:
    require(REPO.is_dir() and not REPO.is_symlink(), "release checkout absent")
    candidate.verify_checkout(expected_commit)


def basic_preflight(expected_commit: str, public_url: str,
                    *, first_time: bool) -> dict:
    require_root()
    check_url(public_url)
    source_checkout(expected_commit)
    secure_dir(BACKUPS)
    # Rollback uses an atomic rename of the old host state into BACKUPS.
    # Detect cross-filesystem layouts BEFORE stopping the live service.
    require(BACKUPS.stat().st_dev == STATE.parent.stat().st_dev,
            "host state and backups must share filesystem for safe rollback")
    require(service_active(HOST_SERVICE), "production HUB not active")
    require(service_active(XRAY_SERVICE), "host Xray not active")
    require(command("docker", "info", timeout=10).returncode == 0,
            "Docker daemon unavailable; cannot verify single-writer isolation")
    require(not docker_running(), "Docker production HUB already running")
    if first_time:
        require(not JOURNAL.exists() and not JOURNAL.is_symlink(),
                "cutover journal already exists; investigate before proceeding")
        require(not DATA.exists() and not DATA.is_symlink(),
                "Docker data directory already exists")
        require(not ENV.exists() and not ENV.is_symlink(),
                ".env already exists; refuse to overwrite")
    require(STATE.is_dir() and not STATE.is_symlink(), "host state absent/unsafe")
    require(XRAY.is_file() and not XRAY.is_symlink(), "host Xray config absent/unsafe")
    info, _ = gate.analyze(STATE, XRAY)
    require(ALL_REQUIRED <= {p.name for p in STATE.iterdir() if p.is_file()},
            "production state required files missing")
    gate.record_state_files(STATE)
    require(shutil.disk_usage(ROOT).free >= 512 * 1024 * 1024,
            "insufficient free space for private state copy")
    return {"fleet": info["active_count"], "wss": info["wss"],
            "tcp": info["tcp"], "router_hash": identity_digest(info),
            "xray_hash": file_sha(XRAY),
            "old_hub_source_hash": file_sha(Path("/opt/owrt-remote/owrt-remote-hub.py"))}


def expected_image(expected_commit: str) -> str:
    return "router-orchestrator-hub:" + expected_commit[:12]


def verify_image(image: str) -> None:
    require(re.fullmatch(r"router-orchestrator-hub:[0-9a-f]{12}", image) is not None,
            "unsafe candidate image")
    command_ok("docker", "image", "inspect", image)


def build_image(expected_commit: str) -> str:
    image = expected_image(expected_commit)
    source = REPO / "vps/owrt-remote-hub.py"
    require(source.is_file() and not source.is_symlink(), "reviewed HUB source missing")
    command_ok("docker", "build", "--file", "docker/hub/Dockerfile",
               "--build-arg", "HUB_SOURCE_SHA256=" + file_sha(source),
               "--tag", image, ".", timeout=500)
    verify_image(image)
    return image


def write_env(document: dict) -> None:
    require(not ENV.exists() and not ENV.is_symlink(), "refusing to overwrite .env")
    fields = {
        "RO_IMAGE": document["image"],
        "RO_STATE_DIR": str(DATA),
        "RO_PORT": str(PORT),
        "RO_PUBLIC_URL": document["public_url"],
        "RO_XRAY_BACKEND": "external_readonly",
    }
    fd = os.open(ENV, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        for k, v in sorted(fields.items()):
            stream.write(k + "=" + v + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def copy_complete_state(source: Path, target: Path, owner: int) -> dict:
    """Copy a QUIESCENT state tree; SQLite through Backup API, never raw WAL.

    Validates every source file hash, router identities, all SQL table counts,
    and existence of required state before exposing the copy to an application.
    """
    require(source.is_dir() and not source.is_symlink(), "unsafe source state")
    require(not target.exists() and not target.is_symlink(),
            "state target already exists")
    original, _ = gate.analyze(source, XRAY)
    recorded = gate.record_state_files(source)
    target.mkdir(mode=0o700)
    try:
        for directory, dirs, files in os.walk(source, followlinks=False):
            rel = Path(directory).relative_to(source)
            out = target / rel
            out.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chown(out, owner, owner)
            os.chmod(out, 0o700)
            for item in dirs:
                location = Path(directory) / item
                require(location.is_dir() and not location.is_symlink(),
                        "state directory symlink/special file")
        for item, archive_path in recorded:
            rel = Path(archive_path).relative_to("state")
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, dst, follow_symlinks=False)
            require(file_sha(dst) == file_sha(item),
                    "state member changed during copy")
            os.chown(dst, owner, owner)
            os.chmod(dst, 0o600)
        with closing(sqlite3.connect(str(source / "hub.db"))) as src:
            with closing(sqlite3.connect(str(target / "hub.db"))) as dst:
                src.backup(dst)
        os.chown(target / "hub.db", owner, owner)
        os.chmod(target / "hub.db", 0o600)
        fresh = gate.db_inventory(target / "hub.db")
        require(fresh["tables"] == original["tables"] and
                identity_digest(fresh) == identity_digest(original),
                "state SQLite/historical/router parity failed")
        expected_files = {Path(name).relative_to("state").as_posix()
                          for _, name in recorded} | {"hub.db"}
        actual_files = {p.relative_to(target).as_posix()
                        for p in target.rglob("*") if p.is_file()}
        require(expected_files == actual_files, "state files incomplete")
        return {"fleet": fresh["active_count"], "wss": fresh["wss"],
                "tcp": fresh["tcp"], "tables": len(fresh["tables"])}
    except BaseException:
        shutil.rmtree(target)
        raise


def stage_state(source: Path, destination: Path, owner: int) -> dict:
    # Both targets are in /opt/router-orchestrator except rollback recovery.
    tmp = Path(tempfile.mkdtemp(prefix=".state-stage-", dir=destination.parent))
    tmp.rmdir()
    try:
        result = copy_complete_state(source, tmp, owner)
        require(not destination.exists(), "destination appeared concurrently")
        os.replace(tmp, destination)
        return result
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def check_health() -> None:
    for _ in range(20):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/health", timeout=2) as response:
                if json.load(response).get("ok") is True and docker_running():
                    return
        except (OSError, ValueError, urllib.error.HTTPError):
            pass
        time.sleep(1)
    raise Blocked("Docker HUB failed /health; recovery required")


def original_service_enabled() -> bool:
    return command("systemctl", "is-enabled", "--quiet", HOST_SERVICE).returncode == 0


def restart_host(document: dict) -> None:
    if document.get("was_enabled"):
        command_ok("systemctl", "enable", "--now", HOST_SERVICE)
    else:
        command_ok("systemctl", "start", HOST_SERVICE)
    require(service_active(HOST_SERVICE), "host HUB failed to restart")


def stop_docker() -> None:
    """Fence an existing production container even if .env was already moved.

    Docker must be reachable: treating an unavailable daemon as a stopped
    container risks activating the old HUB alongside a restarted container.
    """
    require(command("docker", "info", timeout=10).returncode == 0,
            "Docker daemon unavailable; cannot fence writer")
    probe = command("docker", "inspect", DOCKER_NAME)
    if probe.returncode == 0:
        label = command(
            "docker", "inspect", "-f",
            '{{index .Config.Labels "com.docker.compose.project"}}/'
            '{{index .Config.Labels "com.docker.compose.service"}}',
            DOCKER_NAME)
        require(label.returncode == 0 and
                label.stdout.strip() == "router-orchestrator/hub",
                "named Docker container is not the expected HUB")
        # Disable automatic restarts BEFORE stopping; retained across daemon
        # restarts during a prolonged recovery or host reboot.
        command_ok("docker", "update", "--restart=no", DOCKER_NAME)
        command_ok("docker", "stop", "-t", "30", DOCKER_NAME, timeout=75)
    require(not docker_running(), "Docker HUB still running")


def seal_env(document: dict) -> None:
    archived = BACKUPS / ("docker-env-" + document["id"] + ".env")
    require(not (ENV.exists() and archived.exists()),
            "both active and archived Docker env exist")
    if ENV.exists():
        require(not ENV.is_symlink(), "unsafe Docker environment file")
        os.replace(ENV, archived)
        os.chmod(archived, 0o600)


def _sql_digest(path: Path) -> str:
    """Compare SQL content across SQLite backup copies, not page layout."""
    h = hashlib.sha256()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        for statement in db.iterdump():
            h.update(statement.encode("utf-8"))
            h.update(b"\n")
    return h.hexdigest()


def state_equivalent(first: Path, second: Path) -> bool:
    """Conservative repeat-recovery check after an atomic state promotion."""
    try:
        a = gate.db_inventory(first / "hub.db")
        b = gate.db_inventory(second / "hub.db")
        if (a["tables"] != b["tables"] or identity_digest(a) != identity_digest(b)
                or _sql_digest(first / "hub.db") != _sql_digest(second / "hub.db")):
            return False
        a_files = {Path(name).relative_to("state").as_posix(): file_sha(path)
                   for path, name in gate.record_state_files(first)}
        b_files = {Path(name).relative_to("state").as_posix(): file_sha(path)
                   for path, name in gate.record_state_files(second)}
        return a_files == b_files
    except (OSError, ValueError, sqlite3.Error, gate.GateError):
        return False


def promote_latest_state(document: dict) -> None:
    """Idempotent host-state promotion after container is safely fenced.

    Handles power loss after moving the old host state into BACKUPS but before
    moving a staged data copy into STATE. Never rewinds to a stale snapshot.
    """
    require(not docker_running(), "Docker HUB must be stopped before promoting state")
    require(DATA.is_dir() and not DATA.is_symlink(), "latest Docker state missing")
    require(BACKUPS.stat().st_dev == STATE.parent.stat().st_dev,
            "backup and host state must share filesystem for atomic rollback")
    archived = BACKUPS / ("pre-docker-host-state-" + document["id"])
    require(not archived.is_symlink() and not STATE.is_symlink(),
            "unsafe host state directory")

    if archived.exists():
        require(archived.is_dir(), "original host state archive invalid")
        if STATE.exists():
            require(STATE.is_dir() and state_equivalent(STATE, DATA),
                    "promoted host state differs from latest Docker data")
            return  # Previous attempt already promoted the correct latest state.
        # Crash after archiving the old STATE but before installing new STATE.
        # Rebuild from the immutable, stopped Docker data instead of trusting
        # a half-written temporary directory.
        stage_state(DATA, STATE, 0)
        return

    require(STATE.is_dir(), "host state missing before promotion")
    # Random staging names avoid colliding with interrupted prior attempts;
    # any orphan directory requires an operator cleanup, not blind deletion.
    import secrets
    staging = STATE.parent / (".owrt-restore-" + document["id"] +
                              "-" + secrets.token_hex(8))
    stage_state(DATA, staging, 0)
    os.replace(STATE, archived)
    # If interrupted here, a repeat recovery sees archived and regenerates
    # a complete STATE from DATA. Never restart systemd while STATE is missing.
    os.replace(staging, STATE)


def recover_host(document: dict) -> None:
    """Repeatable, fenced recovery with progress persisted after each gate."""
    require(not service_active(HOST_SERVICE),
            "host service unexpectedly running during recovery")
    stop_docker()
    mark(document, "DOCKER_FENCED")
    if DATA.exists():
        require(DATA.is_dir() and not DATA.is_symlink(),
                "Docker data not a safe directory")
        promote_latest_state(document)
    else:
        require(STATE.is_dir() and not STATE.is_symlink(),
                "neither host state nor Docker data is available")
    mark(document, "STATE_RECOVERED")
    seal_env(document)
    mark(document, "ENV_SEALED")
    require(not docker_running(), "Docker reappeared during recovery")
    mark(document, "HOST_RESTART_REQUESTED")
    restart_host(document)


RECOVERABLE_PHASES = frozenset({
    "STOP_REQUESTED", "HOST_STOPPED", "STATE_COPIED",
    "DOCKER_START_REQUESTED", "RECOVERING", "RECOVERY_FAILED",
    "ROLLBACK_STARTED", "DOCKER_FENCED", "STATE_RECOVERED",
    "ENV_SEALED", "HOST_RESTART_REQUESTED",
})


def recover(args) -> None:
    """Explicit rescue after SIGKILL/host reboot in an incomplete transition.

    Never silently re-enables the old HUB if the Docker HUB may be writing.
    """
    require(args.confirm, "recover requires explicit --confirm")
    with exclusive_lock():
        doc = load_journal()
        phase = doc["phase"]
        require(phase in RECOVERABLE_PHASES,
                "journal is not an incomplete recoverable transition")
        host = service_active(HOST_SERVICE)
        docker = docker_running()
        require(not (host and docker), "two active HUB writers: operator intervention")
        if host:
            # Crashed after host restart but before journal completion.
            require(not ENV.exists() and not docker,
                    "cannot mark recovery complete while Docker remains armed")
            archive = BACKUPS / ("pre-docker-host-state-" + doc["id"])
            require(not DATA.exists() or
                    (archive.is_dir() and STATE.is_dir() and
                     state_equivalent(STATE, DATA)),
                    "host active before latest Docker data was promoted")
        else:
            mark(doc, "RECOVERING")
            recover_host(doc)
        mark(doc, "ROLLED_BACK" if phase == "ROLLBACK_STARTED" else
             "AUTO_ROLLED_BACK")
        print("RECOVERY PASS: host HUB active, Docker HUB fenced")
        print("Latest retained Docker state was promoted when present")
        print("Host Xray/Nginx were not restarted")



def preflight(args) -> None:
    current = basic_preflight(args.expect_commit, args.public_url, first_time=True)
    print("CUTOVER PREFLIGHT: PASS")
    print(f"FLEET: {current['fleet']} | WSS={current['wss']} TCP={current['tcp']}")
    print("HOST: systemd HUB, dedicated Xray healthy; all state members reviewed")
    print("DOCKER: no competing production container or state")
    print("CHANGE: none; this command does not stop or install anything")


def prepare(args) -> None:
    with exclusive_lock():
        current = basic_preflight(args.expect_commit, args.public_url,
                                  first_time=True)
        image = build_image(args.expect_commit)
        document = {
            "schema": 1, "id": timestamp(), "phase": "PREPARED",
            "commit": args.expect_commit, "image": image,
            "public_url": args.public_url,
            "router_hash": current["router_hash"],
            "xray_hash": current["xray_hash"],
            "original_hub_source_hash": current["old_hub_source_hash"],
            "was_enabled": original_service_enabled(),
            "timestamp": timestamp(),
        }
        atomic_json(JOURNAL, document)
        print("CUTOVER PREPARED: local candidate image built; NO service changes")
        print("IMAGE:", image)
        print("Next: reviewed and supervised --activate only after backup/recovery gate")


def activate(args) -> None:
    require(args.acknowledge_live_cutover and args.acknowledge_candidate_pass,
            "live cutover requires BOTH explicit acknowledgements")
    with exclusive_lock():
        doc = load_journal()
        require(doc["phase"] == "PREPARED", "cutover journal not in PREPARED state")
        original = basic_preflight(doc["commit"], doc["public_url"],
                                   first_time=False)
        require(original["router_hash"] == doc["router_hash"] and
                original["xray_hash"] == doc["xray_hash"] and
                original["old_hub_source_hash"] == doc["original_hub_source_hash"],
                "fleet/source/Xray changed since prepare; re-review required")
        require(not DATA.exists() and not ENV.exists(),
                "existing data or .env would be overwritten")
        verify_image(doc["image"])
        # Fresh safety archive before disabling the live service. Separate from
        # the eventual consistency-guaranteed copy after stop.
        backup, _ = gate.snapshot(
            STATE, XRAY, Path("/opt/owrt-remote/owrt-remote-hub.py"), BACKUPS)
        gate.inspect_archive(backup)
        doc["safety_snapshot"] = str(backup)
        mark(doc, "STOP_REQUESTED")
        try:
            command_ok("systemctl", "disable", "--now", HOST_SERVICE, timeout=70)
            require(not service_active(HOST_SERVICE),
                    "old HUB failed to stop")
            mark(doc, "HOST_STOPPED")
            stage_state(STATE, DATA, UID)
            mark(doc, "STATE_COPIED")
            write_env(doc)
            mark(doc, "DOCKER_START_REQUESTED")
            command_ok("docker", "compose", "up", "-d", "--no-build", "hub", timeout=90)
            check_health()
            require(file_sha(XRAY) == doc["xray_hash"] and
                    service_active(XRAY_SERVICE),
                    "host Xray changed during HUB cutover")
            mark(doc, "DOCKER_ACTIVE")
            print("CUTOVER: DOCKER HUB ACTIVE")
            print("Host Xray, Nginx and router tunnels were not restarted")
            print("Now verify HTTPS, every reachable WSS router LuCI/SSH and heartbeats")
            print("Rollback: run this tool with rollback --confirm")
        except BaseException:
            mark(doc, "RECOVERING")
            try:
                recover_host(doc)
                mark(doc, "AUTO_ROLLED_BACK")
                print("AUTO-ROLLBACK: host HUB restored with latest state",
                      file=sys.stderr)
            except BaseException:
                mark(doc, "RECOVERY_FAILED")
                print("CRITICAL: automatic recovery failed. Do not start a second HUB.",
                      file=sys.stderr)
            raise


def rollback(args) -> None:
    require(args.confirm, "explicit --confirm required for live rollback")
    with exclusive_lock():
        doc = load_journal()
        require(doc["phase"] == "DOCKER_ACTIVE",
                "only a completed Docker cutover can roll back")
        require(not service_active(HOST_SERVICE), "old HUB already active")
        require(docker_running(), "Docker HUB not running: manual recovery needed")
        mark(doc, "ROLLBACK_STARTED")
        try:
            recover_host(doc)
            mark(doc, "ROLLED_BACK")
            print("ROLLBACK: host systemd HUB restored with LATEST Docker state")
            print("Original host state preserved in private backups")
            print("Host Xray and Nginx unchanged")
        except BaseException:
            mark(doc, "RECOVERY_FAILED")
            raise


def status() -> None:
    doc = load_journal() if JOURNAL.exists() else {"phase": "NOT_PREPARED"}
    print("CUTOVER PHASE:", doc["phase"])
    print("Host HUB:", "active" if service_active(HOST_SERVICE) else "inactive")
    print("Docker HUB:", "running" if docker_running() else "stopped")
    print("Host Xray:", "active" if service_active(XRAY_SERVICE) else "inactive")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    subs = p.add_subparsers(dest="mode", required=True)
    for verb in ("preflight", "prepare"):
        cmd = subs.add_parser(verb)
        cmd.add_argument("--expect-commit", required=True)
        # Read-only preflight does not persist the URL. No guesswork about
        # the actual production HTTPS hostname is necessary at this stage.
        cmd.add_argument("--public-url", required=(verb == "prepare"),
                         default="https://preflight.invalid")
    start = subs.add_parser("activate")
    start.add_argument("--acknowledge-live-cutover", action="store_true")
    start.add_argument("--acknowledge-candidate-pass", action="store_true")
    back = subs.add_parser("rollback")
    back.add_argument("--confirm", action="store_true")
    rescue = subs.add_parser("recover")
    rescue.add_argument("--confirm", action="store_true",
                        help="Recover an interrupted single-writer transaction")
    subs.add_parser("status")
    args = p.parse_args()
    try:
        if args.mode == "preflight":
            preflight(args)
        elif args.mode == "prepare":
            prepare(args)
        elif args.mode == "activate":
            activate(args)
        elif args.mode == "rollback":
            rollback(args)
        elif args.mode == "recover":
            recover(args)
        else:
            status()
        return 0
    except (Blocked, gate.GateError, candidate.Rejected, OSError, ValueError,
            KeyError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print("CUTOVER BLOCKED: " + type(exc).__name__ +
              (": " + str(exc)[:160] if isinstance(exc, Blocked) else ""),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
