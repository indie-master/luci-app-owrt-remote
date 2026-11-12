#!/usr/bin/env python3
"""Offline configuration planning for Router Orchestrator.

This does NOT modify an installed HUB, any Xray JSON, OpenWrt, Nginx,
firewall, certificate, systemd service, or router database.
REALITY is experimental until BOTH installed Xray cores and both
reverse channels pass a dedicated integration pilot.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ROUTER_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
SHORT_ID = re.compile(r"^(?:[0-9a-fA-F]{2}){1,8}$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


class InvalidPlan(ValueError):
    pass


def check(condition: bool, why: str) -> None:
    if not condition:
        raise InvalidPlan(why)


def host_only(value: str) -> str:
    """Canonical host from a full URL, domain, IPv4 or bracketed IPv6."""
    v = str(value or "").strip()
    check(v and not any(c.isspace() for c in v), "empty or whitespace host")
    try:
        p = urlsplit(v if "://" in v else "//" + v)
        check(p.scheme in ("", "https", "http"), "unsupported URL scheme")
        check(not p.username and not p.password, "credentials in host not allowed")
        host = (p.hostname or "").lower().rstrip(".")
        check(host, "missing hostname")
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        check(len(host) <= 253 and all(HOST_LABEL.fullmatch(x) for x in host.split(".")),
              "invalid DNS host")
        return host
    except (ValueError, AttributeError) as e:
        if isinstance(e, InvalidPlan):
            raise
        raise InvalidPlan("invalid host syntax") from e


def port(value, label: str) -> int:
    try:
        n = int(value)
    except (ValueError, TypeError) as e:
        raise InvalidPlan(label + " must be a port number") from e
    check(1 <= n <= 65535, label + " must be between 1 and 65535")
    return n


def validate(plan: dict) -> dict:
    check(isinstance(plan, dict) and plan.get("schema") == 1, "unsupported plan schema")
    check(plan.get("domain") == host_only(plan.get("domain", "")),
          "domain must be hostname only, never http(s)://")
    domain = plan["domain"]
    public = urlsplit(str(plan.get("public_url", "")))
    check(public.scheme == "https" and public.hostname == domain
          and not public.username and not public.password
          and public.path in ("", "/") and not public.query and not public.fragment,
          "public_url must be HTTPS root of domain")
    # Validate public port even if the URL has an explicit one.
    try:
        public_port = public.port or 443
    except ValueError as e:
        raise InvalidPlan("invalid public URL port") from e
    port(public_port, "public HTTPS")
    hub = plan.get("hub")
    wss = plan.get("wss")
    reality = plan.get("reality")
    core = plan.get("xray")
    check(all(isinstance(x, dict) for x in (hub, wss, reality, core)),
          "missing hub/wss/reality/xray section")
    hub_port = port(hub.get("port"), "HUB")
    wss_internal = port(wss.get("xray_local_port"), "WSS local Xray")
    wss_external = port(wss.get("public_port"), "WSS public")
    reality_port = port(reality.get("port"), "REALITY public")
    check(hub.get("listen") in ("127.0.0.1", "::1"),
          "HUB must listen on loopback by default")
    check(wss.get("xray_listen") in ("127.0.0.1", "::1"),
          "WSS Xray backend must listen on loopback")
    check(wss.get("enabled") is True, "WSS must remain enabled during first rollout")
    check(wss_external == public_port,
          "WSS public port must match HTTPS frontend port")
    check(isinstance(wss.get("path"), str) and
          wss["path"].startswith("/") and
          wss["path"].endswith("/") and
          not any(c.isspace() for c in wss["path"]),
          "WSS path must start/end with slash and have no spaces")
    check(len({hub_port, wss_internal, public_port, reality_port, 18443}) == 5,
          "port collision (HUB, WSS backend, HTTPS, REALITY or legacy TCP)")
    check(reality.get("enabled") in (False, True), "invalid REALITY enabled flag")
    check(reality.get("listen") in ("0.0.0.0", "::"),
          "REALITY must use a dedicated listener")
    target = reality.get("target")
    check(isinstance(target, str) and ":" in target
          and not "://" in target and "/" not in target,
          "REALITY target must look like example.org:443")
    target_host, target_port = target.rsplit(":", 1)
    check(host_only(target_host) == target_host.lower(), "invalid REALITY target hostname")
    port(target_port, "REALITY target")
    sni = host_only(reality.get("server_name", ""))
    check(sni == reality["server_name"], "REALITY SNI must be bare hostname")
    names = reality.get("server_names")
    check(isinstance(names, list) and names
          and all(isinstance(x, str) and x == host_only(x) for x in names),
          "REALITY server_names must be an array of bare hosts")
    check(sni in names, "REALITY client SNI not allowed by server")
    check(reality.get("fingerprint") in ("chrome", "firefox", "safari"),
          "unsupported REALITY fingerprint")
    check(plan.get("default_profile") in ("wss_tls", "reality_raw"),
          "invalid default profile")
    if plan["default_profile"] == "reality_raw":
        check(reality["enabled"], "cannot default to disabled REALITY")
    for key in ("hub_version", "router_version"):
        check(isinstance(core.get(key), str) and VERSION.fullmatch(core[key]),
              "invalid Xray " + key + " pin")
    check(core.get("auto_update") is False,
          "automatic Xray upgrades forbidden before compatibility validation")
    return plan


def new_plan(domain: str, *, https_port: int = 443,
             reality_port: int = 24443,
             hub_version: str = "26.10.21",
             router_version: str = "26.10.21") -> dict:
    name = host_only(domain)
    https_port = port(https_port, "HTTPS")
    reality_port = port(reality_port, "REALITY")
    suffix = "" if https_port == 443 else ":" + str(https_port)
    plan = {
        "schema": 1,
        "domain": name,
        "public_url": "https://" + name + suffix,
        "hub": {"listen": "127.0.0.1", "port": 8088},
        "wss": {"enabled": True, "public_port": https_port,
                "path": "/owrt-ws/", "xray_listen": "127.0.0.1",
                "xray_local_port": 18444},
        "reality": {"enabled": False, "listen": "0.0.0.0",
                    "port": reality_port, "target": "www.example.org:443",
                    "server_names": ["www.example.org"],
                    "server_name": "www.example.org",
                    "fingerprint": "chrome"},
        "xray": {"hub_version": hub_version, "router_version": router_version,
                 "auto_update": False},
        "default_profile": "wss_tls",
        "status": "PREVIEW_ONLY",
    }
    return validate(plan)


def client_stream(plan: dict, profile: str, secrets: dict | None = None) -> dict:
    validate(plan)
    if profile == "wss_tls":
        return {
            "network": "ws", "security": "tls",
            "tlsSettings": {"serverName": plan["domain"], "allowInsecure": False,
                            "alpn": ["http/1.1"]},
            "wsSettings": {"path": plan["wss"]["path"], "host": plan["domain"]},
        }
    check(profile == "reality_raw", "unknown transport profile")
    check(plan["reality"]["enabled"], "REALITY is disabled in this plan")
    require_secrets(secrets)
    r = plan["reality"]
    return {
        "network": "raw", "security": "reality",
        "realitySettings": {
            "serverName": r["server_name"], "fingerprint": r["fingerprint"],
            "password": secrets["public_key"], "shortId": secrets["short_id"],
            "spiderX": "/",
        },
    }


def server_stream(plan: dict, profile: str, secrets: dict | None = None) -> dict:
    validate(plan)
    if profile == "wss_tls":
        return {"network": "ws", "security": "none",
                "wsSettings": {"path": plan["wss"]["path"]}}
    check(profile == "reality_raw", "unknown transport profile")
    check(plan["reality"]["enabled"], "REALITY disabled")
    require_secrets(secrets)
    r = plan["reality"]
    return {
        "network": "raw", "security": "reality",
        "realitySettings": {
            "show": False, "target": r["target"],
            "serverNames": r["server_names"],
            "privateKey": secrets["private_key"],
            "shortIds": [secrets["short_id"]],
        },
    }


def require_secrets(secrets: dict | None) -> None:
    check(isinstance(secrets, dict), "REALITY secrets are required from private local storage")
    check(all(isinstance(secrets.get(k), str) and secrets[k]
              for k in ("private_key", "public_key", "short_id")),
          "REALITY keypair/short ID incomplete")
    check(SHORT_ID.fullmatch(secrets["short_id"]) is not None,
          "short ID must be non-empty, even-length hex, up to 16 characters")
    for key in ("private_key", "public_key"):
        check(not any(c.isspace() for c in secrets[key])
              and len(secrets[key]) >= 32,
              "invalid REALITY key encoding")


def paired_reverse(plan: dict, profile: str, router: dict,
                   secrets: dict | None = None) -> dict:
    """Render a two-sided *fragment* for tests/staging, not a live Xray config."""
    validate(plan)
    rid = str(router.get("id", ""))
    check(ROUTER_ID.fullmatch(rid) is not None, "invalid router ID")
    import uuid
    uuids = []
    for key in ("vless_uuid", "ssh_vless_uuid"):
        try:
            item = str(uuid.UUID(router[key]))
        except (ValueError, KeyError, AttributeError) as e:
            raise InvalidPlan("invalid router " + key) from e
        uuids.append(item)
    check(uuids[0] != uuids[1], "duplicate router UUIDs")
    if profile == "wss_tls":
        destination_port = plan["wss"]["public_port"]
        listener = plan["wss"]["xray_listen"]
        inner_port = plan["wss"]["xray_local_port"]
    else:
        check(profile == "reality_raw", "unsupported transport")
        destination_port = plan["reality"]["port"]
        listener = plan["reality"]["listen"]
        inner_port = destination_port
    clients = []
    outbounds = []
    streams = client_stream(plan, profile, secrets)
    for n, uuid_value in enumerate(uuids):
        reverse = "reverse-" + rid + ("-ssh" if n else "")
        clients.append({"id": uuid_value, "email": rid + ("-ssh" if n else "")
                        + "@owrt-remote", "reverse": {"tag": reverse}})
        outbounds.append({
            "tag": "vps-ssh-interconn" if n else "vps-interconn",
            "protocol": "vless",
            "settings": {"address": plan["domain"], "port": destination_port,
                         "id": uuid_value, "encryption": "none", "flow": "",
                         "reverse": {"tag": "reverse-in-ssh" if n else "reverse-in"}},
            "streamSettings": streams,
        })
    return {
        "profile": profile,
        "server_inbound": {
            "tag": "owrt-remote-reality" if profile == "reality_raw"
                    else "owrt-remote-wss",
            "listen": listener, "port": inner_port, "protocol": "vless",
            "settings": {"clients": clients, "decryption": "none"},
            "streamSettings": server_stream(plan, profile, secrets),
        },
        "router_outbounds": outbounds,
        "server_portal_required": True,
        "deployable": False,
    }


def safe_write(path: Path, data: str) -> None:
    check(not path.exists() and not path.is_symlink(), "destination exists; refuse overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as dest:
            dest.write(data)
            dest.flush()
            os.fsync(dest.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="create a private offline setup plan")
    init.add_argument("--domain", default="")
    init.add_argument("--output", type=Path, required=True)
    init.add_argument("--https-port", type=int, default=443)
    init.add_argument("--reality-port", type=int, default=24443)
    init.add_argument("--hub-xray-version", default="26.10.21")
    init.add_argument("--router-xray-version", default="26.10.21")
    check_command = sub.add_parser("validate", help="read-only validation")
    check_command.add_argument("plan", type=Path)
    preview = sub.add_parser("preview", help="print credential-free plan summary")
    preview.add_argument("plan", type=Path)
    args = p.parse_args()
    try:
        if args.command == "init":
            domain = args.domain
            if not domain:
                check(sys.stdin.isatty(), "--domain required in noninteractive mode")
                domain = input("Domain for HTTPS HUB & WSS (e.g. hub.example.com): ").strip()
            plan = new_plan(domain, https_port=args.https_port,
                            reality_port=args.reality_port,
                            hub_version=args.hub_xray_version,
                            router_version=args.router_xray_version)
            safe_write(args.output, json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
            print("PLAN CREATED: " + str(args.output))
            print("ONLY STAGED: no HUB/Xray/Nginx/routers modified")
        else:
            plan = validate(json.loads(args.plan.read_text(encoding="utf-8")))
            print("VALID: schema=1, HTTPS=" + plan["public_url"])
            if args.command == "preview":
                print("WSS: " + plan["domain"] + ":" +
                      str(plan["wss"]["public_port"]) + plan["wss"]["path"] +
                      " -> 127.0.0.1:" + str(plan["wss"]["xray_local_port"]))
                print("REALITY: " + ("STAGED / NOT ENABLED" if not plan["reality"]["enabled"]
                                   else "EXPERIMENTAL / NEEDS KEYPAIR + PILOT"))
                print("XRAY PINS: hub=" + plan["xray"]["hub_version"] +
                      " router=" + plan["xray"]["router_version"])
                print("DEPLOYABLE: NO (preview only)")
        return 0
    except (InvalidPlan, OSError, ValueError, json.JSONDecodeError) as exc:
        print("BLOCKED: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
