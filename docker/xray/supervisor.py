#!/usr/bin/env python3
"""A limited UNIX-socket Xray supervisor, without systemd or docker.sock."""
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time

CONFIG = Path(os.environ.get("XRAY_CONFIG", "/etc/xray/owrt-remote.json"))
CONTROL = Path(os.environ.get("CONTROL_DIR", "/run/owrt-control"))
SOCKET = CONTROL / "xray.sock"
BIN = "/usr/local/bin/xray"
MAX_BYTES = 2_000_000

class Manager:
    def __init__(self):
        self.child = None
        self.stopping = False
        self.next_retry = 0

    def validate(self, filename):
        p = subprocess.run([BIN, "run", "-test", "-config", str(filename)],
                           capture_output=True, text=True, timeout=30)
        if p.returncode:
            raise RuntimeError("Xray validation failed: " + (p.stderr or p.stdout)[-1200:])

    def start(self):
        if not CONFIG.is_file():
            return False
        self.validate(CONFIG)
        self.child = subprocess.Popen([BIN, "run", "-config", str(CONFIG)])
        time.sleep(0.7)
        if self.child.poll() is not None:
            raise RuntimeError("Xray exited during startup")
        return True

    def stop_child(self):
        child = self.child
        self.child = None
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()

    def install(self, data):
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".xray-candidate-", dir=CONFIG.parent)
        path = Path(name)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(path, 0o600)
            self.validate(path)
            prior = CONFIG.read_bytes() if CONFIG.exists() else None
            candidate = path.read_bytes()
            if prior == candidate:
                return {"changed": False}
            backup = CONFIG.with_name(CONFIG.name + ".last-good")
            if prior is not None:
                with open(backup, "wb") as f:
                    os.chmod(backup, 0o600)
                    f.write(prior)
            self.stop_child()
            os.replace(path, CONFIG)
            try:
                self.start()
            except Exception as exc:
                self.stop_child()
                if prior is not None:
                    restore_fd, restore_name = tempfile.mkstemp(dir=CONFIG.parent)
                    with os.fdopen(restore_fd, "wb") as f:
                        f.write(prior)
                    os.chmod(restore_name, 0o600)
                    os.replace(restore_name, CONFIG)
                    self.start()
                raise RuntimeError("New config failed to start; restored previous config: " + str(exc))
            return {"changed": True}
        finally:
            path.unlink(missing_ok=True)

    def restart(self):
        if not CONFIG.exists():
            raise RuntimeError("No Xray configuration available")
        self.validate(CONFIG)
        self.stop_child()
        self.start()

    def status(self):
        return {"ok": self.child is not None and self.child.poll() is None,
                "pid": self.child.pid if self.child and self.child.poll() is None else None}

    def handle(self, conn):
        with conn:
            conn.settimeout(35)
            body = bytearray()
            while b"\n" not in body:
                piece = conn.recv(8192)
                if not piece:
                    break
                body.extend(piece)
                if len(body) > MAX_BYTES:
                    raise ValueError("Request exceeds size limit")
            msg = json.loads(bytes(body).split(b"\n", 1)[0])
            action = msg.get("action")
            if action == "status":
                result = self.status()
            elif action == "apply":
                config = msg.get("config")
                if not isinstance(config, dict) or not isinstance(config.get("inbounds"), list):
                    raise ValueError("Invalid config payload")
                result = {"ok": True, **self.install(config)}
            elif action == "restart":
                self.restart()
                result = {"ok": True}
            else:
                raise ValueError("Unsupported control action")
            conn.sendall((json.dumps(result) + "\n").encode())

    def serve(self):
        CONTROL.mkdir(parents=True, exist_ok=True)
        SOCKET.unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(SOCKET))
        os.chmod(SOCKET, 0o600)
        listener.listen(8)
        listener.settimeout(1)
        def terminate(*_):
            self.stopping = True
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        try:
            while not self.stopping:
                if (self.child is None or self.child.poll() is not None) and CONFIG.exists() and time.monotonic() >= self.next_retry:
                    try:
                        self.start()
                        print("Xray started", flush=True)
                    except Exception as exc:
                        print("Xray startup error:", exc, file=sys.stderr, flush=True)
                        self.next_retry = time.monotonic() + 5
                try:
                    conn, _ = listener.accept()
                except socket.timeout:
                    continue
                try:
                    self.handle(conn)
                except Exception as exc:
                    print("Control operation rejected:", type(exc).__name__, file=sys.stderr, flush=True)
                    try:
                        conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
                    except OSError:
                        pass
                    conn.close()
        finally:
            self.stop_child()
            listener.close()
            SOCKET.unlink(missing_ok=True)

if __name__ == "__main__":
    Manager().serve()
