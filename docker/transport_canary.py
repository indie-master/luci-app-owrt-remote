"""Pure additive shadow-reverse canary builder.

No DB, files, docker or network. Never alters the currently deployed router
listener, UUIDs, portal ports or routing, and never promotes a pilot.
"""
from __future__ import annotations

import copy
import re
import uuid

from transport_profiles import ProfileError, render_stream_settings, _port

TAG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RESERVED_PORTS = {80, 443, 8088, 18089, 18443, 18444, 18445}


def build_shadow_canary(live: dict, profile: dict, router: dict,
                        *, admin_test_port: int, ssh_test_port: int,
                        listen: str = "127.0.0.1") -> tuple[dict, dict]:
    """Build server candidate + router pilot description.

    Server clients use existing identity UUIDs but distinct pilot reverse tags.
    A separate router Xray pilot must use those tags to establish its shadow
    tunnels. No login or existing router configuration is changed here.
    """
    if not isinstance(live, dict) or not isinstance(profile, dict) or not isinstance(router, dict):
        raise ProfileError("invalid pilot input")
    if listen not in ("127.0.0.1", "0.0.0.0"):
        raise ProfileError("unsupported pilot bind address")
    identity = router.get("id")
    name = profile.get("name")
    if not isinstance(identity, str) or not TAG_RE.fullmatch(identity):
        raise ProfileError("invalid router ID")
    if not isinstance(name, str) or not TAG_RE.fullmatch(name):
        raise ProfileError("invalid pilot profile name")
    if len(identity) + len(name) > 75:
        raise ProfileError("pilot tag too long")
    try:
        admin_uuid = str(uuid.UUID(router["vless_uuid"]))
        ssh_uuid = str(uuid.UUID(router["ssh_vless_uuid"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ProfileError("invalid router UUIDs") from None
    if admin_uuid == ssh_uuid:
        raise ProfileError("admin and SSH UUIDs must be distinct")
    for key in ("admin_host", "ssh_host"):
        if not isinstance(router.get(key), str) or not router[key]:
            raise ProfileError("missing router redirect target")
    admin_port = _port(router.get("admin_port"))
    ssh_port = _port(router.get("ssh_port"))
    listener = _port(profile.get("port"))
    test_ports = [_port(admin_test_port), _port(ssh_test_port)]
    if len({listener, *test_ports}) != 3:
        raise ProfileError("pilot listener/portal port collision")

    inbounds = live.get("inbounds")
    rules = live.get("routing", {}).get("rules") if isinstance(live.get("routing"), dict) else None
    if not isinstance(inbounds, list) or not isinstance(rules, list):
        raise ProfileError("invalid baseline Xray config")
    old_ports = {item.get("port") for item in inbounds if isinstance(item, dict)}
    old_tags = {item.get("tag") for item in inbounds if isinstance(item, dict)}
    if any(port in old_ports or port in RESERVED_PORTS for port in (listener, *test_ports)):
        raise ProfileError("pilot port conflicts with active service")

    prefix = "pilot-" + identity + "-" + name
    inbound_tag = prefix + "-in"
    entry_tag = prefix + "-entry"
    ssh_tag = prefix + "-ssh"
    admin_reverse = prefix + "-reverse"
    ssh_reverse = prefix + "-reverse-ssh"
    tags = (inbound_tag, entry_tag, ssh_tag)
    if any(len(tag) > 128 or tag in old_tags for tag in tags):
        raise ProfileError("pilot tag collides with existing Xray inbound")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ProfileError("invalid baseline routing rule")
        if any(tag in (rule.get("inboundTag") or []) for tag in tags):
            raise ProfileError("pilot routing tag already exists")

    result = copy.deepcopy(live)
    stream = render_stream_settings(profile)
    result["inbounds"].append({
        "tag": inbound_tag,
        "listen": listen,
        "port": listener,
        "protocol": "vless",
        "settings": {
            "decryption": "none",
            "clients": [{
                "id": admin_uuid,
                "email": prefix + "@owrt-remote",
                "reverse": {"tag": admin_reverse},
            }, {
                "id": ssh_uuid,
                "email": prefix + "-ssh@owrt-remote",
                "reverse": {"tag": ssh_reverse},
            }],
        },
        "streamSettings": stream,
    })
    for tag, port, host, target_port in (
        (entry_tag, test_ports[0], router["admin_host"], admin_port),
        (ssh_tag, test_ports[1], router["ssh_host"], ssh_port),
    ):
        result["inbounds"].append({
            "tag": tag, "listen": "127.0.0.1", "port": port,
            "protocol": "tunnel",
            "settings": {
                "allowedNetwork": "tcp",
                "rewriteAddress": host, "rewritePort": target_port,
            }
        })
    result["routing"]["rules"].extend([
        {"type": "field", "inboundTag": [entry_tag],
         "outboundTag": admin_reverse},
        {"type": "field", "inboundTag": [ssh_tag],
         "outboundTag": ssh_reverse},
    ])
    pilot = {
        "router_id": identity,
        "name": name,
        "transport": profile["transport"],
        "remote_port": listener,
        "admin_test_port": test_ports[0],
        "ssh_test_port": test_ports[1],
        "reverse_tag": admin_reverse,
        "ssh_reverse_tag": ssh_reverse,
        "vless_uuid": admin_uuid,
        "ssh_vless_uuid": ssh_uuid,
        "status": "STAGED_ONLY",
    }
    return result, pilot


def assert_baseline_retained(original: dict, candidate: dict) -> None:
    """Ensure all original inbounds and routing entries are byte-level-equal."""
    original_in = original.get("inbounds")
    candidate_in = candidate.get("inbounds")
    original_rules = original.get("routing", {}).get("rules")
    candidate_rules = candidate.get("routing", {}).get("rules")
    if not isinstance(original_in, list) or not isinstance(candidate_in, list):
        raise ProfileError("invalid inbounds")
    if not isinstance(original_rules, list) or not isinstance(candidate_rules, list):
        raise ProfileError("invalid routing")
    if candidate_in[:len(original_in)] != original_in:
        raise ProfileError("candidate changed an existing listener")
    if candidate_rules[:len(original_rules)] != original_rules:
        raise ProfileError("candidate changed existing route")
