#!/usr/bin/env python3
"""Run Xray; validate requested config updates before restarting.
The active process is NOT interrupted if a candidate fails validation.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

CONFIG = Path(os.environ.get("XRAY_CONFIG", "/etc/xray/owrt-remote.json"))
CONTROL = Path(os.environ.get("CONTROL_DIR", "/run/owrt-control"))
REQUEST = CONTROL / "restart.seq"
PIDFILE = CONTROL / "xray.pid"
stop = False

def terminate(_sig, _frame):
    global stop
    stop = True

signal.signal(signal.SIGTERM, terminate)
signal.signal(signal.SIGINT, terminate)

def check():
    return subprocess.run(["/usr/local/bin/xray", "run", "-test", "-config", str(CONFIG)], timeout=30).returncode == 0

def signature():
    try:
        s = CONFIG.stat()
        return (s.st_mtime_ns, s.st_size)
    except OSError:
        return None

def launch():
    p = subprocess.Popen(["/usr/local/bin/xray", "run", "-config", str(CONFIG)])
    PIDFILE.write_text(str(p.pid))
    return p

def shutdown(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    PIDFILE.unlink(missing_ok=True)

def main():
    CONTROL.mkdir(parents=True, exist_ok=True)
    while not stop and not CONFIG.is_file():
        print("Waiting for Xray config...", flush=True)
        time.sleep(3)
    if stop:
        return 0
    if not check():
        print("Initial Xray config is invalid; refusing startup", file=sys.stderr, flush=True)
        return 1
    p = launch()
    last_signature = signature()
    last_request = REQUEST.read_text().strip() if REQUEST.exists() else ""
    while not stop:
        time.sleep(2)
        if p.poll() is not None:
            print(f"Xray exited {p.returncode}; supervisor exiting", file=sys.stderr, flush=True)
            PIDFILE.unlink(missing_ok=True)
            return p.returncode or 1
        current_request = REQUEST.read_text().strip() if REQUEST.exists() else ""
        new_signature = signature()
        if current_request != last_request or new_signature != last_signature:
            # Allow writer to finish and ensure stable bytes across two probes.
            time.sleep(0.5)
            if signature() != new_signature:
                continue
            last_request = current_request
            if check():
                print("Validated config change; restarting Xray", flush=True)
                shutdown(p)
                p = launch()
                last_signature = signature()
            else:
                print("Rejected invalid Xray configuration; keeping running process", file=sys.stderr, flush=True)
                last_signature = new_signature
    shutdown(p)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
