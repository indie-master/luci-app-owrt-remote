#!/usr/bin/env python3
"""Capability-limited, host-side UNIX-socket adapter (experimental).

Read-only status/metrics are implemented; Xray restart is opt-in and
maintenance-hold aware. Does NOT permit arbitrary commands or Xray regeneration.
Run as a separate systemd service only after peer UID/socket review.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time

class Adapter:
    def __init__(self, path, uid, allow_restart):
        self.path = Path(path)
        self.uid = uid
        self.allow_restart = allow_restart
        self.last_restart = 0

    def handle(self, request):
        operation = request.get("op")
        if operation == "health":
            return {"ok": True, "adapter": "host-readonly"}
        if operation == "xray.status":
            result = subprocess.run(["systemctl", "is-active", "owrt-remote-xray"],
                                    capture_output=True, text=True, timeout=4)
            return {"ok": True, "active": result.returncode == 0}
        if operation == "host.metrics":
            data = {}
            with open("/proc/meminfo", encoding="ascii") as stream:
                for line in stream:
                    if line.startswith(("MemTotal:", "MemAvailable:")):
                        k, v = line.split(":", 1)
                        data[k] = int(v.split()[0]) * 1024
            disk = os.statvfs("/")
            return {"ok": True, "memory_bytes": data, "disk_bytes":
                    {"total": disk.f_blocks * disk.f_frsize,
                     "free": disk.f_bavail * disk.f_frsize}}
        if operation == "xray.restart":
            if not self.allow_restart:
                return {"ok": False, "error": "restart not enabled"}
            if Path("/var/lib/owrt-remote/.xray-maintenance-hold").exists():
                return {"ok": False, "error": "maintenance hold"}
            if time.monotonic() - self.last_restart < 120:
                return {"ok": False, "error": "restart cooldown"}
            result = subprocess.run(["systemctl", "restart", "owrt-remote-xray"],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                return {"ok": False, "error": "xray restart failed"}
            self.last_restart = time.monotonic()
            return {"ok": True}
        return {"ok": False, "error": "unsupported operation"}

    def run(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            raise SystemExit("Refuse existing socket path (do not unlink)")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            os.chown(self.path, 0, self.uid)
            os.chmod(self.path.parent, 0o750)
            os.chmod(self.path, 0o660)
            server.listen(4)
            try:
                while True:
                    conn, _ = server.accept()
                    with conn:
                        peer = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                        pid, uid, gid = struct.unpack("3i", peer)
                        if uid != self.uid:
                            continue
                        conn.settimeout(5)
                        buf = conn.recv(8192)
                        if len(buf) >= 8192 or b"\n" not in buf:
                            continue
                        try:
                            request = json.loads(buf.split(b"\n", 1)[0])
                            response = self.handle(request)
                        except (ValueError, OSError, subprocess.TimeoutExpired):
                            response = {"ok": False, "error": "request failed"}
                        conn.sendall(json.dumps(response).encode() + b"\n")
            finally:
                self.path.unlink(missing_ok=True)

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--socket", default="/run/router-orchestrator/control.sock")
    p.add_argument("--peer-uid", type=int, default=65532)
    p.add_argument("--allow-xray-restart", action="store_true")
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Host adapter must run as root")
    Adapter(args.socket, args.peer_uid, args.allow_xray_restart).run()
