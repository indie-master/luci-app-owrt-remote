#!/usr/bin/env python3
"""Host-only Router Orchestrator capability adapter.

No docker.sock, no arbitrary shell command, no dynamic paths or systemd units.
The write capabilities are OFF unless explicitly enabled by the operator.
Requests are accepted only from a dedicated Docker HUB peer UID over a
mode-0660 Unix socket. Mount /run/router-orchestrator read-only in HUB.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time

SOCKET = Path("/run/router-orchestrator/control.sock")
CONFIG = Path("/etc/xray/owrt-remote.json")
BINARY = "/usr/local/bin/xray"
SERVICE = "owrt-remote-xray"
HOLD = Path("/var/lib/owrt-remote/.xray-maintenance-hold")
MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024

class Rejected(RuntimeError):
    pass

def require(value, reason):
    if not value:
        raise Rejected(reason)

def sha(content):
    return hashlib.sha256(content).hexdigest()

def systemctl(*args, timeout=35):
    return subprocess.run(["systemctl", *args, SERVICE], capture_output=True,
                          timeout=timeout, text=True, check=False)

def config_inbounds(value):
    inbounds = value.get("inbounds") if isinstance(value, dict) else None
    require(isinstance(inbounds, list), "missing Xray inbound list")
    by_tag = {}
    for item in inbounds:
        require(isinstance(item, dict), "invalid inbound object")
        tag = item.get("tag")
        require(isinstance(tag, str) and tag not in by_tag, "duplicate/invalid inbound tag")
        by_tag[tag] = item
    return by_tag

def validate_transition(previous, candidate):
    old = config_inbounds(previous)
    new = config_inbounds(candidate)
    for tag in ("owrt-remote-vless", "owrt-remote-wss"):
        require(tag in old and tag in new, "missing required Xray transport")
        for field in ("protocol", "listen", "port", "streamSettings"):
            require(new[tag].get(field) == old[tag].get(field),
                    "transport listener/TLS/WS policy changed")
    # Protect all existing router reverse portals. Removing/reassigning a portal
    # requires a separate reviewed maintenance transaction, never auto-apply.
    for tag, old_inbound in old.items():
        if tag.startswith(("entry-", "ssh-")):
            require(tag in new and new[tag] == old_inbound,
                    "existing LuCI/SSH reverse portal changed: " + tag)
    require(len(json.dumps(candidate).encode()) <= MAX_REQUEST - 4096,
            "candidate Xray config exceeds limit")
    rules = candidate.get("routing", {}).get("rules", [])
    require(isinstance(rules, list), "invalid routing rules")

def atomic_replace(path, data):
    fd, temp = tempfile.mkstemp(prefix=".ro-xray-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        dirfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

def active_xray():
    if systemctl("is-active", "--quiet", timeout=5).returncode:
        return False
    for port in (18443, 18444):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            return False
    return True

def wait_xray(attempts=15):
    for _ in range(attempts):
        if active_xray():
            return True
        time.sleep(1)
    return False

class Adapter:
    def __init__(self, path=SOCKET, uid=23088, allow_restart=False, allow_apply=False):
        self.path = Path(path)
        self.uid = uid
        self.allow_restart = allow_restart
        self.allow_apply = allow_apply
        self.last_restart = 0
        self.cpu_previous = None

    def can_mutate(self, enabled):
        require(enabled, "Xray mutation not approved in host adapter")
        require(not HOLD.exists(), "Xray maintenance hold is active")
        require(time.monotonic() - self.last_restart >= 120,
                "Xray restart cooldown is active")

    def current(self):
        data = CONFIG.read_bytes()
        require(len(data) <= MAX_REQUEST - 4096, "Xray config exceeds response limit")
        return {"ok": True, "config": json.loads(data), "sha256": sha(data)}

    def xray_restart(self):
        self.can_mutate(self.allow_restart)
        require(systemctl("restart").returncode == 0 and wait_xray(),
                "Xray restart/health failed; investigate host service")
        self.last_restart = time.monotonic()
        return {"ok": True, "service": SERVICE}

    def xray_apply(self, request):
        self.can_mutate(self.allow_apply)
        original = CONFIG.read_bytes()
        require(request.get("expected_sha256") == sha(original),
                "Xray config changed concurrently")
        candidate = request.get("config")
        validate_transition(json.loads(original), candidate)
        data = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode()
        if json.loads(original) == candidate:
            return {"ok": True, "unchanged": True, "service": SERVICE}
        fd, test_path = tempfile.mkstemp(prefix=".ro-candidate-", suffix=".json",
                                         dir=CONFIG.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(data)
            test = subprocess.run([BINARY, "run", "-test", "-config", test_path],
                                  capture_output=True, timeout=25)
            require(test.returncode == 0, "Xray candidate failed validation")
        finally:
            Path(test_path).unlink(missing_ok=True)
        backup_dir = CONFIG.parent / "owrt-remote-backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        backup = backup_dir / ("router-orchestrator-" + stamp + ".json")
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
        try:
            atomic_replace(CONFIG, data)
            require(systemctl("restart").returncode == 0 and wait_xray(),
                    "Xray candidate failed to start")
        except BaseException:
            atomic_replace(CONFIG, original)
            recovered = systemctl("restart").returncode == 0 and wait_xray()
            require(recovered, "Xray rollback failed; immediate operator recovery required")
            raise Rejected("Xray candidate rejected; original config restored")
        self.last_restart = time.monotonic()
        return {"ok": True, "unchanged": False, "service": SERVICE,
                "backup": str(backup)}

    def metrics(self):
        info = {}
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith(("MemTotal:", "MemAvailable:")):
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        disk = shutil.disk_usage("/")
        with open("/proc/stat", encoding="ascii") as stream:
            ticks = [int(x) for x in stream.readline().split()[1:9]]
        now_ticks = (sum(ticks), ticks[3] + ticks[4])
        previous, self.cpu_previous = self.cpu_previous, now_ticks
        percent = None
        if previous:
            delta = now_ticks[0] - previous[0]
            idle = now_ticks[1] - previous[1]
            if delta > 0 and idle >= 0:
                percent = round(100 * (delta - idle) / delta, 1)
        return {"ok": True, "cpu": {"percent": percent, "cores": os.cpu_count() or 1},
                "memory": {"percent": round((total - avail) / total * 100, 1) if total else None,
                           "used_bytes": total - avail, "total_bytes": total,
                           "available_bytes": avail},
                "disk": {"percent": round(disk.used / disk.total * 100, 1),
                         "used_bytes": disk.used, "total_bytes": disk.total,
                         "free_bytes": disk.free, "path": "/"},
                "sampled_at": time.time(), "scope": "host-adapter"}

    def handle(self, request):
        require(isinstance(request, dict), "invalid request")
        op = request.get("op")
        if op == "health":
            return {"ok": True, "adapter": "host-control-v1"}
        if op == "host.metrics":
            return self.metrics()
        if op == "xray.status":
            return {"ok": True, "active": active_xray(), "service": SERVICE}
        if op == "xray.current":
            return self.current()
        if op == "xray.restart":
            return self.xray_restart()
        if op == "xray.apply":
            return self.xray_apply(request)
        raise Rejected("unsupported operation")

    def run(self):
        self.path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        require(not self.path.exists() and not self.path.is_symlink(),
                "Refuse existing socket; review ownership before starting")
        os.chown(self.path.parent, 0, self.uid)
        os.chmod(self.path.parent, 0o750)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            os.chown(self.path, 0, self.uid)
            os.chmod(self.path, 0o660)
            server.listen(8)
            try:
                while True:
                    conn, _ = server.accept()
                    with conn:
                        pid, uid, gid = struct.unpack("3i", conn.getsockopt(
                            socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        if uid != self.uid:
                            continue
                        conn.settimeout(8)
                        try:
                            with conn.makefile("rb") as reader:
                                raw = reader.readline(MAX_REQUEST + 1)
                            require(raw.endswith(b"\n") and len(raw) <= MAX_REQUEST,
                                    "request too large or incomplete")
                            response = self.handle(json.loads(raw))
                        except (Rejected, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                            response = {"ok": False, "error": str(exc)[:180]}
                        encoded = json.dumps(response, ensure_ascii=False).encode() + b"\n"
                        if len(encoded) <= MAX_RESPONSE:
                            conn.sendall(encoded)
            finally:
                self.path.unlink(missing_ok=True)

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", default=str(SOCKET))
    p.add_argument("--peer-uid", type=int, default=23088)
    p.add_argument("--allow-xray-restart", action="store_true")
    p.add_argument("--allow-xray-apply", action="store_true")
    args = p.parse_args()
    require(os.geteuid() == 0, "Host adapter requires root service")
    Adapter(args.socket, args.peer_uid, args.allow_xray_restart, args.allow_xray_apply).run()
