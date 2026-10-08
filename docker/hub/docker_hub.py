#!/usr/bin/env python3
"""Docker adapter around the upstream Hub; never talks to systemd/Docker API."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys

BASE = Path("/opt/owrt-remote/owrt-remote-hub.py")
spec = importlib.util.spec_from_file_location("upstream_owrt_hub", BASE)
hub = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hub
spec.loader.exec_module(hub)

CONTROL_SOCKET = os.environ.get("OWRT_CONTROL_SOCKET", "/run/owrt-control/xray.sock")
XRAY_CONFIG = Path(os.environ.get("OWRT_REMOTE_XRAY_CONFIG", "/etc/xray/owrt-remote.json"))

def request(action, config=None):
    message = {"action": action}
    if config is not None:
        message["config"] = config
    packet = (json.dumps(message, ensure_ascii=False) + "\n").encode()
    if len(packet) > 2_000_000:
        raise RuntimeError("Xray configuration exceeds IPC size limit")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(40)
        s.connect(CONTROL_SOCKET)
        s.sendall(packet)
        chunks = bytearray()
        while b"\n" not in chunks:
            part = s.recv(8192)
            if not part:
                break
            chunks.extend(part)
            if len(chunks) > 65536:
                raise RuntimeError("Xray controller response too large")
    response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    if not response.get("ok"):
        raise RuntimeError("Xray controller rejected operation: " + str(response.get("error", "unknown")))
    return response

def clean_host(value):
    """URL for the Hub is valid, but a VLESS address must be a hostname or IP."""
    raw = str(value or "").strip()
    if not raw:
        return raw
    host = hub.vps_host_name(raw)
    if not host:
        raise ValueError("Invalid VPS host")
    return host

def reload_xray(db_path=None):
    path = db_path or hub.DB_PATH
    with hub.connect(path) as conn:
        hub.init_db(conn)
        rows = hub.list_router_rows(conn)
        config = hub.make_server_xray_config(rows)
    # Xray validates the candidate inside its container before replacing the
    # persisted config. The response is synchronous: errors reach the web UI.
    result = request("apply", config)
    return {"config": str(XRAY_CONFIG), "service": "owrt-xray", "routers": len(rows),
            "changed": result.get("changed", True)}

def restart_xray():
    request("restart")
    return {"service": "owrt-xray"}

hub.reload_vps_xray = reload_xray
hub.restart_vps_xray = restart_xray

_original_update = hub.canonical_router_hub_update
def canonical_update(router, app_public_url="", request_origin=""):
    item = _original_update(router, app_public_url, request_origin)
    if item.get("vps_host"):
        item["vps_host"] = clean_host(item["vps_host"])
    return item
hub.canonical_router_hub_update = canonical_update

_original_payload = hub.build_openwrt_config_payload
def safe_payload(row, hub_url, vps_host_override="", public_url_override=""):
    data = dict(row)
    if data.get("vps_host"):
        data["vps_host"] = clean_host(data["vps_host"])
    if vps_host_override:
        vps_host_override = clean_host(vps_host_override)
    return _original_payload(data, hub_url, vps_host_override, public_url_override)
hub.build_openwrt_config_payload = safe_payload

_original_client = hub.make_client_xray_config
def safe_client(row, *args, **kwargs):
    data = dict(row)
    if data.get("vps_host"):
        data["vps_host"] = clean_host(data["vps_host"])
    return _original_client(data, *args, **kwargs)
hub.make_client_xray_config = safe_client

def create_initial_config():
    if XRAY_CONFIG.is_file():
        return
    with hub.connect(hub.DB_PATH) as conn:
        hub.init_db(conn)
        rows = hub.list_router_rows(conn)
        config = hub.make_server_xray_config(rows)
    hub.atomic_write_text(XRAY_CONFIG, json.dumps(config, ensure_ascii=False, indent=2) + "\n", mode=0o600)

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "set-password-stdin":
        username = sys.argv[2] if len(sys.argv) > 2 else "admin"
        password = sys.stdin.readline().rstrip("\r\n")
        if len(password) < 12:
            raise SystemExit("Password must be at least 12 characters")
        hub.save_auth(username, password)
        print("Hub credentials set")
        return
    if cmd == "apply-xray":
        print(json.dumps(reload_xray(), ensure_ascii=False))
        return
    if cmd == "xray-status":
        print(json.dumps(request("status"), ensure_ascii=False))
        return
    if cmd == "serve":
        if not hub.AUTH_FILE.is_file():
            raise SystemExit("No credentials. Run: docker compose run --rm --no-deps -T hub set-password-stdin admin")
        create_initial_config()
    hub.main()

if __name__ == "__main__":
    main()
