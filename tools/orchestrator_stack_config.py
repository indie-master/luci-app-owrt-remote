#!/usr/bin/env python3
"""Validate a root Compose stack configuration and render nginx files offline."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from orchestrator_edge_plan import WSS_PATH, proxy_locations
from orchestrator_setup_profiles import InvalidPlan, check, host_only, port, safe_write

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "docker"))
from manager import ALLOWED, Blocked, read_env, validate_settings

MODES = ("standalone", "external-stream", "external-only")
STACK_KEYS = {
    "RO_STACK_MODE", "RO_STACK_NAME", "RO_XRAY_IMAGE", "RO_XRAY_CONFIG_DIR",
    "RO_XRAY_ASSET_DIR", "RO_TLS_DIR", "RO_NGINX_IMAGE", "RO_NGINX_CONF",
    "RO_NGINX_HEALTH_PORT", "RO_HTTP_PORT", "RO_TLS_PORT", "RO_WSS_PORT",
    "RO_WSS_PATH", "RO_STREAM_ROUTES", "RO_NGINX_STREAM_MODULE",
}
IMAGE_DIGEST = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}")
STREAM_MODULE = "/usr/lib/nginx/modules/ngx_stream_module.so"


def nginx_host(value):
    host = host_only(value)
    check(value == host, "Require a canonical DNS name")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise InvalidPlan("SNI requires a DNS name")


def bind_path(value, key):
    path = Path(value)
    check(re.fullmatch(r"/[A-Za-z0-9._/-]+", value) and str(path.resolve()) == value,
          key + " must be a normalized absolute path without aliases")
    return path


def stack_plan(config):
    validate_settings({key: value for key, value in config.items() if key in ALLOWED}, unified=True)
    mode = config.get("RO_STACK_MODE")
    check(mode in MODES, "Invalid RO_STACK_MODE")
    name = config.get("RO_STACK_NAME", "router-orchestrator")
    check(re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", name), "Invalid RO_STACK_NAME")
    url = urlsplit(config["RO_PUBLIC_URL"])
    check(url.scheme == "https" and url.hostname and url.path == "" and
          not url.username and not url.password and not url.query and not url.fragment,
          "RO_PUBLIC_URL must be an HTTPS origin")
    host = nginx_host(url.hostname)
    public = url.port if url.port is not None else 443
    check(port(public, "HTTPS") and url.netloc in {host, f"{host}:{public}"},
          "Invalid HTTPS authority")
    hub = int(config["RO_PORT"])
    wss = port(config.get("RO_WSS_PORT", "18444"), "WSS")
    check(wss >= 1024, "WSS requires an unprivileged Xray listener port")
    wss_path = config.get("RO_WSS_PATH", "/owrt-ws/")
    check(wss_path != "/" and "//" not in wss_path and WSS_PATH.fullmatch(wss_path), "Invalid RO_WSS_PATH")
    paths = {key: bind_path(config.get(key, ""), key) for key in
             ("RO_STATE_DIR", "RO_XRAY_CONFIG_DIR", "RO_XRAY_ASSET_DIR", "RO_TLS_DIR")}
    check(all(not left.is_relative_to(right) for left in paths.values() for right in paths.values()
              if left != right) and len(set(paths.values())) == len(paths),
          "HUB data, Xray config, assets and TLS directories must be separate")
    check(IMAGE_DIGEST.fullmatch(config.get("RO_XRAY_IMAGE", "")),
          "RO_XRAY_IMAGE requires a pinned digest")
    http = tls = health = None
    nginx_conf = None
    module = config.get("RO_NGINX_STREAM_MODULE", "")
    check(module in {"", STREAM_MODULE} and (not module or mode == "standalone"),
          "Unsupported nginx stream module setting")
    used = [public, hub, wss]
    if mode != "external-only":
        check(IMAGE_DIGEST.fullmatch(config.get("RO_NGINX_IMAGE", "")),
              "RO_NGINX_IMAGE requires a pinned digest")
        nginx_conf = bind_path(config.get("RO_NGINX_CONF", ""), "RO_NGINX_CONF")
        tls = port(config.get("RO_TLS_PORT", "18445"), "internal TLS")
        health = port(config.get("RO_NGINX_HEALTH_PORT", "18189"), "nginx health")
        used.extend([tls, health])
    if mode == "standalone":
        http = port(config.get("RO_HTTP_PORT", "80"), "HTTP")
        used.append(http)
    routes = {}
    raw_routes = config.get("RO_STREAM_ROUTES", "")
    for rule in raw_routes.split(",") if raw_routes else ():
        alias, sep, target = rule.partition(":")
        check(sep == ":", "Use hostname:port for each stream route")
        alias = nginx_host(alias)
        check(alias != host and alias not in routes, "Duplicate or conflicting SNI")
        target_port = port(target, "stream upstream")
        check(target_port >= 1024, "Stream upstream requires an unprivileged Xray listener port")
        routes[alias] = target_port
    check(len(set(used)) == len(used) and not (set(used) & set(routes.values())),
          "Conflicting ingress or backend ports")
    return {"mode": mode, "name": name, "host": host, "public": public, "hub": hub,
            "wss": wss, "wss_path": wss_path, "http": http, "tls": tls,
            "health": health, "routes": routes, "nginx_conf": nginx_conf,
            "module": module}


def stream_entries(plan):
    return "".join(f"    {host} 127.0.0.1:{target};\n" for host, target in plan["routes"].items())


def hub_locations(plan):
    return proxy_locations(plan["wss_path"], upstream="127.0.0.1",
                           hub_port=plan["hub"], wss_port=plan["wss"])


def render_internal(plan):
    if plan["nginx_conf"] is None:
        return None
    prefix = ""
    redirect = ""
    if plan["mode"] == "standalone":
        if plan["module"]:
            prefix = f"load_module {plan['module']};\n"
        prefix += (
            "stream {\n  map $ssl_preread_server_name $ro_ingress {\n"
            f"    default 127.0.0.1:{plan['tls']};\n"
            + stream_entries(plan) +
            "  }\n  server {\n"
            f"    listen {plan['public']};\n"
            "    ssl_preread on;\n    proxy_pass $ro_ingress;\n  }\n}\n"
        )
        public_suffix = "" if plan["public"] == 443 else f":{plan['public']}"
        redirect = (
            f"  server {{ listen {plan['http']}; server_name _; "
            f"return 301 https://{plan['host']}{public_suffix}$request_uri; }}\n"
        )
    return (
        prefix + "worker_processes 2;\npid /var/run/nginx.pid;\n"
        "events { worker_connections 1024; }\nhttp {\n"
        "  access_log /dev/stdout;\n  error_log /dev/stderr warn;\n  server_tokens off;\n"
        f"  server {{ listen 127.0.0.1:{plan['health']}; "
        'location = /healthz { access_log off; return 200 "ok\\n"; } }\n'
        + redirect +
        f"  server {{ listen 127.0.0.1:{plan['tls']} ssl default_server; ssl_reject_handshake on; }}\n"
        "  server {\n"
        f"    listen 127.0.0.1:{plan['tls']} ssl;\n    server_name {plan['host']};\n"
        "    http2 on;\n"
        "    ssl_certificate /etc/router-orchestrator/tls/fullchain.pem;\n"
        "    ssl_certificate_key /etc/router-orchestrator/tls/privkey.pem;\n"
        "    ssl_protocols TLSv1.2 TLSv1.3;\n"
        + hub_locations(plan) + "  }\n}\n"
    )


def render_external(plan):
    files = {}
    if plan["mode"] == "external-only":
        files["external-http.snippet"] = (
            "# REVIEW ONLY. Include inside the HTTPS server for " + plan["host"] + ".\n"
            + hub_locations(plan)
        )
    if plan["mode"] == "external-stream" or (plan["mode"] == "external-only" and plan["routes"]):
        stream = "# REVIEW ONLY. Merge into the existing ssl_preread SNI map.\n"
        if plan["mode"] == "external-stream":
            stream += f"    {plan['host']} 127.0.0.1:{plan['tls']};\n"
        files["external-stream.snippet"] = stream + stream_entries(plan)
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = read_env(args.env_file, ALLOWED | STACK_KEYS)
        plan = stack_plan(config)
        output = args.output_dir
        check(output.is_absolute() and output == output.resolve(), "Require an absolute output without aliases")
        files = render_external(plan)
        internal = render_internal(plan)
        if internal is not None:
            check(plan["nginx_conf"] == output / "nginx.conf",
                  "RO_NGINX_CONF must point to nginx.conf in the new output directory")
            files["nginx.conf"] = internal
        output.mkdir(mode=0o700)
        for name, content in files.items():
            path = output / name
            safe_write(path, content)
            if name == "nginx.conf":
                # The nginx master has no DAC override. The enclosing directory stays private.
                path.chmod(0o644)
        print(json.dumps({"compose_file": str(ROOT / "docker-compose.yml"), "profile": plan["mode"],
                          "project_name": plan["name"], "deployment_status": "REVIEW_ONLY"}))
        return 0
    except (Blocked, InvalidPlan, OSError, ValueError) as exc:
        print("BLOCKED:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
