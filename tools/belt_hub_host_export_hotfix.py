#!/usr/bin/env python3
"""Source-only Belt HUB host-export hotfix.

Changes exactly one known Python function block. Never changes SQLite,
Xray JSON, Nginx or router files. Explicit --apply restarts HUB only.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request

OLD = '''    effective_public_url = str(public_url_override or row_str_value(row, "public_url", "")).strip()
    effective_vps_host = str(vps_host_override or row_str_value(row, "vps_host", "")).strip()
    if not effective_vps_host:
        effective_vps_host = vps_host_name(effective_public_url) or vps_host_name(hub_url) or "127.0.0.1"
'''
NEW = '''    effective_public_url = str(public_url_override or row_str_value(row, "public_url", "")).strip()
    # Xray expects a hostname/IP in vps_host, never a URL with https://,
    # a scheme, port or path. Historical HUB rows and UI overrides may
    # contain a full HTTPS URL. Normalize *only the exported router config*:
    # leave the existing DB, heartbeat host policy and HTTPS hub/public URLs
    # unchanged so connected routers retain their settings.
    effective_vps_host = (
        vps_host_name(vps_host_override or row_str_value(row, "vps_host", ""))
        or vps_host_name(effective_public_url)
        or vps_host_name(hub_url)
        or "127.0.0.1"
    )
'''


class Blocked(Exception):
    pass


def require(value, reason):
    if not value:
        raise Blocked(reason)


def patch_source(text):
    if text.count(NEW) == 1 and OLD not in text:
        return text, False
    require(text.count(OLD) == 1 and NEW not in text,
            "installed HUB source differs from reviewed export block")
    return text.replace(OLD, NEW), True


def hash_file(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def service_pid(name):
    active = subprocess.run(["systemctl", "is-active", "--quiet", name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=6)
    require(active.returncode == 0, name + " is not active")
    pid = subprocess.run(["systemctl", "show", "--property=MainPID",
                          "--value", name],
                         capture_output=True, text=True, timeout=6)
    require(pid.returncode == 0 and pid.stdout.strip().isdigit(),
            name + " PID unknown")
    value = int(pid.stdout.strip())
    require(value > 1, name + " has no main process")
    return value


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:8088/health", timeout=4) as response:
            data = json.loads(response.read(8192))
        require(data.get("ok") is True, "HUB health != ok")
    except (OSError, ValueError) as exc:
        raise Blocked("HUB health check failed") from exc


def restart_hub():
    result = subprocess.run(["systemctl", "restart", "owrt-remote.service"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=35)
    require(result.returncode == 0, "HUB systemd restart failed")
    import time
    for _ in range(20):
        try:
            health()
            return
        except Blocked:
            time.sleep(1)
    raise Blocked("HUB health failed after restart")


def smoke_test_source(path, root):
    env = dict(os.environ)
    isolated = root / "smoke-state"
    isolated.mkdir(mode=0o700)
    env["OWRT_REMOTE_STATE_DIR"] = str(isolated)
    code = '''
import importlib.util, sys
p=sys.argv[1]
spec=importlib.util.spec_from_file_location("hub_host_hotfix_smoke",p)
m=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=m
spec.loader.exec_module(m)
row={"id":"test-only","vps_host":"https://sprt-wrt.cdnvc.xyz:443/owrt-ws/",
     "public_url":"https://sprt-wrt.cdnvc.xyz",
     "vps_transport":"ws","vless_port":443}
cfg=m.build_openwrt_config_payload(row,"https://sprt-wrt.cdnvc.xyz")
assert cfg["vps_host"]=="sprt-wrt.cdnvc.xyz"
assert cfg["vps_sni"]=="sprt-wrt.cdnvc.xyz"
assert cfg["hub_url"]=="https://sprt-wrt.cdnvc.xyz"
assert cfg["public_url"]=="https://sprt-wrt.cdnvc.xyz"
'''
    result = subprocess.run([sys.executable, "-c", code, str(path)],
                            cwd=str(root), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=25)
    require(result.returncode == 0, "isolated HUB config export smoke test failed")


def atomic_source_copy(source, target):
    fd, name = tempfile.mkstemp(prefix=".owrt-hub-host-", suffix=".py",
                                dir=str(target.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        st = target.stat()
        os.chmod(tmp, st.st_mode & 0o777)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def run(args):
    require(os.geteuid() == 0, "Belt root required")
    require(args.installed.is_file() and not args.installed.is_symlink(),
            "invalid installed HUB source")
    require(args.installed.resolve() == Path("/opt/owrt-remote/owrt-remote-hub.py"),
            "unexpected installed HUB source path")
    require(args.xray_config.is_file() and not args.xray_config.is_symlink(),
            "invalid Xray config")
    hold = Path("/var/lib/owrt-remote/.xray-maintenance-hold")
    require(hold.is_file() and not hold.is_symlink(),
            "Xray maintenance hold must exist; refusing risk to live Xray")
    with Path("/run/lock/owrt-hub-host-export-hotfix.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        health()
        old_hub_pid = service_pid("owrt-remote.service")
        xray_pid = service_pid("owrt-remote-xray.service")
        xray_digest = hash_file(args.xray_config)
        source_digest = hash_file(args.installed)
        text = args.installed.read_text(encoding="utf-8")
        candidate_text, needs_fix = patch_source(text)
        if not needs_fix:
            print("ALREADY PATCHED: installed HUB exports bare vps_host")
            return 0
        work = Path(tempfile.mkdtemp(prefix="owrt-hub-host-hotfix-", dir="/root"))
        work.chmod(0o700)
        candidate = work / "candidate.py"
        candidate.write_text(candidate_text, encoding="utf-8")
        candidate.chmod(0o600)
        result = subprocess.run([sys.executable, "-m", "py_compile", str(candidate)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=15)
        require(result.returncode == 0, "candidate Python syntax invalid")
        smoke_test_source(candidate, work)
        require(hash_file(args.installed) == source_digest and
                hash_file(args.xray_config) == xray_digest,
                "source/Xray changed during preflight")
        print("PRECHECK: PASS — exact one-function export-only change")
        print("SMOKE: PASS — host without HTTPS, hub/public URL stay HTTPS")
        print("XRAY: no changes; original PID=" + str(xray_pid))
        print("STAGE: " + str(work))
        if not args.apply:
            print("READ ONLY: live HUB and Xray unchanged; --apply is required")
            return 0
        backup = work / "installed-before.py"
        shutil.copy2(args.installed, backup)
        backup.chmod(0o600)
        require(hash_file(backup) == source_digest, "source changed during backup")
        swapped = False
        try:
            atomic_source_copy(candidate, args.installed)
            swapped = True
            restart_hub()
            require(hash_file(args.xray_config) == xray_digest and
                    service_pid("owrt-remote-xray.service") == xray_pid,
                    "dedicated Xray changed during HUB-only hotfix")
            require(service_pid("owrt-remote.service") != old_hub_pid,
                    "HUB did not restart")
            print("APPLIED: new router OpenWrt config uses bare vps_host")
            print("HUB: restarted and healthy; Xray PID/config unchanged")
            print("BACKUP: " + str(backup))
            print("HOLD: retained")
            return 0
        except Exception:
            if swapped:
                try:
                    atomic_source_copy(backup, args.installed)
                    restart_hub()
                    print("ROLLBACK: prior installed HUB source restored", file=sys.stderr)
                except Exception:
                    print("CRITICAL: restore HUB source manually from backup",
                          file=sys.stderr)
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--installed", type=Path,
                   default=Path("/opt/owrt-remote/owrt-remote-hub.py"))
    p.add_argument("--xray-config", type=Path,
                   default=Path("/etc/xray/owrt-remote.json"))
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    try:
        return run(args)
    except (Blocked, OSError, ValueError, TypeError, json.JSONDecodeError,
            subprocess.TimeoutExpired, BlockingIOError) as exc:
        print("BLOCKED: " + str(exc) if isinstance(exc, Blocked)
              else "BLOCKED: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
