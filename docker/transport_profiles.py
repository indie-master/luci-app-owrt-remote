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
        settings["tlsSettings"]["alpn"] = ["h2"]
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

def render_router_outbound(profile: dict[str, Any], *, router_uuid: str,
                           remote_host: str, reverse_tag: str = "reverse-in") -> dict[str, Any]:
    """Generate a *candidate* OpenWrt Xray VLESS outbound, never deploy it.

    Use the custom Xray Reverse VLESS settings syntax used by this project,
    not standard VLESS vnext (which cannot establish a reverse bridge).
    Server REALITY private key is never copied into the client structure.
    """
    import uuid as uuid_module
    try:
        identity = str(uuid_module.UUID(router_uuid))
    except (ValueError, AttributeError, TypeError):
        raise ProfileError("invalid router_uuid") from None
    host = _host(remote_host)
    port = _port(profile.get("port"))
    name = _name(_required_string(profile, "name", maximum=64), "name")
    server = render_stream_settings(profile)
    transport = profile["transport"]
    if transport == "reality-tcp":
        public_key = _required_string(profile, "public_key", maximum=80)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", public_key):
            raise ProfileError("invalid REALITY public key")
        stream = {
            "network": "tcp", "security": "reality",
            "realitySettings": {
                "serverName": profile["server_name"],
                "fingerprint": "chrome",
                "publicKey": public_key,
                "shortId": profile["short_ids"][0],
            },
        }
    elif transport == "tcp":
        stream = {"network": "tcp", "security": "none"}
    else:
        stream = {
            "network": server["network"],
            "security": "tls",
            "tlsSettings": {
                "serverName": profile["server_name"],
                "allowInsecure": False,
            }
        }
        if transport == "wss":
            stream["wsSettings"] = {
                "path": server["wsSettings"]["path"],
                "headers": {"Host": profile["server_name"]}
            }
        elif transport == "xhttp":
            stream["xhttpSettings"] = dict(server["xhttpSettings"])
        elif transport == "grpc":
            stream["grpcSettings"] = dict(server["grpcSettings"])
            stream["tlsSettings"]["alpn"] = ["h2"]
    return {
        "tag": "ro-control-" + name,
        "protocol": "vless",
        "settings": {
            "address": host,
            "port": port,
            "id": identity,
            "encryption": "none",
            "flow": "",
            "reverse": {"tag": _name(reverse_tag, "reverse_tag")},
        },
        "streamSettings": stream,
    }

