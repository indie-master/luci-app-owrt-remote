"""Build a real, isolated two-link reverse interoperability probe.

No production UUIDs, configs or listeners are used. The caller runs both
cores and a private nginx stream listener and verifies unpredictable nonce
responses on the two reverse portals. This is not hardware acceptance.
"""
from __future__ import annotations

import copy
import re
import uuid

from transport_canary import build_shadow_canary
from transport_profiles import ProfileError, require_secure_profile, render_router_outbound, _port


def needs_final_rules(version):
    match = re.match(r"^Xray (\d+)\.(\d+)\.\d+\b", version)
    if not match:
        raise ProfileError("unrecognized Xray version")
    return tuple(map(int, match.groups())) >= (26, 5)


def build_probe(profile, *, ingress_port, admin_port, ssh_port,
                admin_target, ssh_target, core_version, proxy_protocol):
    require_secure_profile(profile)
    ports = [_port(p) for p in (profile["port"], ingress_port, admin_port,
                                ssh_port, admin_target, ssh_target)]
    if len(set(ports)) != len(ports) or min(ports) < 1024:
        raise ProfileError("isolated probe ports must be distinct and unprivileged")
    if type(proxy_protocol) is not bool:
        raise ProfileError("invalid PROXY protocol flag")
    profile = dict(profile, name="trial")
    router = {"id":"probe","admin_host":"127.0.0.1","ssh_host":"127.0.0.1",
              "admin_port":admin_target,"ssh_port":ssh_target,
              "vless_uuid":str(uuid.uuid4()),"ssh_vless_uuid":str(uuid.uuid4())}
    baseline = {"log":{"loglevel":"warning"}, "inbounds":[],
                "outbounds":[{"protocol":"blackhole","tag":"deny"}],
                "routing":{"rules":[]}}
    server, pilot = build_shadow_canary(baseline, profile, router,
                        admin_test_port=admin_port, ssh_test_port=ssh_port)
    if proxy_protocol:
        server["inbounds"][0]["streamSettings"]["sockopt"] = {"acceptProxyProtocol":True}
    # Reverse transport and credentials match real OpenWrt. The two local
    # Freedom routes are restricted to the two temporary loopback targets.
    client = {"log":{"loglevel":"warning"},"inbounds":[],
              "outbounds":[],"routing":{"rules":[]}}
    outbound_profile = dict(profile, port=ingress_port)
    for role, identity, tag, target in (
        ("admin",router["vless_uuid"],pilot["reverse_tag"],admin_target),
        ("ssh",router["ssh_vless_uuid"],pilot["ssh_reverse_tag"],ssh_target)):
        route = "local-"+role
        settings = {"redirect":"127.0.0.1:"+str(target)}
        if needs_final_rules(core_version):
            settings["finalRules"] = [{"action":"allow","network":"tcp",
                                       "ip":["127.0.0.1/32"],"port":str(target)}]
        client["outbounds"].append({"tag":route,"protocol":"freedom","settings":settings})
        outbound = render_router_outbound(outbound_profile,router_uuid=identity,
                                           remote_host="127.0.0.1",reverse_tag=tag)
        outbound["tag"] = "probe-"+role
        client["outbounds"].append(outbound)
        client["routing"]["rules"].append({"type":"field","inboundTag":[tag],"outboundTag":route})
    return server, client
