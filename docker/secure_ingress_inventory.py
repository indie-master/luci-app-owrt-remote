"""Read-only verification of an operator-reviewed nginx ingress inventory.

The inventory is deliberately root-owned, not editable from a web request.
No external nginx route is claimed automatically. A complete nginx -T dump
is hashed in memory; its contents are not returned to the HUB.
"""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess

from secure_profile_store import STORE
from secure_publication import validate_inventory
from transport_profiles import ProfileError


def read_inventory(path=STORE / "ingress.json"):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1
                or info.st_size > 1024 * 1024):
            raise ProfileError("unsafe ingress inventory file")
        inventory = json.load(stream)
    validate_inventory(inventory)
    return inventory


def nginx_digest(mode):
    if mode == "external-stream":
        command = ["/usr/sbin/nginx", "-T"]
    elif mode == "standalone-stream":
        command = ["/usr/bin/docker", "exec", "router-orchestrator-nginx", "nginx", "-T"]
    else:
        raise ProfileError("unknown ingress mode")
    try:
        result = subprocess.run(command, capture_output=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        raise ProfileError("nginx configuration test unavailable or failed") from None
    if not result.stdout or len(result.stdout) > 16 * 1024 * 1024:
        raise ProfileError("nginx configuration dump is missing or exceeds limit")
    return hashlib.sha256(result.stdout).hexdigest()


def reviewed_inventory(path=STORE / "ingress.json"):
    try:
        inventory = read_inventory(path)
    except (OSError, ValueError) as exc:
        raise ProfileError("reviewed nginx inventory missing or invalid") from None
    if nginx_digest(inventory["mode"]) != inventory["nginx_config_sha256"]:
        raise ProfileError("nginx configuration changed since ownership review")
    return inventory


def live_tcp_ports():
    """Snapshot kernel TCP listeners, including non-Xray host services."""
    try:
        result = subprocess.run(["/usr/bin/ss", "-H", "-ltn"], capture_output=True,
                                text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        raise ProfileError("live TCP socket inventory unavailable") from None
    ports = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5:
            raise ProfileError("unrecognized TCP socket inventory")
        port = fields[3].rsplit(":", 1)[-1]
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ProfileError("unrecognized TCP listener port")
        ports.add(int(port))
    return ports


def approve_draft_names(artifact, *, path=STORE / "ingress.json"):
    """Explicit owner action: approve exact SNI names, not DNS or a live route.

    Only the already-reviewed nginx inventory is eligible. The lock serializes
    inventory changes by this module; future publication must use it too.
    """
    from secure_ingress import plan_ingress
    from secure_publication_transaction import atomic
    path = Path(path)
    fd = os.open(path.parent / "ingress.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ProfileError("unsafe ingress lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inventory = reviewed_inventory(path)
        if inventory["mode"] != "external-stream":
            raise ProfileError("managed external ingress not configured")
        if artifact["public_port"] != inventory["public_port"]:
            raise ProfileError("public port differs from reviewed ingress")
        plan = plan_ingress(artifact["profile"], public_port=artifact["public_port"],
                           existing_routes=inventory["existing_routes"] + inventory["managed_routes"],
                           occupied_ports=set(inventory["occupied_ports"]) | live_tcp_ports())
        names = {route["sni"] for route in plan["routes"]}
        updated = dict(inventory, allowed_names=sorted(set(inventory["allowed_names"]) | names))
        validate_inventory(updated)
        if reviewed_inventory(path) != inventory:
            raise ProfileError("ingress inventory changed during approval")
        atomic(path, json.dumps(updated, indent=2).encode())
        return {"approved_names": sorted(names), "applied": False, "published": False}
    finally:
        os.close(fd)
