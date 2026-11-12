#!/usr/bin/env python3
"""Offline catalog and failover gating for Router Orchestrator transport profiles.

Individual routers may have their own ordered alternatives (WSS endpoints,
REALITY/RAW listeners). This module generates independent two-sided
candidate fragments, but NEVER restarts services or changes active tunnels.

Automatic switching requires a pre-staged router-side candidate and
end-to-end probes on BOTH LuCI and SSH channels; an unreachable router
cannot be reconfigured remotely just by changing the server.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys

from orchestrator_setup_profiles import (
    InvalidPlan, host_only, check, port, new_plan, paired_reverse, safe_write,
)

PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{1,47}$")
SECRET_REF = re.compile(r"^secrets/[a-zA-Z0-9_-]{2,64}$")
MODES = ("manual", "automatic_verified")
TYPES = ("wss_tls", "reality_raw", "legacy_tcp")
REQUIRED_ACCEPT = (
    "candidate_pre_staged",
    "server_config_valid",
    "router_config_valid",
    "both_reverse_connected",
    "luci_http_ok",
    "ssh_banner_ok",
    "heartbeat_fresh",
    "xray_version_compatible",
)


def profile_validate(profile: dict) -> dict:
    check(isinstance(profile, dict), "profile must be object")
    ident = profile.get("id")
    check(isinstance(ident, str) and PROFILE_ID.fullmatch(ident),
          "invalid profile ID")
    kind = profile.get("type")
    check(kind in TYPES, "unsupported transport profile")
    host = profile.get("endpoint_host")
    check(isinstance(host, str) and host_only(host) == host,
          "endpoint_host must be bare hostname or IP")
    port(profile.get("public_port"), "profile public port")
    check(profile.get("enabled") in (True, False), "profile must set enabled")
    if kind == "legacy_tcp":
        check(profile.get("locked") is True and profile.get("enabled") is True,
              "legacy TCP is preserved only as a locked existing profile")
        check(not any(k in profile for k in ("secret_ref", "ws_path", "reality_sni")),
              "legacy TCP cannot contain WSS/REALITY settings")
    elif kind == "wss_tls":
        check(profile.get("sni") == host_only(profile.get("sni", "")),
              "WSS SNI must be bare hostname")
        check(isinstance(profile.get("ws_path"), str) and
              re.fullmatch(r"/[a-zA-Z0-9/_-]*/", profile["ws_path"]) is not None,
              "invalid WSS path")
        check("secret_ref" not in profile and "reality_sni" not in profile,
              "WSS cannot carry REALITY secrets")
        listen = port(profile.get("server_local_port"), "WSS backend port")
        check(listen != profile["public_port"],
              "WSS backend and public port must be separate")
    else:
        check(profile.get("reality_sni") == host_only(profile.get("reality_sni", "")),
              "REALITY SNI must be bare hostname")
        target = profile.get("reality_target")
        check(isinstance(target, str) and ":" in target and
              not "://" in target and "/" not in target,
              "REALITY camouflage target must be host:port")
        target_host, target_port = target.rsplit(":", 1)
        check(target_host == host_only(target_host), "invalid camouflage host")
        port(target_port, "camouflage target port")
        check(profile.get("fingerprint") in ("chrome", "firefox", "safari"),
              "unsupported fingerprint")
        ref = profile.get("secret_ref")
        check(isinstance(ref, str) and SECRET_REF.fullmatch(ref) is not None,
              "REALITY profile must use a secret reference, never inline private keys")
        check(not any(k in profile for k in ("private_key", "public_key", "short_id")),
              "no REALITY secret material is permitted in public catalog")
        check(profile.get("public_port") != 443,
              "REALITY default must not occupy the existing HTTPS/WSS 443")
    return profile


def validate(catalog: dict) -> dict:
    check(isinstance(catalog, dict) and catalog.get("schema") == "transport-catalog/1",
          "invalid transport catalog schema")
    profiles = catalog.get("profiles")
    check(isinstance(profiles, list) and 1 <= len(profiles) <= 32,
          "profiles must contain 1..32 entries")
    for x in profiles:
        profile_validate(x)
    ids = [x["id"] for x in profiles]
    check(len(set(ids)) == len(ids), "duplicate profile IDs")
    by_id = {x["id"]: x for x in profiles}
    # Different paths can share the public TLS edge, but their Xray
    # backend ports must differ. Identical backends must use the same
    # path so the Nginx routing cannot become ambiguous.
    used_local: dict[int, str] = {}
    for p in profiles:
        if p["type"] != "wss_tls":
            continue
        listen = p["server_local_port"]
        old = used_local.get(listen)
        if old is None:
            used_local[listen] = p["ws_path"]
        else:
            check(old == p["ws_path"], "two WS paths cannot share one Xray backend")
    bindings = {}
    for p in profiles:
        if p["type"] == "reality_raw":
            bind = ("0.0.0.0", p["public_port"])
            check(bind not in bindings, "REALITY public listener port conflict")
            bindings[bind] = p["id"]
    routers = catalog.get("routers", {})
    check(isinstance(routers, dict), "routers must be object")
    for router_id, policy in routers.items():
        check(isinstance(router_id, str) and router_id and len(router_id) <= 100,
              "invalid router ID")
        check(isinstance(policy, dict) and policy.get("mode") in MODES,
              "invalid router failover mode")
        active = policy.get("active")
        check(active in by_id and by_id[active]["enabled"],
              "router active profile disabled/unknown")
        order = policy.get("fallback_order")
        check(isinstance(order, list) and
              len(set(order)) == len(order) and len(order) <= 12,
              "fallback order invalid/duplicated")
        check(all(p in by_id and p != active and by_id[p]["enabled"]
                  and by_id[p]["type"] != "legacy_tcp"
                  for p in order), "fallback contains unknown/disabled/legacy profile")
    check(catalog.get("automatic_restart") is False
          and catalog.get("deployable") is False,
          "profiles cannot auto-deploy from this preview catalog")
    return catalog


def starter_catalog(domain: str, *, backup_domain: str = "",
                    add_reality: bool = False) -> dict:
    domain = host_only(domain)
    back = host_only(backup_domain) if backup_domain else ""
    profiles = [{
        "id": "wss-primary", "type": "wss_tls", "enabled": True,
        "endpoint_host": domain, "public_port": 443,
        "sni": domain, "ws_path": "/owrt-ws/", "server_local_port": 18444,
    }]
    if back:
        profiles.append({
            "id": "wss-backup", "type": "wss_tls", "enabled": True,
            "endpoint_host": back, "public_port": 443,
            "sni": back, "ws_path": "/owrt-ws/", "server_local_port": 18444,
        })
    if add_reality:
        profiles.append({
            "id": "reality-pilot", "type": "reality_raw", "enabled": False,
            "endpoint_host": domain, "public_port": 24443,
            "reality_sni": "www.example.org",
            "reality_target": "www.example.org:443",
            "fingerprint": "chrome", "secret_ref": "secrets/reality-pilot",
        })
    catalog = {
        "schema": "transport-catalog/1",
        "profiles": profiles, "routers": {},
        "automatic_restart": False, "deployable": False,
        "status": "REVIEW_ONLY",
    }
    return validate(catalog)


def candidate_pair(catalog: dict, profile_id: str, router: dict,
                   secrets: dict | None = None) -> dict:
    validate(catalog)
    profiles = {p["id"]: p for p in catalog["profiles"]}
    check(profile_id in profiles, "profile not present")
    profile = profiles[profile_id]
    check(profile["enabled"], "profile is disabled")
    check(profile["type"] != "legacy_tcp",
          "no regeneration of old non-TLS client allowed")
    if profile["type"] == "wss_tls":
        # Same host is both SNI and endpoint in the current baseline
        # profile generator; differing SNI must be developed/tested
        # explicitly and is never silently normalized.
        check(profile["sni"] == profile["endpoint_host"],
              "separate endpoint/SNI requires an experimental renderer")
        plan = new_plan(profile["endpoint_host"], https_port=profile["public_port"],
                        wss_path=profile["ws_path"])
        plan["wss"]["xray_local_port"] = profile["server_local_port"]
        result = paired_reverse(plan, "wss_tls", router)
    else:
        plan = new_plan(
            profile["endpoint_host"], enable_reality=True,
            reality_port=profile["public_port"],
            reality_target=profile["reality_target"],
            reality_sni=profile["reality_sni"],
            reality_fingerprint=profile["fingerprint"])
        result = paired_reverse(plan, "reality_raw", router, secrets)
    result["profile_id"] = profile_id
    result["deployable"] = False
    result["warnings"] = ["Needs Nginx/external listener setup",
                          "Requires isolated reverse LuCI+SSH test",
                          "Never replace active configuration without rollback"]
    return result


def failover_decision(catalog: dict, router_id: str, *,
                      failures: int, cooldown_elapsed: bool,
                      probes: dict[str, dict[str, bool]]) -> dict:
    """Deterministic decision, with strictly validated multi-channel probes.

    The resulting action is an *instruction to a future privileged
    coordinator*, not something this preview tool executes.
    """
    validate(catalog)
    check(router_id in catalog["routers"], "unknown router")
    policy = catalog["routers"][router_id]
    active = policy["active"]
    base = {"router_id": router_id, "current": active, "target": None,
            "active_retained": True, "deployable": False}
    if policy["mode"] != "automatic_verified":
        return {**base, "action": "MANUAL_SELECT_REQUIRED"}
    check(isinstance(failures, int) and failures >= 0, "invalid failure count")
    if failures < 3:
        return {**base, "action": "KEEP_ACTIVE", "reason": "hysteresis"}
    if not cooldown_elapsed:
        return {**base, "action": "KEEP_ACTIVE", "reason": "cooldown"}
    for profile in policy["fallback_order"]:
        state = probes.get(profile, {})
        # A candidate not locally pre-staged on the OpenWrt router cannot
        # be activated by the remote HUB during a connectivity failure.
        if all(state.get(x) is True for x in REQUIRED_ACCEPT):
            return {**base, "target": profile,
                    "action": "PILOT_VERIFIED_SWITCH_PROPOSAL"}
    return {**base, "action": "NO_SAFE_ALTERNATIVE",
            "reason": "no router-prestaged candidate passed LuCI/SSH and heartbeat"}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sp = p.add_subparsers(dest="command", required=True)
    init = sp.add_parser("init", help="write private, immutable profile draft")
    init.add_argument("--domain", required=True)
    init.add_argument("--backup-domain", default="")
    init.add_argument("--add-reality", action="store_true")
    init.add_argument("--out", type=Path, required=True)
    listing = sp.add_parser("profiles")
    listing.add_argument("file", type=Path)
    generate = sp.add_parser("candidate", help="generate private separate server/router review fragments")
    generate.add_argument("file", type=Path)
    generate.add_argument("--profile", required=True)
    generate.add_argument("--router-file", required=True, type=Path)
    generate.add_argument("--secrets-file", type=Path,
                          help="required for REALITY; local root-private JSON only")
    generate.add_argument("--server-out", type=Path, required=True)
    generate.add_argument("--client-out", type=Path, required=True)
    args = p.parse_args()
    try:
        if args.command == "init":
            catalog = starter_catalog(
                args.domain, backup_domain=args.backup_domain,
                add_reality=args.add_reality)
            safe_write(args.out, json.dumps(catalog, indent=2) + "\n")
            print("PROFILE CATALOG STAGED: " + str(args.out))
        elif args.command == "profiles":
            catalog = validate(json.loads(args.file.read_text()))
            for profile in catalog["profiles"]:
                print(profile["id"], ":", profile["type"],
                      "@", profile["endpoint_host"], ":", profile["public_port"],
                      "enabled=" + str(profile["enabled"]).lower())
        else:
            catalog = validate(json.loads(args.file.read_text()))
            check(args.server_out.resolve() != args.client_out.resolve(),
                  "server/client output cannot be the same file")
            check(not args.server_out.exists() and not args.client_out.exists(),
                  "candidate output already exists; refuse overwrite")
            router = json.loads(args.router_file.read_text())
            secrets = None
            if args.secrets_file:
                check(args.secrets_file.is_file() and
                      not args.secrets_file.is_symlink() and
                      args.secrets_file.stat().st_mode & 0o077 == 0,
                      "REALITY keys file must be a private 0600 regular file")
                secrets = json.loads(args.secrets_file.read_text())
            pair = candidate_pair(catalog, args.profile, router, secrets)
            # Intentionally split private server key/UUID and client config.
            server = {
                "profile_id": pair["profile_id"],
                "server_inbound": pair["server_inbound"],
                "deployable": False,
                "status": "REVIEW_ONLY",
            }
            client = {
                "profile_id": pair["profile_id"],
                "router_outbounds": pair["router_outbounds"],
                "deployable": False,
                "status": "REVIEW_ONLY",
            }
            safe_write(args.server_out, json.dumps(server, indent=2) + "\n")
            try:
                safe_write(args.client_out, json.dumps(client, indent=2) + "\n")
            except BaseException:
                args.server_out.unlink(missing_ok=True)
                raise
            print("PRIVATE CANDIDATES CREATED — server/client isolated")
            print("NO Xray service or router changed")
        print("READ ONLY: no router/server configuration changes")
        return 0
    except (InvalidPlan, KeyError, OSError, TypeError, ValueError) as e:
        print("BLOCKED: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
