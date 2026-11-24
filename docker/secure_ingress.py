"""Pure SNI ingress planner. No nginx writes, certificate reads or reloads.

Existing ingress ownership must be supplied by the operator or a trusted
host inventory. One public port/SNI combination selects one TCP upstream;
REALITY is never TLS-terminated by nginx.
"""
from __future__ import annotations

import re
from transport_profiles import ProfileError, require_secure_profile, render_stream_settings, _port

DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
RESERVED = {80, 443, 8088, 18089, 18443, 18444, 18445}


def domain(value):
    if not isinstance(value, str) or not DOMAIN.fullmatch(value.lower()):
        raise ProfileError("ingress requires a literal DNS hostname")
    return value.lower()


def plan_ingress(profile, *, public_port=443, existing_routes=(), occupied_ports=()):
    """Produce a single candidate route for review by a privileged adapter.

    public hostname is the actual SNI sent by the router, not its dial address.
    With REALITY several serverNames may be accepted by one inbound, but every
    exposed name must have a collision-free route. No claim of DNS ownership.
    """
    require_secure_profile(profile)
    stream = render_stream_settings(profile)
    backend = _port(profile.get("port"))
    public = _port(public_port)
    if backend in RESERVED or backend in set(occupied_ports) or backend == public:
        raise ProfileError("backend listener conflicts with reserved or active port")
    sni = domain(profile.get("server_name"))
    names = ([domain(n) for n in stream["realitySettings"]["serverNames"]]
             if stream["security"] == "reality" else [sni])
    if len(names) != len(set(names)):
        raise ProfileError("duplicate ingress names")
    existing = {(int(route["public_port"]), domain(route["sni"]))
                for route in existing_routes}
    if any((public, name) in existing for name in names):
        raise ProfileError("SNI already belongs to another route; choose a dedicated name or port")
    routes = [{"public_port": public, "sni": name, "upstream": f"127.0.0.1:{backend}"}
              for name in names]
    return {"mode": "stream-sni-passthrough", "tls_termination": "xray",
            "listen": "127.0.0.1", "backend_port": backend,
            "routes": routes, "requires_certificate": stream["security"] == "tls",
            "streamSettings": stream, "applied": False}


def render_map_entries(plan):
    """Only render entries for a managed nginx stream map, not an nginx file."""
    return "".join(f"{domain(route['sni'])} 127.0.0.1:{_port(plan['backend_port'])};\n"
                   for route in plan["routes"])
