#!/usr/bin/env python3
"""Router Orchestrator Docker operator CLI.

This is an opt-in fresh-install / offline-backup CLI, NOT a live Belt cutover.
It deliberately blocks host HUB co-existence and implicit state restoration.
"""
from __future__ import annotations
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"
APP = ROOT / "vps" / "owrt-remote-hub.py"
LOCK = Path("/opt/router-orchestrator/.deploy.lock")
BACKUPS = Path("/opt/router-orchestrator/backups")
ALLOWED = {"RO_IMAGE", "RO_STATE_DIR", "RO_PORT", "RO_PUBLIC_URL", "RO_XRAY_BACKEND"}
TRANSIENT = {"hub.db-wal", "hub.db-shm", "hub.db-journal"}

class Blocked(Exception):
    pass

def require(condition, message):
    if not condition:
        raise Blocked(message)

def call(*args, check=True):
    return subprocess.run(args, cwd=ROOT, check=check, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)

def require_root():
    require(os.geteuid() == 0, "Run as root")

def settings():
    require(ENV.is_file() and not ENV.is_symlink(), ".env missing/unsafe")
    out = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        require("=" in line, "Invalid .env line")
        k, v = line.split("=", 1)
        require(k in ALLOWED and k not in out, "Unknown/duplicate .env variable")
        require(not re.search(r"\s|['\"\x00\r\n\x60$]", v), "Unquoted/simple .env values required")
        out[k] = v
    require(ALLOWED <= out.keys(), "Incomplete .env")
    require(out["RO_XRAY_BACKEND"] == "external_readonly",
            "Only external_readonly is approved for this release")
    require(out["RO_PORT"].isdigit() and 1024 <= int(out["RO_PORT"]) <= 65535,
            "Invalid port")
    path = Path(out["RO_STATE_DIR"])
    require(path.is_absolute() and path != Path("/var/lib/owrt-remote")
            and not path.is_symlink(), "Unsafe state path")
    require(out["RO_IMAGE"].startswith("router-orchestrator-hub:"),
            "Unexpected image tag")
    return out

def save_env(data):
    require(not ENV.exists(), "Refusing to replace existing .env")
    body = "# Managed by docker/manager.py; secrets never committed.\n"
    body += "".join(f"{k}={data[k]}\n" for k in sorted(ALLOWED))
    fd = os.open(ENV, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())

def status_service():
    return subprocess.run(["systemctl", "is-active", "--quiet", "owrt-remote.service"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def docker_running():
    r = call("docker", "compose", "ps", "--status", "running", "-q", "hub", check=False)
    return bool(r.stdout.strip())

def health(port, attempts=12):
    import urllib.request
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as resp:
                if json.load(resp).get("ok") is True:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False

def git_sha():
    result = call("git", "rev-parse", "--verify", "HEAD")
    return result.stdout.strip()

def build_image():
    require(APP.is_file() and not APP.is_symlink(), "HUB source missing")
    commit = git_sha()
    image = "router-orchestrator-hub:" + commit[:12]
    source_hash = hashlib.sha256(APP.read_bytes()).hexdigest()
    subprocess.run(["docker", "build", "--file", "docker/hub/Dockerfile",
                    "--build-arg", "HUB_SOURCE_SHA256=" + source_hash,
                    "--tag", image, "."], cwd=ROOT, check=True)
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
    require(not ENV.exists(), "An installation already exists; never overwrite .env")
    require(call("docker", "compose", "version").returncode == 0, "Docker Compose missing")
    default_port = 18089
    default_data = "/opt/router-orchestrator/data"
    if args.non_interactive:
        require(args.public_url and args.port and args.state_dir,
                "Noninteractive mode requires --public-url, --port and --state-dir")
    url = args.public_url or input("Public HTTPS URL (e.g. https://hub.example.net): ").strip()
    port = int(args.port or input(f"Local HUB port [{default_port}]: ").strip() or default_port)
    state = Path(args.state_dir or input(f"State directory [{default_data}]: ").strip() or default_data)
    require(re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d+)?", url) is not None,
            "Require public HTTPS URL without path")
    require(1024 <= port <= 65535 and free_port(port), "Port occupied/invalid")
    require(state.is_absolute() and str(state).startswith("/opt/router-orchestrator/")
            and state != ROOT and not state.exists(), "Require NEW state directory under /opt/router-orchestrator")
    require(not ROOT.resolve().is_relative_to(state.resolve()), "Unsafe overlapping path")
    answer = "yes" if args.non_interactive else input("Create clean Docker HUB (no routers yet)? type YES: ")
    require(answer.lower() == "yes", "Cancelled")
    image = build_image()
    state.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chown(state, 65532, 65532)
    save_env({"RO_IMAGE": image, "RO_STATE_DIR": str(state), "RO_PORT": str(port),
              "RO_PUBLIC_URL": url, "RO_XRAY_BACKEND": "external_readonly"})
    try:
        subprocess.run(["docker", "compose", "up", "-d", "--no-build", "hub"], cwd=ROOT, check=True)
        require(health(port), "HUB health failed; inspect container logs privately")
    except BaseException:
        subprocess.run(["docker", "compose", "stop", "hub"], cwd=ROOT, check=False)
        raise
    print("PASS: clean HUB is listening on loopback port", port)
    print("Host Xray, Nginx and TLS are NOT configured by this installer")

def state_path():
    value = Path(settings()["RO_STATE_DIR"])
    require(value.is_dir() and not value.is_symlink(), "State missing/unsafe")
    return value

def backup():
    require_root()
    require(not docker_running(), "Stop Docker HUB before consistency backup")
    state = state_path()
    require((state / "hub.db").is_file(), "hub.db missing")
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(BACKUPS, 0o700)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = BACKUPS / ("router-orchestrator-" + stamp + ".tar.gz")
    require(not dest.exists(), "Backup already exists")
    files = {}
    with tempfile.TemporaryDirectory(prefix=".router-backup-", dir=BACKUPS) as td:
        dbpath = Path(td) / "hub.db"
        with sqlite3.connect(str(state / "hub.db")) as src, sqlite3.connect(str(dbpath)) as dst:
            src.backup(dst)
            require(dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "SQLite backup corrupt")
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, tarfile.open(fileobj=out, mode="w:gz") as tar:
            for item in sorted(state.rglob("*")):
                require(not item.is_symlink(), "State contains symlink: stop and audit")
                if item.is_dir():
                    continue
                require(item.is_file(), "Non-regular state member")
                if item.name in TRANSIENT or item.name == "hub.db":
                    continue
                rel = item.relative_to(state).as_posix()
                content = item.read_bytes()
                files[rel] = hashlib.sha256(content).hexdigest()
                info = tarfile.TarInfo("state/" + rel)
                info.mode = 0o600
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
            content = dbpath.read_bytes()
            files["hub.db"] = hashlib.sha256(content).hexdigest()
            info = tarfile.TarInfo("state/hub.db")
            info.mode = 0o600
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
            manifest = {"format": 1, "files": files,
                        "hub_source_sha256": hashlib.sha256(APP.read_bytes()).hexdigest()}
            blob = json.dumps(manifest, sort_keys=True).encode()
            info = tarfile.TarInfo("manifest.json")
            info.mode = 0o600
            info.size = len(blob)
            tar.addfile(info, io.BytesIO(blob))
            out.flush()
            os.fsync(out.fileno())
    print("BACKUP:", dest)
    print("SHA256:", hashlib.sha256(dest.read_bytes()).hexdigest())
    return dest

def restore(args):
    require_root()
    dest = Path(args.destination)
    require(dest.is_absolute() and str(dest).startswith("/opt/router-orchestrator/")
            and not dest.exists(), "Restore requires a NEW destination under /opt/router-orchestrator")
    archive = Path(args.archive)
    require(archive.is_file() and not archive.is_symlink(), "Archive missing")
    require(archive.stat().st_size <= 2 * 1024**3, "Backup unexpectedly large")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        require(len(members) <= 10000, "Too many archive members")
        require(all(m.isfile() and not m.issym() and not m.islnk() for m in members),
                "Unsafe archive member")
        names = [m.name for m in members]
        require(len(names) == len(set(names)) and "manifest.json" in names,
                "Missing/duplicate manifest")
        manifest = json.load(tar.extractfile("manifest.json"))
        require(manifest.get("format") == 1, "Unsupported backup format")
        expected = manifest.get("files")
        require(isinstance(expected, dict) and "hub.db" in expected
                and set(names) == {"manifest.json"} | {"state/" + n for n in expected},
                "Manifest/archive mismatch")
        require(all(n and all(p not in ("", ".", "..") for p in Path(n).parts)
                    and not n.startswith("/") and "\\" not in n for n in expected),
                "Unsafe filename")
        require(sum(m.size for m in members) <= 4 * 1024**3, "Extracted data too large")
        dest.mkdir(mode=0o700, parents=True)
        try:
            for n, expected_hash in expected.items():
                chunk = tar.extractfile("state/" + n).read()
                require(hashlib.sha256(chunk).hexdigest() == expected_hash,
                        "Backup digest mismatch")
                target = dest / n
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_bytes(chunk)
                target.chmod(0o600)
                os.chown(target, 65532, 65532)
            for directory in sorted((p for p in dest.rglob("*") if p.is_dir()), reverse=True):
                os.chown(directory, 65532, 65532)
            os.chown(dest, 65532, 65532)
            with sqlite3.connect(f"file:{dest / 'hub.db'}?mode=ro", uri=True) as db:
                require(db.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                        "Restored SQLite corrupt")
        except BaseException:
            shutil.rmtree(dest)
            raise
    print("PASS: isolated restore:", dest)
    print("Never mount it over the live state without a separate cutover plan")

def status():
    print("Repository:", ROOT)
    if ENV.exists():
        conf = settings()
        print("Image:", conf["RO_IMAGE"])
        print("Port:", conf["RO_PORT"])
        print("State:", conf["RO_STATE_DIR"])
    print("Host service active:", status_service())
    print("Docker HUB running:", docker_running())

def main():
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
    restore_parser = subs.add_parser("restore")
    restore_parser.add_argument("archive")
    restore_parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    try:
        if args.action == "install":
            install(args)
        elif args.action == "status":
            status()
        elif args.action == "build":
            require_root()
            print("BUILT:", build_image())
        elif args.action == "backup":
            backup()
        elif args.action == "restore":
            restore(args)
        return 0
    except (Blocked, OSError, subprocess.CalledProcessError, ValueError,
            sqlite3.DatabaseError, tarfile.TarError) as exc:
        print("BLOCKED:", str(exc)[:350], file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
