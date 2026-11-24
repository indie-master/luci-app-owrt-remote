"""Build an additive server publication candidate, never mutate live services.

External nginx ownership is an explicit, administrator-reviewed contract.
Its digest binds a candidate to the exact reviewed inventory; an inventory
is not proof of a running listener. A transaction worker must revalidate it.
"""
from __future__ import annotations
import copy
import hashlib
import json
import re
from secure_ingress import domain, plan_ingress
from transport_profiles import ProfileError, _port
from secure_profile_catalog import _identifier


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def validate_inventory(inventory):
    if not isinstance(inventory, dict) or set(inventory) != {
        "mode", "public_port", "allowed_names", "existing_routes",
        "occupied_ports", "managed_routes", "nginx_config_sha256"
    }:
        raise ProfileError("invalid reviewed ingress inventory")
    if inventory["mode"] not in ("external-stream", "standalone-stream"):
        raise ProfileError("unsupported ingress mode")
    public = _port(inventory["public_port"])
    names = inventory["allowed_names"]
    if not isinstance(names, list) or not names or len(names) > 128:
        raise ProfileError("no approved ingress names")
    allowed = {domain(n) for n in names}
    if len(allowed) != len(names):
        raise ProfileError("duplicate approved ingress name")
    ports = inventory["occupied_ports"]
    if not isinstance(ports, list) or len(ports) > 65535:
        raise ProfileError("invalid occupied port inventory")
    for port in ports:
        _port(port)
    stamp = inventory["nginx_config_sha256"]
    if not isinstance(stamp, str) or not re.fullmatch(r"[a-f0-9]{64}", stamp):
        raise ProfileError("invalid reviewed nginx digest")
    seen = set()
    for field in ("existing_routes", "managed_routes"):
        routes = inventory[field]
        if not isinstance(routes, list) or len(routes) > 4096:
            raise ProfileError("invalid ingress route inventory")
        for route in routes:
            expected = {"public_port", "sni"} | ({"upstream"} if field == "managed_routes" else set())
            if not isinstance(route, dict) or set(route) != expected:
                raise ProfileError("invalid inventory route")
            key = (_port(route["public_port"]), domain(route["sni"]))
            if key in seen:
                raise ProfileError("duplicate route in inventory")
            seen.add(key)
            if field == "managed_routes":
                if key[0] != public or key[1] not in allowed:
                    raise ProfileError("managed route outside reviewed ownership")
                upstream = route["upstream"]
                if not isinstance(upstream, str) or not re.fullmatch(r"127\.0\.0\.1:[0-9]{1,5}", upstream):
                    raise ProfileError("invalid managed upstream")
                _port(int(upstream.rsplit(":", 1)[1]))
    return public, allowed


def build_publication(live, artifact, inventory, *, profile_id, revision):
    _identifier(profile_id)
    if type(revision) is not int or revision < 1:
        raise ProfileError("invalid revision")
    public, allowed = validate_inventory(inventory)
    if not isinstance(artifact, dict) or set(artifact) != {"profile", "remote_host", "public_port"}:
        raise ProfileError("invalid private profile artifact")
    profile = artifact["profile"]
    if not isinstance(profile, dict):
        raise ProfileError("invalid private profile")
    if profile.get("name") != profile_id or artifact["public_port"] != public:
        raise ProfileError("profile identity or public port differs from reviewed ingress")
    inbounds = live.get("inbounds") if isinstance(live, dict) else None
    if not isinstance(inbounds, list) or any(not isinstance(i, dict) for i in inbounds):
        raise ProfileError("invalid baseline Xray inbounds")
    routes = inventory["existing_routes"] + inventory["managed_routes"]
    occupied = set(inventory["occupied_ports"])
    occupied.update(i["port"] for i in inbounds if type(i.get("port")) is int)
    plan = plan_ingress(profile, public_port=public, existing_routes=routes, occupied_ports=occupied)
    exposed = {r["sni"] for r in plan["routes"]}
    if not exposed <= allowed:
        raise ProfileError("SNI not explicitly approved for managed ingress")
    if plan["streamSettings"]["security"] == "reality":
        target = domain(profile["target_host"])
        # DNS aliases and local addresses also need a live resolver check
        # by the privileged worker. Reject obvious recursive target names.
        if profile["target_port"] == public and target in (allowed | {domain(artifact["remote_host"])}):
            raise ProfileError("REALITY target may loop into managed ingress")
    tag = f"ro-secure-{profile_id}-r{revision}"
    if len(tag) > 128 or any(i.get("tag") == tag for i in inbounds):
        raise ProfileError("candidate inbound tag already exists or is too long")
    candidate = copy.deepcopy(live)
    candidate["inbounds"].append({
        "tag": tag, "listen": "127.0.0.1", "port": plan["backend_port"],
        "protocol": "vless", "settings": {"decryption": "none", "clients": []},
        "streamSettings": plan["streamSettings"],
    })
    # Render the complete managed fragment; preserve previously managed routes.
    combined = sorted(inventory["managed_routes"] + plan["routes"], key=lambda r: r["sni"])
    fragment = "".join(f"{domain(r['sni'])} {r['upstream']};\n" for r in combined)
    return {"candidate": candidate, "fragment": fragment, "tag": tag,
            "baseline_sha256": digest(live), "inventory_sha256": digest(inventory),
            "artifact_sha256": digest(artifact), "routes": plan["routes"],
            "backend_port": plan["backend_port"], "public_port": public,
            "published": False, "applied": False}


def public_summary(plan):
    """Never expose the candidate Xray document (contains private keys)."""
    return {k: plan[k] for k in ("tag", "baseline_sha256", "inventory_sha256",
            "routes", "backend_port", "public_port", "published", "applied")}
