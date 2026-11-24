#!/usr/bin/env python3
"""Read-only external nginx onboarding report; never print raw nginx -T."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

CONFIG = Path("/etc/xray/owrt-remote.json")
HEADER = re.compile(r"^# configuration file (/[A-Za-z0-9_./-]+):$")
MAP = re.compile(r"^\s*map\s+\$ssl_preread_server_name\s+(\$[A-Za-z0-9_]+)\s*\{")
DIRECTIVE = re.compile(r"^\s*(listen|ssl_preread|proxy_pass|include|server_name)\s+([^;{}]+);")
SAFE = re.compile(r"^[A-Za-z0-9_./:$* \[\],=-]+$")
ENTRY = re.compile(r"^\s*([A-Za-z0-9_.*-]+)\s+([A-Za-z0-9_.$:\[\]-]+);\s*(?:#.*)?$")


def summarize(raw):
    files = {}
    current = None
    mapping = None
    for line in raw.decode("utf-8", errors="replace").splitlines():
        header = HEADER.fullmatch(line)
        if header:
            current = header[1]
            files.setdefault(current, {"routing": [], "sni_maps": []})
            mapping = None
            continue
        if current is None:
            continue
        entry = files[current]
        match = MAP.match(line)
        if match:
            mapping = {"variable":match[1], "entries":[], "includes":[], "unparsed":0}
            entry["sni_maps"].append(mapping)
            continue
        if mapping is not None:
            if line.strip().startswith("}"):
                mapping = None
                continue
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            directive = DIRECTIVE.match(line)
            if directive and directive[1] == "include" and SAFE.fullmatch(directive[2]):
                mapping["includes"].append(directive[2])
            else:
                pair = ENTRY.fullmatch(line)
                if pair:
                    mapping["entries"].append({"sni":pair[1], "upstream":pair[2]})
                else:
                    mapping["unparsed"] += 1
            continue
        directive = DIRECTIVE.match(line)
        if directive and SAFE.fullmatch(directive[2]):
            kind, value = directive.groups()
            # No HTTP proxy URLs or arbitrary header/secret directives.
            if kind != "proxy_pass" or value.startswith(("$", "127.0.0.1:", "[::1]:")):
                entry["routing"].append({"directive":kind, "value":value})
    return files


def main():
    result = subprocess.run(["/usr/sbin/nginx", "-T"], capture_output=True, timeout=20)
    if result.returncode or not result.stdout or len(result.stdout) > 16 * 1024 * 1024:
        raise SystemExit("NGINX TEST FAILED: inspect locally; raw output omitted")
    config = json.loads(CONFIG.read_bytes())
    sockets = subprocess.run(["ss","-H","-ltn"],capture_output=True,text=True,timeout=5,check=True)
    ports = set()
    for line in sockets.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            port = fields[3].rsplit(":",1)[-1]
            if port.isdigit():
                ports.add(int(port))
    print(json.dumps({
        "nginx_config_sha256":hashlib.sha256(result.stdout).hexdigest(),
        "files":summarize(result.stdout),
        "occupied_tcp_ports":sorted(ports),
        "xray_listeners":[{"tag":i.get("tag"),"listen":i.get("listen"),"port":i.get("port"),
                           "network":i.get("streamSettings",{}).get("network"),
                           "security":i.get("streamSettings",{}).get("security")}
                          for i in config.get("inbounds",[])],
        "applied":False,
        "note":"Review hints only; included map fragments, regex/wildcards and default routes require explicit ownership review."
    },ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
