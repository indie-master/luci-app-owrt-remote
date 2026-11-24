"""Read-only verification of an operator-reviewed nginx ingress inventory.

The inventory is deliberately root-owned, not editable from a web request.
No external nginx route is claimed automatically. A complete nginx -T dump
is hashed in memory; its contents are not returned to the HUB.
"""
from __future__ import annotations

import hashlib
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
