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
import stat
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

def _client_map(inbound):
    """Index clients without exposing credentials in validation errors."""
    settings = inbound.get("settings", {})
    require(isinstance(settings, dict), "invalid Xray inbound settings")
    clients = settings.get("clients", [])
    require(isinstance(clients, list), "invalid Xray client collection")
    indexed = {}
    emails = set()
    for entry in clients:
        require(isinstance(entry, dict), "invalid Xray client")
        identity = entry.get("id")
        email = entry.get("email")
        require(isinstance(identity, str) and identity and identity not in indexed,
                "missing/duplicate Xray client identity")
        require(isinstance(email, str) and email and email not in emails,
                "missing/duplicate Xray client label")
        indexed[identity] = entry
        emails.add(email)
    return indexed


def validate_transition(previous, candidate):
    """Fail closed: the only approved mutation enrolls ONE new WSS router.

    Existing LuCI/SSH listeners, identities and routing remain unchanged.
    An unrelated Xray change needs an independently reviewed transaction.
    """
    require(isinstance(previous, dict) and isinstance(candidate, dict),
            "invalid Xray config")
    require(previous.keys() == candidate.keys(),
            "unexpected top-level Xray config change")
    for key in previous:
        if key not in {"inbounds", "routing"}:
            require(previous[key] == candidate[key],
                    "unrelated Xray setting changed")
    old = config_inbounds(previous)
    new = config_inbounds(candidate)
    require(set(old).issubset(new), "existing Xray inbound removed")
    for tag, earlier in old.items():
        latest = new[tag]
        if tag in ("owrt-remote-vless", "owrt-remote-wss"):
            require({k: v for k, v in earlier.items() if k != "settings"} ==
                    {k: v for k, v in latest.items() if k != "settings"},
                    "existing transport listener/TLS/WS policy changed")
            original_settings = dict(earlier.get("settings", {}))
            updated_settings = dict(latest.get("settings", {}))
            original_settings.pop("clients", None)
            updated_settings.pop("clients", None)
            require(original_settings == updated_settings,
                    "existing transport settings changed")
        else:
            require(latest == earlier,
                    "existing LuCI/SSH reverse portal changed")
    for tag in ("owrt-remote-vless", "owrt-remote-wss"):
        require(tag in old and tag in new, "missing required Xray transport")

    legacy = _client_map(old["owrt-remote-vless"])
    next_legacy = _client_map(new["owrt-remote-vless"])
    require(legacy == next_legacy,
            "TCP identities changed; new routers must use WSS")
    wss = _client_map(old["owrt-remote-wss"])
    next_wss = _client_map(new["owrt-remote-wss"])
    require(all(next_wss.get(uid) == row for uid, row in wss.items()),
            "existing WSS router identity changed or removed")
    added = [row for uid, row in next_wss.items() if uid not in wss]

    new_tags = set(new) - set(old)
    routing_before = previous.get("routing", {})
    routing_after = candidate.get("routing", {})
    require(isinstance(routing_before, dict) and isinstance(routing_after, dict),
            "invalid routing settings")
    require({k: v for k, v in routing_before.items() if k != "rules"} ==
            {k: v for k, v in routing_after.items() if k != "rules"},
            "unrelated routing settings changed")
    before_rules = routing_before.get("rules", [])
    after_rules = routing_after.get("rules", [])
    require(isinstance(before_rules, list) and isinstance(after_rules, list),
            "invalid routing rules")

    # Preserve the relative order of existing routing rules even when the
    # generated config inserts new router rules in the middle of the list.
    pending = iter(after_rules)
    for rule in before_rules:
        for found in pending:
            if rule == found:
                break
        else:
            raise Rejected("existing routing rule removed or reordered")
    extra_rules = list(after_rules)
    for rule in before_rules:
        extra_rules.remove(rule)

    if not (added or new_tags or extra_rules):
        return  # Read-only/no-op candidate.

    require(len(added) == 2, "new WSS router requires exactly two clients")
    labels = {r["email"]: r for r in added}
    owners = [email[:-len("@owrt-remote")] for email in labels
              if email.endswith("@owrt-remote")
              and not email.endswith("-ssh@owrt-remote")]
    require(len(owners) == 1 and owners[0] and
            all(ch.isascii() and (ch.isalnum() or ch in "_.-")
                for ch in owners[0]),
            "invalid new router identity")
    router_id = owners[0]
    normal = router_id + "@owrt-remote"
    ssh = router_id + "-ssh@owrt-remote"
    require(set(labels) == {normal, ssh},
            "new WSS client pair is incomplete")
    require(labels[normal].get("reverse", {}).get("tag") ==
            "reverse-" + router_id and
            labels[ssh].get("reverse", {}).get("tag") ==
            "reverse-" + router_id + "-ssh",
            "new WSS reverse tags do not match")
    expected_tags = {"entry-" + router_id, "ssh-" + router_id}
    require(new_tags == expected_tags, "new reverse portal set changed")
    ports = [int(item.get("port", 0)) for item in new.values()]
    require(len(ports) == len(set(ports)),
            "new reverse portal port conflicts with existing listener")
    for tag in expected_tags:
        item = new[tag]
        require(item.get("protocol") == "tunnel" and
                item.get("listen") == "127.0.0.1" and
                isinstance(item.get("port"), int) and 1024 <= item["port"] <= 65535,
                "new reverse portal is not a loopback TCP tunnel")
        settings = item.get("settings", {})
        require(isinstance(settings, dict) and
                settings.get("allowedNetwork") == "tcp" and
                isinstance(settings.get("rewriteAddress"), str) and
                isinstance(settings.get("rewritePort"), int),
                "new reverse portal target is invalid")
    expected_rules = [
        {"type": "field", "inboundTag": ["entry-" + router_id],
         "outboundTag": "reverse-" + router_id},
        {"type": "field", "inboundTag": ["ssh-" + router_id],
         "outboundTag": "reverse-" + router_id + "-ssh"},
    ]
    require(len(extra_rules) == 2 and
            all(rule in extra_rules for rule in expected_rules),
            "new router rules differ from expected LuCI/SSH routes")
    require(len(json.dumps(candidate).encode()) <= MAX_REQUEST - 4096,
            "candidate Xray config exceeds limit")


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

def clear_stale_socket(path):
    """Safely recover a crash-left socket; never remove an active listener.

    Called only after checking a root-owned, non-writable runtime directory.
    """
    path = Path(path)
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return False
    require(stat.S_ISSOCK(meta.st_mode) and meta.st_uid == os.geteuid(),
            "existing control.sock is not an owned UNIX socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            # A lingering bind inode after SIGKILL does not have a listener.
            again = path.lstat()
            require(again.st_ino == meta.st_ino and
                    stat.S_ISSOCK(again.st_mode) and again.st_uid == meta.st_uid,
                    "control.sock changed while checking stale socket")
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise Rejected("control.sock could not be verified idle") from exc
        else:
            raise Rejected("Host adapter already listening on control.sock")


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

    def serve_peer(self, conn):
        """One peer-credential authorized request, tested on real UNIX sockets.

        Unauthorized UIDs receive no payload or sensitive error message.
        The peer check is performed before reading even one JSON byte.
        """
        _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED,
            struct.calcsize("3i")))
        if uid != self.uid:
            return False
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
        if len(encoded) > MAX_RESPONSE:
            encoded = b'{"ok":false,"error":"response too large"}\n'
        try:
            conn.sendall(encoded)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return False
        return True

    def run(self):
        require(self.path == SOCKET, "Host adapter is restricted to its fixed UNIX socket")
        parent = self.path.parent
        require(parent.exists() and not parent.is_symlink(),
                "systemd runtime directory missing/unsafe")
        meta = parent.lstat()
        require(stat.S_ISDIR(meta.st_mode) and meta.st_uid == 0
                and not (stat.S_IMODE(meta.st_mode) & 0o022),
                "runtime directory must be root-owned and not writable by group/others")
        # The group can traverse the directory and access only this socket;
        # it cannot unlink or replace a root-owned socket entry.
        os.chown(parent, 0, self.uid)
        os.chmod(parent, 0o750)
        clear_stale_socket(self.path)
        bound_inode = None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            bound_inode = self.path.lstat().st_ino
            os.chown(self.path, 0, self.uid)
            os.chmod(self.path, 0o660)
            server.listen(8)
            try:
                while True:
                    conn, _ = server.accept()
                    with conn:
                        self.serve_peer(conn)
            finally:
                # Never unlink a socket replaced behind our back.
                try:
                    info = self.path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and info.st_ino == bound_inode:
                        self.path.unlink()
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", default=str(SOCKET))
    p.add_argument("--peer-uid", type=int, default=23088)
    p.add_argument("--allow-xray-restart", action="store_true")
    p.add_argument("--allow-xray-apply", action="store_true")
    args = p.parse_args()
    require(Path(args.socket) == SOCKET, "custom socket paths are forbidden")
    require(0 < args.peer_uid < 65535, "invalid HUB peer UID")
    require(os.geteuid() == 0, "Host adapter requires root service")
    Adapter(args.socket, args.peer_uid, args.allow_xray_restart, args.allow_xray_apply).run()
