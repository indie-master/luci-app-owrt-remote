"""Strict, side-effect-free transport profile renderer for router management.

This module deliberately does not mutate /etc/xray, restart containers,
create TLS certificates, or replace existing router identities. Deploying a
profile is a separate authenticated, transactional operation.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

SUPPORTED = ("tcp", "wss", "reality-tcp", "xhttp", "grpc")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHORT_ID_RE = re.compile(r"^(?:[0-9a-fA-F]{2}){0,8}$")


class ProfileError(ValueError):
    pass


def _required_string(data: dict, field: str, *, maximum: int = 255) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProfileError(f"invalid {field}")
    return value


def _host(value: str) -> str:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not _HOST_RE.fullmatch(value) or ".." in value or value.startswith("-"):
            raise ProfileError("invalid server_name")
    return value


def _path(value: str) -> str:
    if not value.startswith("/") or len(value) > 256 or any(
        ord(char) < 33 or ord(char) > 126 or char in "?#" for char in value
    ):
        raise ProfileError("invalid path")
    return value


def _port(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise ProfileError("invalid port")
    return value


def _name(value: str, field: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise ProfileError(f"invalid {field}")
    return value


def render_stream_settings(profile: dict[str, Any]) -> dict[str, Any]:
    """Render streamSettings only, with no hidden fallback transport.

    TLS material must already be provisioned and is referenced by fixed,
    explicit paths. REALITY private keys remain server-side.
    """
    if not isinstance(profile, dict):
        raise ProfileError("profile must be an object")
    transport = _required_string(profile, "transport", maximum=32)
    if transport not in SUPPORTED:
        raise ProfileError("unsupported transport")
    # The legacy TCP transport is intentionally unchanged.
    if transport == "tcp":
        if profile.get("tls") or profile.get("reality"):
            raise ProfileError("legacy TCP must not silently enable encryption")
        return {"network": "tcp", "security": "none"}

    server_name = _host(_required_string(profile, "server_name"))
    if transport == "reality-tcp":
        private_key = _required_string(profile, "private_key", maximum=128)
        public_dest = _host(_required_string(profile, "target_host"))
        target_port = _port(profile.get("target_port"))
        short_ids = profile.get("short_ids")
        if (not isinstance(short_ids, list) or not short_ids
                or len(short_ids) > 16 or
                any(not isinstance(item, str) or
                    not _SHORT_ID_RE.fullmatch(item) for item in short_ids) or
                len(set(short_ids)) != len(short_ids)):
            raise ProfileError("invalid short_ids")
        if not profile.get("server_names") == [server_name]:
            raise ProfileError("server_names must match the selected server_name")
        return {
            "network": "tcp", "security": "reality",
            "realitySettings": {
                "show": False,
                "dest": f"{public_dest}:{target_port}",
                "xver": 0,
                "serverNames": [server_name],
                "privateKey": private_key,
                "shortIds": short_ids
            }
        }

    certificate = _required_string(profile, "certificate_file")
    key = _required_string(profile, "key_file")
    # Explicit allowlist prevents pointing the renderer at arbitrary paths.
    if not (certificate.startswith("/etc/router-orchestrator/tls/")
            and key.startswith("/etc/router-orchestrator/tls/")
            and ".." not in certificate and ".." not in key):
        raise ProfileError("TLS assets must be inside the dedicated TLS directory")
    settings: dict[str, Any] = {
        "security": "tls",
        "tlsSettings": {
            "serverName": server_name,
            "certificates": [{
                "certificateFile": certificate, "keyFile": key
            }]
        }
    }
    if transport == "wss":
        settings.update({
            "network": "ws",
            "wsSettings": {"path": _path(_required_string(profile, "path"))}
        })
    elif transport == "xhttp":
        settings.update({
            "network": "xhttp",
            "xhttpSettings": {
                "path": _path(_required_string(profile, "path")),
                "mode": "auto"
            }
        })
    elif transport == "grpc":
        settings.update({
            "network": "grpc",
            "grpcSettings": {
                "serviceName": _name(
                    _required_string(profile, "service_name", maximum=64),
                    "service_name"
                )
            }
        })
    return settings


def validate_profiles(profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate a set of additions without touching an existing listener."""
    if not isinstance(profiles, list) or len(profiles) > 16:
        raise ProfileError("invalid profile list")
    ports: set[int] = set()
    names: set[str] = set()
    rendered = []
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ProfileError("invalid profile")
        port = _port(profile.get("port"))
        name = _name(_required_string(profile, "name", maximum=64), "name")
        if port in ports or name in names:
            raise ProfileError("duplicate port or name")
        ports.add(port)
        names.add(name)
        rendered.append({
            "name": name,
            "port": port,
            "streamSettings": render_stream_settings(profile)
        })
    return rendered
