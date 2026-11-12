#!/usr/bin/env python3
"""Safe offline planner for Router Orchestrator's Docker HTTPS edge.

Two mutually exclusive deployment choices:
- external_nginx: existing host/container reverse-proxy owns 80/443 and certs.
- bundled_nginx: dedicated Nginx edge owns 80/443 with its own cert strategy.

No Docker/NGINX/systemd commands are run; nothing is deployed.
Generated snippets are review-only; existing Nginx vhosts aren't touched.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from orchestrator_setup_profiles import host_only, port, check, InvalidPlan, safe_write

NGINX_MODES = ("external_nginx", "bundled_nginx")
TLS_MODES = ("external_tls", "existing_pem", "existing_letsencrypt", "acme_http01", "acme_dns01")
ABS_PATH = re.compile(r"^/[a-zA-Z0-9._/-]+$")
WSS_PATH = re.compile(r"^/[a-zA-Z0-9/_-]*/$")
FQDN_LABEL = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.-]*$")


def _safe_path(value: object, label: str) -> str:
    check(isinstance(value, str) and ABS_PATH.fullmatch(value)
          and "//" not in value and "/../" not in value
          and not value.endswith("/.."),
          label + " must be a normalized absolute path")
    return value


def new_edge_plan(domain: str, *, mode: str = "external_nginx",
                  tls_mode: str | None = None,
                  https_port: int = 443, hub_port: int = 18088,
                  wss_port: int = 28444, wss_path: str = "/owrt-ws/",
                  cert_path: str = "", key_path: str = "",
                  cert_name: str = "",
                  acme_email: str = "", dns_provider: str = "") -> dict:
    host = host_only(domain)
    https_port = port(https_port, "HTTPS")
    plan = {
        "schema": "edge-plan/1",
        "domain": host,
        "public_url": "https://" + host + (":" + str(https_port) if https_port != 443 else ""),
        "nginx_mode": mode,
        "tls_mode": tls_mode or ("external_tls" if mode == "external_nginx" else "acme_http01"),
        "public_https_port": https_port,
        "public_http_port": 80,
        "hub_host_port": hub_port,
        "wss_host_port": wss_port,
        "hub_container_port": 8088,
        "wss_container_port": 18444,
        "wss_path": wss_path,
        "cert_path": cert_path,
        "key_path": key_path,
        "cert_name": cert_name or (host if (tls_mode or "") == "existing_letsencrypt" else ""),
        "acme_email": acme_email,
        "dns_provider": dns_provider,
        "automatic_nginx_overwrite": False,
        "automatic_xray_restart": False,
        "deployment_status": "REVIEW_ONLY",
    }
    return validate(plan)


def validate(plan: dict) -> dict:
    check(isinstance(plan, dict) and plan.get("schema") == "edge-plan/1",
          "invalid edge plan schema")
    domain = host_only(plan.get("domain", ""))
    check(plan["domain"] == domain, "domain must be a bare canonical hostname")
    check(plan.get("nginx_mode") in NGINX_MODES, "invalid Nginx deployment mode")
    check(plan.get("tls_mode") in TLS_MODES, "invalid TLS strategy")
    mode = plan["nginx_mode"]
    tls = plan["tls_mode"]
    check((mode == "external_nginx" and tls == "external_tls") or
          (mode == "bundled_nginx" and tls in ("existing_pem", "existing_letsencrypt", "acme_http01", "acme_dns01")),
          "TLS strategy incompatible with Nginx mode")
    check(plan.get("automatic_nginx_overwrite") is False
          and plan.get("automatic_xray_restart") is False,
          "automatic production changes are prohibited")
    check(plan.get("deployment_status") == "REVIEW_ONLY",
          "rendered edge plan cannot self-activate")
    https = port(plan.get("public_https_port"), "HTTPS")
    http = port(plan.get("public_http_port"), "HTTP")
    hub = port(plan.get("hub_host_port"), "HUB host-bound")
    wss = port(plan.get("wss_host_port"), "WSS host-bound")
    hub_inner = port(plan.get("hub_container_port"), "HUB internal")
    wss_inner = port(plan.get("wss_container_port"), "WSS internal")
    check(hub_inner != wss_inner and len({https, http, hub, wss}) == 4,
          "duplicate public/backend port")
    # Current compose/standalone templates use these internal ports,
    # matching existing HUB code; custom ports require a separate adapter.
    check(hub_inner == 8088 and wss_inner == 18444,
          "unsupported internal HUB/WSS ports")
    expected_url = "https://" + domain + (":" + str(https) if https != 443 else "")
    check(plan.get("public_url") == expected_url,
          "public HTTPS URL must match domain and port")
    check(isinstance(plan.get("wss_path"), str) and
          WSS_PATH.fullmatch(plan["wss_path"]) is not None,
          "WSS path has unsafe characters; must start/end with /")
    if tls == "existing_pem":
        check(not plan.get("cert_name"), "cert_name applies only to certbot directory")
        _safe_path(plan.get("cert_path"), "TLS certificate")
        _safe_path(plan.get("key_path"), "TLS private key")
        check(plan["cert_path"] != plan["key_path"],
              "certificate and private key cannot be identical")
    elif tls == "existing_letsencrypt":
        # Mount the complete tree, including live/ and archive/ symlinks.
        # Mounting individual certbot-renewed PEM files can pin stale inodes.
        _safe_path(plan.get("cert_path"), "existing Let's Encrypt directory")
        name = plan.get("cert_name")
        check(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) is not None
              and ".." not in name and name not in (".", ".."),
              "invalid certificate name")
        check(not plan.get("key_path"),
              "existing_letsencrypt uses the whole certificate tree")
    else:
        check(not plan.get("cert_path") and not plan.get("key_path")
              and not plan.get("cert_name"),
              "certificate paths are not applicable")
    if tls in ("acme_http01", "acme_dns01"):
        email = str(plan.get("acme_email") or "")
        check("@" in email and not any(x.isspace() for x in email)
              and len(email) <= 254 and email.count("@") == 1,
              "ACME contact email required")
        check(not _is_ip(domain), "ACME must use an eligible DNS hostname")
    if tls == "acme_dns01":
        check(isinstance(plan.get("dns_provider"), str) and
              re.fullmatch(r"[A-Za-z0-9_-]{2,32}", plan["dns_provider"]) is not None,
              "DNS-01 requires explicit provider plugin")
    else:
        check(not plan.get("dns_provider"),
              "DNS provider only supported for DNS-01")
    return plan


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _locations(plan: dict, *, upstream: str) -> str:
    path = plan["wss_path"]
    # This is a location snippet for manual import by an existing vhost,
    # not a fully managed arbitrary virtual host.
    return (
        f"    location ^~ {path} {{\n"
        f"        proxy_pass http://{upstream}:18444;\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Upgrade $http_upgrade;\n"
        '        proxy_set_header Connection "upgrade";\n'
        "        proxy_set_header Host $host;\n"
        "        proxy_read_timeout 3600s;\n"
        "        proxy_send_timeout 3600s;\n"
        "    }\n"
        "    location / {\n"
        f"        proxy_pass http://{upstream}:8088;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    }\n"
    )


def render_external_nginx_snippet(plan: dict) -> str:
    validate(plan)
    check(plan["nginx_mode"] == "external_nginx", "not external Nginx mode")
    # The host proxy connects to loopback-published Docker ports. Its
    # certificate and any existing vhost remain owned by the operator.
    lines = _locations(plan, upstream="127.0.0.1")
    lines = lines.replace("127.0.0.1:18444", "127.0.0.1:" + str(plan["wss_host_port"]))
    lines = lines.replace("127.0.0.1:8088", "127.0.0.1:" + str(plan["hub_host_port"]))
    return (
        "# REVIEW ONLY: include only in the chosen HTTPS server{} block.\n"
        "# Never replace an existing Nginx vhost automatically.\n"
        "# Existing Nginx must handle TLS and the public HTTPS listener.\n" + lines
    )


def render_bundled_nginx(plan: dict) -> str:
    validate(plan)
    check(plan["nginx_mode"] == "bundled_nginx", "not bundled Nginx mode")
    domain = plan["domain"]
    if plan["tls_mode"] == "existing_pem":
        cert, key = "/run/ro-tls/fullchain.pem", "/run/ro-tls/privkey.pem"
    else:
        cert_name = plan["cert_name"] if plan["tls_mode"] == "existing_letsencrypt" else domain
        cert = f"/etc/letsencrypt/live/{cert_name}/fullchain.pem"
        key = f"/etc/letsencrypt/live/{cert_name}/privkey.pem"
    https = int(plan["public_https_port"])
    suffix = "" if https == 443 else ":" + str(https)
    return (
        "# REVIEW ONLY: Nginx HTTPS stage starts AFTER a valid certificate exists.\n"
        "server {\n"
        "    listen 80;\n"
        f"    server_name {domain};\n"
        "    location ^~ /.well-known/acme-challenge/ {\n"
        "        root /var/www/acme;\n"
        "        try_files $uri =404;\n"
        "    }\n"
        f"    location / {{ return 301 https://$host{suffix}$request_uri; }}\n"
        "}\n"
        "server {\n"
        f"    listen {https} ssl;\n"
        f"    server_name {domain};\n"
        f"    ssl_certificate {cert};\n"
        f"    ssl_certificate_key {key};\n"
        "    ssl_protocols TLSv1.2 TLSv1.3;\n"
        + _locations(plan, upstream="ro-core") +
        "}\n"
    )


def render_compose_plan(plan: dict) -> str:
    validate(plan)
    # This topology intentionally keeps HUB and Xray in the same network
    # namespace, preserving 127.0.0.1 reverse portal listeners for LuCI/SSH.
    # Xray WSS/HUB front-end ports must bind 0.0.0.0 INSIDE the isolated
    # container network; never publish them on all host interfaces.
    # Xray restart in Docker needs a privilege-separated lifecycle adapter:
    # the existing HUB's systemctl backend cannot be used as-is.
    nginx = plan["nginx_mode"] == "bundled_nginx"
    base = (
        "# REVIEW ONLY — not an executable production deployment.\n"
        "# Requires pinned images, lifecycle adapter and certificate bootstrap.\n"
        "services:\n"
        "  ro-core:\n"
        "    image: PINNED_XRAY_RUNTIME_IMAGE_REQUIRED\n"
        "    networks: [ro-internal]\n"
        "    volumes:\n"
        "      - ./data/xray:/etc/xray:ro\n"
        "      - ./data/hub:/var/lib/owrt-remote\n"
        "    ports:\n"
        f"      - \"127.0.0.1:{plan['hub_host_port']}:8088\"\n"
        f"      - \"127.0.0.1:{plan['wss_host_port']}:18444\"\n"
        "    restart: unless-stopped\n"
        "  hub:\n"
        "    image: PINNED_HUB_RUNTIME_IMAGE_REQUIRED\n"
        "    network_mode: service:ro-core\n"
        "    depends_on: [ro-core]\n"
        "    volumes:\n"
        "      - ./data/hub:/var/lib/owrt-remote\n"
        "    restart: unless-stopped\n"
    )
    if nginx:
        base += (
            "  nginx:\n"
            "    image: PINNED_NGINX_IMAGE_REQUIRED\n"
            "    networks: [ro-internal]\n"
            "    ports:\n"
            '      - "80:80"\n'
            f'      - "{plan["public_https_port"]}:{plan["public_https_port"]}"\n'
            "    volumes:\n"
            "      - ./nginx/orchestrator.conf:/etc/nginx/conf.d/default.conf:ro\n"
        )
        if plan["tls_mode"] == "existing_pem":
            base += (
                f"      - {plan['cert_path']}:/run/ro-tls/fullchain.pem:ro\n"
                f"      - {plan['key_path']}:/run/ro-tls/privkey.pem:ro\n"
            )
        elif plan["tls_mode"] == "existing_letsencrypt":
            # Selective live+archive mounts preserve the relative certbot
            # symlinks while restricting the container to ONE cert lineage.
            cert_root = plan["cert_path"].rstrip("/")
            cert_name = plan["cert_name"]
            base += (
                f"      - {cert_root}/live/{cert_name}:/etc/letsencrypt/live/{cert_name}:ro\n"
                f"      - {cert_root}/archive/{cert_name}:/etc/letsencrypt/archive/{cert_name}:ro\n"
                "      - ./acme/webroot:/var/www/acme:ro\n"
            )
        else:
            base += (
                "      - ./acme/certs:/etc/letsencrypt:ro\n"
                "      - ./acme/webroot:/var/www/acme:ro\n"
            )
        base += "    restart: unless-stopped\n"
    base += "networks:\n  ro-internal:\n    internal: false\n"
    return base


def preview(plan: dict) -> str:
    validate(plan)
    x = [
        "ROUTER ORCHESTRATOR EDGE / REVIEW ONLY",
        "Domain: " + plan["domain"],
        "HTTPS: " + plan["public_url"],
        "Proxy: " + plan["nginx_mode"],
        "TLS: " + plan["tls_mode"],
        "WSS: " + plan["wss_path"],
        "Docker host HUB: 127.0.0.1:" + str(plan["hub_host_port"]),
        "Docker host WSS: 127.0.0.1:" + str(plan["wss_host_port"]),
        "HUB+XRAY shared network namespace: REQUIRED",
        "Existing HUB systemctl backend: DOCKER ADAPTER REQUIRED",
        "Deployment: DISABLED (draft only)",
    ]
    if plan["nginx_mode"] == "bundled_nginx":
        x.append("Certificate bootstrap/renewal: REQUIRED BEFORE START")
    else:
        x.append("TLS certs: remain with existing proxy, no extra Nginx container")
    return "\n".join(x)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--domain", required=True)
    init.add_argument("--mode", choices=NGINX_MODES, default="external_nginx")
    init.add_argument("--tls", choices=TLS_MODES)
    init.add_argument("--email", default="")
    init.add_argument("--dns-provider", default="")
    init.add_argument("--cert", default="")
    init.add_argument("--key", default="")
    init.add_argument("--cert-name", default="",
                      help="certbot lineage name, e.g. cdnvc.xyz or example.org-001")
    init.add_argument("--output", type=Path, required=True)
    stage = sub.add_parser("stage")
    stage.add_argument("plan", type=Path)
    stage.add_argument("--output-dir", type=Path, required=True)
    show = sub.add_parser("preview")
    show.add_argument("plan", type=Path)
    args = p.parse_args()
    try:
        if args.command == "init":
            plan = new_edge_plan(
                args.domain, mode=args.mode, tls_mode=args.tls,
                acme_email=args.email, dns_provider=args.dns_provider,
                cert_path=args.cert, key_path=args.key,
                cert_name=args.cert_name)
            safe_write(args.output, json.dumps(plan, indent=2) + "\n")
            print("STAGED: " + str(args.output))
            print("NO changes to Docker/Nginx/Xray/HUB")
        else:
            plan = validate(json.loads(args.plan.read_text()))
            if args.command == "preview":
                print(preview(plan))
            else:
                path = args.output_dir
                check(not path.exists(), "destination exists; refuse overwrite")
                path.mkdir(parents=True, mode=0o700)
                safe_write(path / "compose.review.yml", render_compose_plan(plan))
                if plan["nginx_mode"] == "external_nginx":
                    safe_write(path / "nginx-external.snippet",
                               render_external_nginx_snippet(plan))
                else:
                    safe_write(path / "nginx-bundled.review.conf",
                               render_bundled_nginx(plan))
                safe_write(path / "README.review.txt", preview(plan) + "\n")
                print("PREVIEW ONLY: " + str(path))
                print("NO deploy, no nginx -s reload, no docker compose up")
        return 0
    except (InvalidPlan, OSError, KeyError, ValueError, json.JSONDecodeError) as e:
        print("BLOCKED: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
