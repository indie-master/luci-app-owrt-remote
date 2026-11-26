#!/usr/bin/env python3
"""Router Orchestrator Docker operator CLI.

This is an opt-in fresh-install / offline-backup CLI, NOT a live Belt cutover.
It deliberately blocks host HUB co-existence and implicit state restoration.
"""
from __future__ import annotations
import argparse
import contextlib
from contextvars import ContextVar
import ctypes
import datetime
import fcntl
import gzip
import hashlib
import http.client
import io
import itertools
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zlib

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
APP = ROOT / "vps" / "owrt-remote-hub.py"
HUB_DEPLOY_ROOT = Path("/opt/router-orchestrator")
LEGACY_HUB_STATE = HUB_DEPLOY_ROOT / "data"
LOCK = HUB_DEPLOY_ROOT / ".deploy.lock"
BACKUPS = HUB_DEPLOY_ROOT / "backups"
ALLOWED = {"RO_IMAGE", "RO_STATE_DIR", "RO_PORT", "RO_PUBLIC_URL", "RO_XRAY_BACKEND"}
MAX_ARCHIVE = 2 * 1024**3
MAX_DATA = 4 * 1024**3
MAX_FILES = 9999
MAX_MANIFEST = 4 * 1024**2
MAX_TAR_OVERHEAD = 64 * 1024**2
HUB_UID = 23088
HUB_GID = 23088
SOURCE_LABEL = "org.router-orchestrator.hub-source-sha256"
PROJECT = "router-orchestrator"
LOCK_FD = ContextVar("deployment_lock_fd", default=None)
SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
TRANSIENT = {"hub.db" + suffix for suffix in SQLITE_SUFFIXES}
STACK_PRIVATE_FILES = {"profiles.db", "xray.json", "xray-active.json", "nginx-active.conf",
                       "stack.env", "stack.json", "compose.yml", "compose-stack.yml"}
STACK_PRIVATE_DIRECTORIES = {"tls", "xray-assets"}


def stack_private_member(name):
    return name in STACK_PRIVATE_FILES or (
        name.split("/", 1)[0] in STACK_PRIVATE_DIRECTORIES and
        all(part not in {"", ".", ".."} for part in name.split("/")) and "/" in name)

class Blocked(Exception):
    pass

@contextlib.contextmanager
def deployment_lock():
    """Serialize installers, backups and release switches across processes.

    The lock is permanent metadata under the Router Orchestrator root.
    Never unlink a flock file while it may be held by another process.
    """
    require_root()
    require(LOCK.parent.is_dir() and not LOCK.parent.is_symlink(),
            "Deployment directory missing/unsafe")
    fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked("Another Router Orchestrator operation is running") from exc
        token = LOCK_FD.set(fd)
        try:
            yield
        finally:
            LOCK_FD.reset(token)
    finally:
        os.close(fd)

def require(condition, message):
    if not condition:
        raise Blocked(message)

def inherited_lock():
    fd = LOCK_FD.get()
    return () if fd is None else (fd,)


def call(*args, check=True, env=None, timeout=None):
    return subprocess.run(args, cwd=ROOT, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=inherited_lock(),
                          env=env, timeout=timeout)

def require_root():
    require(os.geteuid() == 0, "Run as root")

def settings():
    out = read_env(ENV, ALLOWED)
    validate_settings(out)
    return out


def read_env(path, allowed):
    require(path.is_file() and not path.is_symlink(), ".env missing/unsafe")
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        require("=" in line, "Invalid .env line")
        k, v = line.split("=", 1)
        require(k in allowed and k not in out, "Unknown/duplicate .env variable")
        out[k] = v
    return out

def validate_settings(out, *, unified=False):
    require(ALLOWED == out.keys(), "Incomplete or unexpected settings")
    require(all(not re.search(r"\s|['\"\x00\x60$]", value) for value in out.values()),
            "Unquoted/simple .env values required")
    require(out["RO_XRAY_BACKEND"] in ({"external_readonly", "unified_adapter"}
                                      if unified else {"external_readonly"}),
            "Only external_readonly is approved for this release")
    require(out["RO_PORT"].isdigit() and 1024 <= int(out["RO_PORT"]) <= 65535,
            "Invalid port")
    path = Path(out["RO_STATE_DIR"])
    require(path.is_absolute() and not path.resolve().is_relative_to("/var/lib/owrt-remote")
            and not path.is_symlink(), "Unsafe state path")
    require(re.fullmatch(r"router-orchestrator-hub:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", out["RO_IMAGE"]),
            "Unexpected image tag")

def save_env(data):
    require(not ENV.exists(), "Refusing to replace existing .env")
    body = "# Managed by docker/manager.py; secrets never committed.\n"
    body += "".join(f"{k}={data[k]}\n" for k in sorted(ALLOWED))
    durable_write(ENV, body, exclusive=True)


def status_service(*, timeout=None):
    result = call("systemctl", "list-units", "--all", "--plain", "--no-legend", "--no-pager",
                  "owrt-remote.service", check=False, timeout=timeout)
    units = result.stdout.splitlines()
    require(result.returncode == 0 and len(units) <= 1, "Cannot verify host HUB service state")
    if not units:
        return False
    fields = units[0].split(maxsplit=4)
    require(len(fields) >= 4 and fields[0] == "owrt-remote.service" and fields[2] in
            {"active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance"},
            "Cannot verify host HUB service state")
    return fields[2] not in {"inactive", "failed"}


def local_docker_env():
    return {key: value for key, value in os.environ.items() if key not in
            {"DOCKER_CONTEXT", "DOCKER_HOST", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}


def compose(*args, config=None, runtime_image=None, project=None, profile=None, local=False, timeout=120):
    # Only the validated manager configuration selects the Compose model and its inputs.
    if config is None:
        config = settings()
    if project is None:
        project = PROJECT
    parent_env = local_docker_env() if local else os.environ
    env = {key: value for key, value in parent_env.items() if not key.startswith(("COMPOSE_", "RO_"))}
    env.update(config, RO_STACK_NAME=project)
    if runtime_image is not None:
        require(re.fullmatch(r"sha256:[0-9a-f]{64}", runtime_image), "Invalid immutable HUB image ID")
        env["RO_HUB_RUNTIME_IMAGE"] = runtime_image
    return subprocess.run(
        ["docker", *(("--host", "unix:///var/run/docker.sock") if local else ()),
         "compose", "--file", str(ROOT / "docker-compose.yml"),
         "--project-name", project, "--env-file", "/dev/null",
         *(("--profile", profile) if profile else ()), *args],
        cwd=ROOT, env=env, check=True, text=True, pass_fds=inherited_lock(),
        timeout=timeout if local else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

def docker_hub_states():
    result = compose("ps", "--all", "--format", "{{.State}}", "hub")
    states = result.stdout.split()
    require(set(states) <= {"created", "restarting", "running", "removing", "paused", "exited", "dead"},
            "Cannot determine Docker HUB state")
    return states

def docker_running():
    return docker_hub_states() == ["running"]

def health(port, attempts=12, revision=None):
    endpoint = "/health?panel-update=1" if revision else "/health"
    for attempt in range(attempts):
        with contextlib.closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as conn:
            try:
                conn.request("GET", endpoint)
                resp = conn.getresponse()
                if resp.status == 200:
                    payload = json.loads(resp.read(4096))
                    if (isinstance(payload, dict) and payload.get("ok") is True and
                            (revision is None or payload.get("revision") == revision)):
                        return True
            except (OSError, ValueError, http.client.HTTPException):
                pass
        if attempt + 1 < attempts:
            time.sleep(1)
    return False

def git_sha():
    result = call("git", "rev-parse", "--verify", "HEAD")
    return result.stdout.strip()

def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_image():
    require(APP.is_file() and not APP.is_symlink(), "HUB source missing")
    require(not call("git", "--no-optional-locks", "status", "--porcelain").stdout.strip(),
            "Git tree is dirty: build only a reviewed committed revision")
    commit = git_sha()
    image = "router-orchestrator-hub:" + commit[:12]
    source_hash = file_hash(APP)
    subprocess.run(["docker", "build", "--file", "docker/hub/Dockerfile",
                    "--build-arg", "HUB_SOURCE_SHA256=" + source_hash,
                    "--tag", image, "."], cwd=ROOT, check=True, pass_fds=inherited_lock())
    return image

def free_port(port):
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

def install(args):
    require_root()
    require(not status_service(), "Existing owrt-remote.service active: fresh installer refuses cutover")
    require(not ENV.exists() and not ENV.is_symlink(), "An installation already exists; never overwrite .env")
    require(call("docker", "compose", "version").returncode == 0, "Docker Compose missing")
    default_port = 18089
    default_data = str(LEGACY_HUB_STATE)
    if args.non_interactive:
        require(args.public_url and args.port and args.state_dir,
                "Noninteractive mode requires --public-url, --port and --state-dir")
    url = args.public_url or input("Public HTTPS URL (e.g. https://hub.example.net): ").strip()
    port = int(args.port or input(f"Local HUB port [{default_port}]: ").strip() or default_port)
    state = Path(args.state_dir or input(f"State directory [{default_data}]: ").strip() or default_data)
    require(re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?", url) is not None,
            "Require public HTTPS URL without path")
    require(1024 <= port <= 65535 and free_port(port), "Port occupied/invalid")
    require_new_destination(state)
    require(not ROOT.resolve().is_relative_to(state.resolve()) and
            not state.resolve().is_relative_to(ROOT.resolve()), "Unsafe overlapping path")
    answer = "yes" if args.non_interactive else input("Create clean Docker HUB (no routers yet)? type YES: ")
    require(answer.lower() == "yes", "Cancelled")
    image = build_image()
    config = {"RO_IMAGE": image, "RO_STATE_DIR": str(state), "RO_PORT": str(port),
              "RO_PUBLIC_URL": url, "RO_XRAY_BACKEND": "external_readonly"}
    validate_settings(config)
    compose("config", "--quiet", config=config)
    require(observed_hub(config, set()) is None, "Compose project already has a HUB")
    tx = begin_transaction("install", config, image_id(image))
    finish_install(tx)
    print("PASS: clean HUB is listening on loopback port", port)
    print("Host Xray, Nginx and TLS are NOT configured by this installer")

def state_path():
    value = Path(settings()["RO_STATE_DIR"])
    require(value.is_dir() and not value.is_symlink(), "State missing/unsafe")
    return value

def require_new_destination(dest):
    base = HUB_DEPLOY_ROOT
    require(dest.is_absolute() and base in dest.parents and dest == dest.resolve()
            and not dest.exists(), f"Require a new directory under {base} without path aliases")


def create_private_parents(path):
    for parent in reversed(path.parents):
        if HUB_DEPLOY_ROOT in parent.parents and not parent.exists():
            parent.mkdir(mode=0o700)
            sync_directory(parent.parent)


class ArchiveWriter:
    def __init__(self, stream):
        self.stream = stream
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, data):
        require(self.size + len(data) <= MAX_ARCHIVE, "Compressed backup too large")
        written = self.stream.write(data)
        require(written == len(data), "Short archive write")
        self.size += written
        self.digest.update(data)
        return written


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


class BoundedTarReader:
    """Bound tar headers, seeks and padding as well as extracted file data."""

    def __init__(self, stream):
        self.stream = stream
        self.limit = MAX_DATA + MAX_TAR_OVERHEAD

    def tell(self):
        return self.stream.tell()

    def seek(self, offset):
        require(0 <= offset <= self.limit, "Uncompressed archive too large")
        return self.stream.seek(offset)

    def read(self, size):
        require(0 <= size <= MAX_MANIFEST + 1024, "Oversized archive read")
        require(size <= self.limit - self.tell(), "Uncompressed archive too large")
        return self.stream.read(size)

    def check_trailer(self):
        # tarfile stops at the first zero header. Consume padding and force
        # gzip to verify the CRC and size rather than accepting a truncated footer.
        padding = 0
        while chunk := self.read(min(tarfile.RECORDSIZE, self.limit - self.tell())):
            require(not any(chunk), "Unexpected data after tar end marker")
            padding += len(chunk)
            require(padding <= tarfile.RECORDSIZE, "Excess tar end padding")
        require(self.tell() < self.limit and padding >= 512, "Incomplete archive trailer")


def sqlite_read(path, *, immutable=False):
    require(path.is_file() and not path.is_symlink(), "SQLite database missing/unsafe")
    for suffix in SQLITE_SUFFIXES:
        sidecar = path.with_name(path.name + suffix)
        require(not sidecar.is_symlink() and (not sidecar.exists() or sidecar.is_file()),
                "Unsafe SQLite sidecar")
    query = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    return contextlib.closing(sqlite3.connect(path.as_uri() + query, uri=True, timeout=5))


def check_integrity(db):
    require(db.execute("PRAGMA integrity_check(1)").fetchone() == ("ok",),
            "SQLite integrity check failed")


def state_files(directory, depth=0):
    require(depth <= 128, "Backup directory nesting too deep")
    with os.scandir(directory) as entries:
        for entry in entries:
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                yield from state_files(path, depth + 1)
            else:
                require(entry.is_file(follow_symlinks=False), "Non-regular state member")
                yield path


def backup_members(state, snapshot):
    yield snapshot, "hub.db"
    for item in state_files(state):
        relative = item.relative_to(state).as_posix()
        if relative not in TRANSIENT and relative != "hub.db":
            require("\\" not in relative, "Unsafe backup filename")
            yield item, relative


def archive_file(tar, path, name, remaining):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), "Non-regular state member")
        require(before.st_size <= remaining, "Backup data too large")
        info = tarfile.TarInfo(name)
        info.mode = 0o600
        info.size = before.st_size
        reader = HashReader(stream)
        tar.addfile(info, reader)
        after = os.fstat(stream.fileno())
        require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                "State file changed during backup")
    return before.st_size, reader.digest.hexdigest()


def backup():
    require_root()
    require(not status_service(), "Host HUB active: refuse mixed-writer backup")
    require(set(docker_hub_states()) <= {"created", "exited"},
            "Stop Docker HUB before consistency backup")
    return backup_state(state_path(), BACKUPS)


def snapshot_sqlite(source, target):
    with sqlite_read(source) as src, contextlib.closing(sqlite3.connect(target)) as dst:
        src.execute("BEGIN")
        pages = src.execute("PRAGMA page_count").fetchone()[0]
        page_size = src.execute("PRAGMA page_size").fetchone()[0]
        require(pages * page_size <= MAX_DATA, "SQLite snapshot too large")
        src.backup(dst, pages=128)
        check_integrity(dst)


def backup_state(state, directory, *, stack_private=None):
    require(stack_private is None or all(stack_private_member(name) for name in stack_private),
            "Unexpected private stack backup member")
    require(directory == directory.resolve() and not directory.is_relative_to(state.resolve())
            and not state.resolve().is_relative_to(directory),
            "Backup directory overlaps state or has path aliases")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    sync_directory(directory.parent)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = directory / f"router-orchestrator-{stamp}-{uuid.uuid4().hex}.tar.gz"
    files = {}
    manifest = {"format": 1 if stack_private is None else 2,
                "files": files, "hub_source_sha256": file_hash(APP)}
    manifest_size = len(json.dumps(manifest, separators=(",", ":")))
    total = 0
    with tempfile.TemporaryDirectory(prefix=".router-backup-", dir=directory) as td:
        dbpath = Path(td) / "hub.db"
        snapshot_sqlite(state / "hub.db", dbpath)
        members = (("state/" + relative, item) for item, relative in backup_members(state, dbpath))
        private = dict(stack_private or {})
        if "profiles.db" in private:
            profile_snapshot = Path(td) / "profiles.db"
            snapshot_sqlite(private["profiles.db"], profile_snapshot)
            private["profiles.db"] = profile_snapshot
        if "xray.json" in private:
            from unified_xray_state import verify_profile_snapshot

            verify_profile_snapshot(private["xray.json"], Path(td) / "profiles.db", dbpath)
        pending = Path(td) / dest.name
        with pending.open("xb") as out:
            os.fchmod(out.fileno(), 0o600)
            limited = ArchiveWriter(out)
            with tarfile.open(fileobj=limited, mode="w|gz") as tar:
                for name, item in itertools.chain(members, (
                        ("private/" + name, item) for name, item in private.items())):
                    relative = name if stack_private is not None else name.removeprefix("state/")
                    require(len(files) < MAX_FILES, "Too many backup files")
                    manifest_size += len(json.dumps(relative)) + 67 + bool(files)
                    require(manifest_size <= MAX_MANIFEST, "Backup manifest/data too large")
                    size, files[relative] = archive_file(
                        tar, item, name, MAX_DATA - total - manifest_size)
                    total += size
                blob = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
                require(len(blob) <= MAX_MANIFEST and total + len(blob) <= MAX_DATA,
                        "Backup manifest/data too large")
                info = tarfile.TarInfo("manifest.json")
                info.mode = 0o600
                info.size = len(blob)
                tar.addfile(info, io.BytesIO(blob))
            out.flush()
            os.fsync(out.fileno())
        os.link(pending, dest)
        sync_directory(directory)
    print("BACKUP:", dest)
    print("SHA256:", limited.digest.hexdigest())
    return dest


def publish_restore(source, dest):
    """Atomically publish a verified tree without replacing an existing directory."""
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    at_fdcwd, rename_noreplace = -100, 1
    if libc.renameat2(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(dest),
                      rename_noreplace) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), dest)
    sync_directory(source.parent)
    sync_directory(dest.parent)


def restore(args):
    require_root()
    dest = Path(args.destination)
    require_new_destination(dest)
    archive = Path(args.archive)
    fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream, gzip.GzipFile(fileobj=stream) as compressed:
        metadata = os.fstat(stream.fileno())
        require(stat.S_ISREG(metadata.st_mode), "Archive must be a regular file")
        require(metadata.st_size <= MAX_ARCHIVE, "Backup unexpectedly large")
        reader = BoundedTarReader(compressed)
        with tarfile.open(fileobj=reader, mode="r:") as tar:
            restore_members(tar, reader, dest, stack_bundle=getattr(args, "stack_bundle", False))
    print("PASS: isolated restore:", dest)
    print("Never mount it over the live state without a separate cutover plan")


def restore_members(tar, reader, dest, *, stack_bundle=False):
    members = {}
    total_size = 0
    for member in tar:
        require(len(members) <= MAX_FILES, "Too many archive members")
        require(member.isfile() and 0 <= member.size, "Unsafe archive member")
        require(member.name not in members, "Duplicate archive member")
        total_size += member.size
        require(total_size <= MAX_DATA, "Extracted data too large")
        members[member.name] = member
    reader.check_trailer()
    require("manifest.json" in members and members["manifest.json"].size <= MAX_MANIFEST,
            "Missing or oversized manifest")
    manifest = json.load(tar.extractfile(members["manifest.json"]))
    version = 2 if stack_bundle else 1
    require(isinstance(manifest, dict) and manifest.get("format") == version,
            "Unsupported backup format for requested restore mode")
    expected = manifest.get("files")
    require(isinstance(expected, dict), "Invalid backup manifest")
    if not stack_bundle:
        expected = {"state/" + name: digest for name, digest in expected.items()}
    require("state/hub.db" in expected and members.keys() == {"manifest.json"} | expected.keys(),
            "Manifest/archive mismatch")
    require(all(all(p not in ("", ".", "..") for p in n.split("/"))
                and "\\" not in n and "\x00" not in n for n in expected),
            "Unsafe filename")
    for name in expected:
        scope, _, relative = name.partition("/")
        require((scope == "state" and relative and relative not in TRANSIENT) or
                (stack_bundle and scope == "private" and stack_private_member(relative)),
                "Unsafe backup scope")
    require(all(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                for digest in expected.values()), "Invalid file digest")
    create_private_parents(dest)
    with tempfile.TemporaryDirectory(prefix=".router-restore-", dir=dest.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        state = bundle / "state"
        state.mkdir(mode=0o700, parents=True)
        bundle.chmod(0o700)
        for member in members.values():
            if member.name == "manifest.json":
                continue
            target = bundle / member.name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            digest = hashlib.sha256()
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out, tar.extractfile(member) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
                    out.write(chunk)
                require(digest.hexdigest() == expected[member.name], "Backup digest mismatch")
                if target.is_relative_to(state):
                    os.chown(target, HUB_UID, HUB_GID)
                out.flush()
                os.fsync(out.fileno())
        # SQLite Backup API produced a self-contained snapshot. Immutable mode
        # keeps the check from creating WAL/SHM files in the verified tree.
        with sqlite_read(state / "hub.db", immutable=True) as db:
            check_integrity(db)
        for directory in sorted((p for p in state.rglob("*") if p.is_dir()), reverse=True):
            os.chown(directory, HUB_UID, HUB_GID)
            sync_directory(directory)
        os.chown(state, HUB_UID, HUB_GID)
        sync_directory(state)
        private = bundle / "private"
        if private.exists():
            profiles = private / "profiles.db"
            if profiles.exists():
                with sqlite_read(profiles, immutable=True) as db:
                    check_integrity(db)
            from unified_xray_state import verify_profile_snapshot

            verify_profile_snapshot(private / "xray.json", profiles, state / "hub.db")
            for directory in sorted((p for p in private.rglob("*") if p.is_dir()), reverse=True):
                sync_directory(directory)
            sync_directory(private)
        sync_directory(bundle)
        publish_restore(bundle if stack_bundle else state, dest)

RELEASE_STATE = HUB_DEPLOY_ROOT / ".releases.json"


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path, body, *, exclusive=False):
    fd, name = tempfile.mkstemp(prefix=path.name + ".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(name, path)
        else:
            os.replace(name, path)
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def write_release(data):
    durable_write(RELEASE_STATE, json.dumps(data, sort_keys=True) + "\n")


def read_release():
    require(RELEASE_STATE.is_file() and not RELEASE_STATE.is_symlink(),
            "No verified release history")
    state = json.loads(RELEASE_STATE.read_text())
    require(isinstance(state, dict) and "previous_image" in state,
            "Invalid release history")
    return state


def write_image(image):
    conf = settings()
    original = ENV.read_text()
    old = "RO_IMAGE=" + conf["RO_IMAGE"] + "\n"
    require(original.count(old) == 1, "Image entry is ambiguous")
    durable_write(ENV, original.replace(old, "RO_IMAGE=" + image + "\n"))


def compose_up(identifier):
    # Constrain Docker itself to the observed artifact even if a tag moves
    # after the inspection and before service creation.
    compose("up", "-d", "--no-build", "--pull", "never", "hub", runtime_image=identifier)


def compose_stop():
    compose("stop", "hub")


def image_id(image):
    result = call("docker", "image", "inspect", "--format", "{{.Id}}", image, check=False)
    identifier = result.stdout.strip()
    require(result.returncode == 0 and re.fullmatch(r"sha256:[0-9a-f]{64}", identifier),
            "Required local image missing or invalid")
    return identifier


def observed_hub(config, images):
    ids = compose("ps", "--all", "--quiet", "hub", config=config).stdout.split()
    require(len(ids) <= 1, "Multiple HUB containers")
    if not ids:
        return None
    output = call("docker", "container", "inspect", ids[0]).stdout
    containers = json.loads(output)
    require(isinstance(containers, list) and len(containers) == 1,
            "Cannot inspect the expected Docker HUB")
    inspected = containers[0]
    options = inspected["Config"]
    runtime = inspected["HostConfig"]
    labels = options["Labels"]
    environment = dict(item.split("=", 1) for item in options["Env"])
    mounts = [m for m in inspected["Mounts"] if m["Destination"] == "/var/lib/owrt-remote"]
    require(labels.get("com.docker.compose.project") == PROJECT and
            labels.get("com.docker.compose.service") == "hub" and
            inspected["Image"] in images and inspected["Name"] == "/" + PROJECT + "-hub" and
            runtime["NetworkMode"] == "host" and runtime["ReadonlyRootfs"] is True and
            options["User"] == f"{HUB_UID}:{HUB_GID}" and len(mounts) == 1 and
            mounts[0]["Type"] == "bind" and mounts[0]["RW"] is True and
            mounts[0]["Source"] == config["RO_STATE_DIR"] and
            environment.get("OWRT_REMOTE_BIND") == "127.0.0.1" and
            environment.get("OWRT_REMOTE_STATE_DIR") == "/var/lib/owrt-remote" and
            environment.get("OWRT_REMOTE_RUNTIME") == "docker" and
            environment.get("OWRT_REMOTE_PORT") == config["RO_PORT"] and
            environment.get("OWRT_REMOTE_PUBLIC_URL") == config["RO_PUBLIC_URL"] and
            environment.get("OWRT_REMOTE_XRAY_CONTROL_BACKEND") == config["RO_XRAY_BACKEND"],
            "Actual HUB identity or configuration differs from the transaction")
    require(inspected["State"]["Status"] in {"created", "exited", "running", "restarting", "paused"},
            "HUB is in an unexpected state")
    return inspected


def verified_hub(config, identifier):
    hub = observed_hub(config, {identifier})
    require(hub and hub["State"]["Status"] == "running", "Expected HUB is not running")
    revision = hub["Config"]["Labels"].get(SOURCE_LABEL)
    require(revision is None or re.fullmatch(r"[0-9a-f]{64}", revision), "Invalid HUB source label")
    require(health(int(config["RO_PORT"]), revision=revision), "HUB failed /health")
    hub = observed_hub(config, {identifier})
    require(hub and hub["State"]["Status"] == "running", "HUB stopped during health check")


def schema_fingerprint(config):
    state = Path(config["RO_STATE_DIR"])
    require(state == state.resolve(), "Unsafe SQLite state path")
    digest = hashlib.sha256()
    with sqlite_read(state / "hub.db") as db:
        db.execute("BEGIN")
        check_integrity(db)
        versions = [db.execute("PRAGMA " + name).fetchone()[0]
                    for name in ("user_version", "application_id")]
        digest.update(json.dumps(versions).encode() + b"\n")
        for row in db.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema "
                              "ORDER BY type, name, tbl_name"):
            digest.update(json.dumps(row).encode() + b"\n")
    return digest.hexdigest()


def journal_path():
    return HUB_DEPLOY_ROOT / ".transaction.json"


def read_transaction():
    path = journal_path()
    require(not path.is_symlink(), "Unsafe transaction journal")
    if not path.exists():
        return None
    require(path.is_file(), "Unsafe transaction journal")
    tx = json.loads(path.read_text())
    require(isinstance(tx, dict) and tx.get("version") == 1 and
            re.fullmatch(r"[0-9a-f]{32}", tx.get("id", "")) and
            tx.get("phase") in {"prepared", "starting", "switching", "reverting", "commit", "complete", "recovered"} and
            tx.get("operation") in {"install", "deploy", "rollback"},
            "Invalid transaction journal")
    return tx


def pending_transaction():
    tx = read_transaction()
    return tx if tx and tx["phase"] not in {"complete", "recovered"} else None


def save_transaction(tx, phase, **fields):
    record = dict(tx, phase=phase, updated=time.time(), **fields)
    durable_write(journal_path(), json.dumps(record, sort_keys=True) + "\n")
    tx.update(record)


def compose_signature(config):
    # Hash the actual effective Compose model, including interpolated inputs.
    model = compose("config", "--format", "json", config=config).stdout
    return hashlib.sha256(json.dumps(json.loads(model), sort_keys=True).encode()).hexdigest()


def begin_transaction(operation, target, target_id, source=None, source_id=None, schema=None):
    require(not pending_transaction(), "Pending transaction: run recover")
    tx = {"version": 1, "id": uuid.uuid4().hex, "operation": operation,
          "target": target, "target_id": target_id, "source": source, "source_id": source_id,
          "schema": schema,
          "previous_release": read_release() if source and
          (RELEASE_STATE.exists() or RELEASE_STATE.is_symlink()) else None,
          "target_model": compose_signature(target),
          "source_model": compose_signature(source) if source else None}
    # Keep the previous terminal record when a new transaction takes its place.
    previous = read_transaction()
    if previous:
        history = journal_path().with_name(journal_path().name + "." + previous["id"])
        if not history.exists():
            durable_write(history, json.dumps(previous, sort_keys=True) + "\n", exclusive=True)
    save_transaction(tx, "prepared")
    return tx


def check_transaction(tx):
    require(not status_service(), "Host HUB active: recovery is blocked")
    target, source = tx["target"], tx["source"]
    validate_settings(target)
    if source:
        validate_settings(source)
        require({**source, "RO_IMAGE": target["RO_IMAGE"]} == target, "Unexpected configuration change")
    for role in ("source", "target"):
        config = tx[role]
        if config:
            require(compose_signature(config) == tx[role + "_model"],
                    "Compose model changed since transaction began")
    if ENV.exists() or ENV.is_symlink():
        require(settings() in (source, target), "Settings differ from transaction")
    else:
        require(source is None and tx["phase"] == "prepared", "Missing transaction settings")


def finish_install(tx):
    check_transaction(tx)
    config = tx["target"]
    identifier = tx["target_id"]
    require(image_id(config["RO_IMAGE"]) == identifier, "Install image tag changed")
    state = Path(config["RO_STATE_DIR"])
    hub = observed_hub(config, {identifier})
    require(state == state.resolve() and HUB_DEPLOY_ROOT in state.parents, "Unsafe install state path")
    if not ENV.exists():
        require(hub is None, "Unexpected HUB during incomplete install")
        # Resume only an empty directory created before publishing .env.
        create_private_parents(state)
        state.mkdir(mode=0o700, exist_ok=True)
        require(state.is_dir() and not any(state.iterdir()) and
                state.stat().st_uid in {0, HUB_UID} and stat.S_IMODE(state.stat().st_mode) == 0o700,
                "Incomplete install state needs operator inspection")
        os.chown(state, HUB_UID, HUB_GID)
        sync_directory(state)
        sync_directory(state.parent)
        save_env(config)
    require(state.is_dir() and state.stat().st_uid == HUB_UID, "Install state missing or unsafe")
    if tx["phase"] == "prepared":
        save_transaction(tx, "starting")
    try:
        if not hub or hub["State"]["Status"] in {"created", "exited"}:
            db = state / "hub.db"
            if db.exists() or db.is_symlink():
                schema_fingerprint(config)
            compose_up(identifier)
        verified_hub(config, identifier)
        schema_fingerprint(config)
    except BaseException:
        hub = observed_hub(config, {identifier})
        if hub and hub["State"]["Status"] not in {"created", "exited"}:
            compose_stop()
        raise
    save_transaction(tx, "complete")


def finalize_switch(tx):
    config = tx["target"]
    require(settings() == config and image_id(config["RO_IMAGE"]) == tx["target_id"],
            "Committed target configuration or tag changed")
    verified_hub(config, tx["target_id"])
    require(schema_fingerprint(config) == tx["release"]["current_schema"],
            "Committed SQLite schema changed")
    write_release(tx["release"])
    save_transaction(tx, "complete")


def recover_switch(tx):
    check_transaction(tx)
    if tx["phase"] == "commit":
        config = tx["target"]
        require(settings() == config and image_id(config["RO_IMAGE"]) == tx["target_id"],
                "Committed target configuration or tag changed")
        hub = observed_hub(config, {tx["target_id"]})
        if not hub or hub["State"]["Status"] in {"exited", "created"}:
            require(schema_fingerprint(config) == tx["release"]["current_schema"],
                    "Committed SQLite schema changed")
            compose_up(tx["target_id"])
        finalize_switch(tx)
        return

    original, target = tx["source"], tx["target"]
    require(image_id(original["RO_IMAGE"]) == tx["source_id"], "Original image tag changed")
    phase = tx["phase"]
    allowed = {tx["source_id"]}
    if phase != "prepared":
        allowed.add(tx["target_id"])
    else:
        require(settings() == original, "Prepared transaction settings changed")
    hub = observed_hub(settings(), allowed)
    if phase == "switching" or (hub and hub["Image"] == tx["target_id"]):
        # Stop a possible migrated writer before validating the live DB. Preserve
        # the database and journal for inspection if the original schema differs.
        if hub and hub["State"]["Status"] not in {"exited", "created"}:
            compose_stop()
        hub = observed_hub(settings(), allowed)
        require(not hub or hub["State"]["Status"] in {"exited", "created"},
                "Cannot verify the HUB stopped")
        require(schema_fingerprint(original) == tx["schema"],
                "SQLite schema changed; automatic rollback blocked, live state preserved")
        save_transaction(tx, "reverting")
    else:
        require(schema_fingerprint(original) == tx["schema"],
                "SQLite schema changed; recovery blocked")
    if settings() == target:
        write_image(original["RO_IMAGE"])
    if not hub or hub["Image"] != tx["source_id"] or hub["State"]["Status"] != "running":
        compose_up(tx["source_id"])
    verified_hub(original, tx["source_id"])
    require(schema_fingerprint(original) == tx["schema"], "Recovered SQLite schema changed")
    save_transaction(tx, "recovered")


def recover():
    tx = pending_transaction()
    require(tx is not None, "No pending transaction")
    if tx["operation"] == "install":
        finish_install(tx)
    else:
        recover_switch(tx)
    print("RECOVERY:", tx["operation"], tx["id"], tx["phase"])


def switch_image(operation, conf, candidate, expected=None):
    original_id = image_id(conf["RO_IMAGE"])
    target_id = image_id(candidate)
    require(original_id != target_id, "Candidate has the same image ID")
    if expected:
        require(expected.get("previous_id") == target_id and expected.get("current_id") == original_id,
                "Rollback image IDs are unverified or changed")
    verified_hub(conf, original_id)
    schema = schema_fingerprint(conf)
    if expected:
        require(expected.get("previous_schema") == schema == expected.get("current_schema"),
                "Rollback SQLite schema compatibility is unverified")
    target = dict(conf, RO_IMAGE=candidate)
    commit = git_sha() if operation == "deploy" else None
    tx = begin_transaction(operation, target, target_id, conf, original_id, schema)
    try:
        compose_stop()
        require(schema_fingerprint(conf) == schema, "SQLite schema changed before switch")
        archive = backup()
        require(image_id(candidate) == target_id, "Candidate image tag changed")
        save_transaction(tx, "switching", snapshot=str(archive))
        write_image(candidate)
        compose_up(target_id)
        verified_hub(target, target_id)
        new_schema = schema_fingerprint(target)
        if expected:
            require(new_schema == schema, "Rollback image changed the SQLite schema")
        release = {"previous_image": conf["RO_IMAGE"], "current_image": candidate,
                   "previous_id": original_id, "current_id": target_id,
                   "previous_schema": schema, "current_schema": new_schema,
                   "snapshot": str(archive), "timestamp": time.time()}
        if commit:
            release["commit"] = commit
        save_transaction(tx, "commit", release=release)
        finalize_switch(tx)
    except BaseException:
        # Re-read durable state: a rename may have succeeded before fsync failed.
        persisted = pending_transaction()
        if persisted and persisted["phase"] != "commit":
            recover_switch(persisted)
        raise


def deploy(args):
    require_root()
    require(args.acknowledge_schema_risk,
            "Schema compatibility is not proven: --acknowledge-schema-risk required")
    require(not status_service(), "Host HUB is active: refuse concurrent Docker handover")
    conf = settings()
    candidate = build_image()
    switch_image("deploy", conf, candidate)
    print("DEPLOY PASS:", candidate, "PREVIOUS:", conf["RO_IMAGE"])


def rollback(args):
    require_root()
    require(args.confirm, "Explicit --confirm required")
    require(not status_service(), "Host HUB active: refuse mixed-writer rollback")
    conf = settings()
    prior = read_release()
    require(prior.get("current_image") == conf["RO_IMAGE"] and
            prior["previous_image"] != conf["RO_IMAGE"], "Release history differs from settings")
    switch_image("rollback", conf, prior["previous_image"], expected=prior)
    print("ROLLBACK PASS:", prior["previous_image"])


def status():
    tx = pending_transaction()
    if tx:
        print("Pending transaction:", tx["id"], tx["operation"], tx["phase"])
    print("Repository:", ROOT)
    if ENV.exists():
        conf = settings()
        print("Image:", conf["RO_IMAGE"])
        print("Port:", conf["RO_PORT"])
        print("State:", conf["RO_STATE_DIR"])
    print("Host service active:", status_service())
    print("Docker HUB running:", docker_running() if ENV.exists() else "unconfigured")

def main():
    # Keep a single manager module instance when invoked as a script.
    sys.modules.setdefault("manager", sys.modules[__name__])
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    ins = subs.add_parser("install")
    ins.add_argument("--non-interactive", action="store_true")
    ins.add_argument("--public-url")
    ins.add_argument("--port", type=int)
    ins.add_argument("--state-dir")
    subs.add_parser("status")
    subs.add_parser("build")
    subs.add_parser("backup")
    subs.add_parser("recover")
    upgrade_parser = subs.add_parser("upgrade")
    upgrade_parser.add_argument("--acknowledge-schema-risk", action="store_true")
    rollback_parser = subs.add_parser("rollback")
    rollback_parser.add_argument("--confirm", action="store_true")
    restore_parser = subs.add_parser("restore")
    restore_parser.add_argument("archive")
    restore_parser.add_argument("--destination", required=True)
    restore_parser.add_argument("--stack-bundle", action="store_true",
                                help="restore HUB state and root-only files into an isolated bundle")
    stack_parser = subs.add_parser("stack")
    stack_parser.add_argument("--env-file", type=Path, required=True)
    stack_actions = stack_parser.add_subparsers(dest="operation", required=True)
    for operation in ("validate", "start", "stop", "status", "recover", "rollback", "backup"):
        stack_actions.add_parser(operation)
    update = stack_actions.add_parser("update")
    update.add_argument("--candidate-env", type=Path, required=True)
    update.add_argument("--acknowledge-schema-risk", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "stack":
            from stack_runtime import run
            with deployment_lock():
                run(args)
        elif args.action == "status":
            status()
        else:
            with deployment_lock():
                if args.action != "recover":
                    require(not pending_transaction(), "Pending transaction: run recover")
                if args.action == "recover":
                    recover()
                elif args.action == "install":
                    install(args)
                elif args.action == "build":
                    print("BUILT:", build_image())
                elif args.action == "backup":
                    backup()
                elif args.action == "restore":
                    restore(args)
                elif args.action == "upgrade":
                    deploy(args)
                elif args.action == "rollback":
                    rollback(args)
        return 0
    except (Blocked, OSError, EOFError, subprocess.SubprocessError, ValueError,
            sqlite3.DatabaseError, tarfile.TarError, zlib.error, KeyError, TypeError) as exc:
        print("BLOCKED:", str(exc)[:350], file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
