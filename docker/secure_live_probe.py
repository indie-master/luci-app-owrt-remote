"""Build transient live probe without modifying any existing router identity."""
from __future__ import annotations
import copy
from secure_reverse_probe import build_probe
from transport_profiles import ProfileError


def build_trial(final, profile, tag, *, public_port, dial_host,
                admin_port, ssh_port, admin_target, ssh_target,
                version, proxy_protocol):
    probe, client = build_probe(profile, ingress_port=public_port,
        admin_port=admin_port, ssh_port=ssh_port, admin_target=admin_target,
        ssh_target=ssh_target, core_version=version, proxy_protocol=proxy_protocol)
    trial = copy.deepcopy(final)
    selected = [i for i in trial["inbounds"] if i.get("tag") == tag]
    if len(selected) != 1:
        raise ProfileError("missing unique publication inbound")
    inbound = selected[0]
    test_inbound = probe["inbounds"][0]
    if (inbound["port"] != test_inbound["port"] or
        inbound["streamSettings"] != test_inbound["streamSettings"] or
        inbound["settings"] != {"decryption":"none","clients":[]}):
        raise ProfileError("trial inbound differs from final profile")
    inbound["settings"]["clients"] = test_inbound["settings"]["clients"]
    portals = probe["inbounds"][1:]
    existing_tags = {i.get("tag") for i in trial["inbounds"]}
    existing_ports = {i.get("port") for i in trial["inbounds"]}
    if any(i["tag"] in existing_tags or i["port"] in existing_ports for i in portals):
        raise ProfileError("trial portal collision")
    trial["inbounds"].extend(portals)
    # Test traffic must not be consumed by a pre-existing catch-all rule.
    # Existing rules preserve their order and their existing inbound tags.
    rules = trial["routing"]["rules"]
    temp_tags = {i["tag"] for i in portals}
    if any(temp_tags.intersection(rule.get("inboundTag") or []) for rule in rules):
        raise ProfileError("trial routing tag collision")
    trial["routing"]["rules"] = probe["routing"]["rules"] + rules
    for link in client["outbounds"]:
        if link["protocol"] == "vless":
            link["settings"]["address"] = dial_host
    return trial, client
