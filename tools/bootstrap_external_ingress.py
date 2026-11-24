#!/usr/bin/env python3
"""One-time, empty managed map onboarding for a reviewed external nginx.

No reload, Xray restart, DNS change, firewall change or router update.
Refuses nontrivial/ambiguous stream layouts rather than rewriting them.
The empty include is syntax-tested before replacing nginx.conf; the old
file is kept in a private durable backup. Runtime still has the old map.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_ingress import domain
from secure_publication import validate_inventory
from secure_publication_transaction import atomic, read_regular, sha
from transport_profiles import ProfileError

MAIN = Path("/etc/nginx/nginx.conf")
MANAGED = Path("/etc/nginx/router-orchestrator-managed.map")
STORE = Path("/var/lib/router-orchestrator-secure-profiles")
NGINX = "/usr/sbin/nginx"

# Only a simple line-based stream layout is eligible for this bootstrap.
# nginx -T remains the authoritative syntax test; this is an ownership gate.
STREAM = re.compile(r"(?m)^[ \t]*stream[ \t]*\{[ \t]*(?:#[^\n]*)?$")
MAP = re.compile(r"(?m)^([ \t]*)map[ \t]+\$ssl_preread_server_name[ \t]+\$backend[ \t]*\{[ \t]*(?:#[^\n]*)?$")
SERVER = re.compile(r"(?m)^[ \t]*server[ \t]*\{[ \t]*(?:#[^\n]*)?$")
PAIR = re.compile(r"([A-Za-z0-9.-]+)[ \t]+(127\.0\.0\.1:[0-9]{1,5});")
DIRECTIVE = re.compile(r"([a-z_]+)[ \t]+([^;{}]+);")


def block_end(text, start):
    # Restrict these specific stream blocks to plain braces, no quoted
    # strings containing braces. Comments are excluded from brace counting.
    depth = 1
    offset = start
    for line in text[start:].splitlines(keepends=True):
        code = line.split("#", 1)[0]
        if '"' in code or "'" in code:
            raise ProfileError("quoted stream syntax needs manual ownership review")
        for index, char in enumerate(code):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    if code[index+1:].strip():
                        raise ProfileError("unsupported inline stream block syntax")
                    return offset + index
        offset += len(line)
    raise ProfileError("unterminated stream block")


def simple_directives(text):
    result = {}
    for line in text.splitlines():
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        match = DIRECTIVE.fullmatch(value)
        if not match:
            raise ProfileError("unrecognized stream directive; review required")
        key, val = match.groups()
        result.setdefault(key, []).append(val.strip())
    return result


def one(directives, name, default=None):
    values = directives.get(name, [])
    if len(values) > 1:
        raise ProfileError("ambiguous stream directive: " + name)
    return values[0] if values else default


def prepare(main, managed_path=MANAGED):
    text = main.decode("utf-8")
    streams = list(STREAM.finditer(text))
    if len(streams) != 1:
        raise ProfileError("expected exactly one explicit stream block")
    start = streams[0].end()
    finish = block_end(text, start)
    stream = text[start:finish]
    maps = list(MAP.finditer(stream))
    servers = list(SERVER.finditer(stream))
    if len(maps) != 1 or len(servers) != 1:
        raise ProfileError("expected one explicit SNI map and stream server")
    mapping, server = maps[0], servers[0]
    me = block_end(stream, mapping.end())
    se = block_end(stream, server.end())
    if mapping.start() >= server.start() or me >= server.start():
        raise ProfileError("unsupported stream block order")
    outer = simple_directives(stream[:mapping.start()] + stream[me+1:server.start()] + stream[se+1:])
    inner = simple_directives(stream[server.end():se])
    if "include" in outer or "include" in inner:
        raise ProfileError("stream includes need explicit ownership review")
    listen = one(inner, "listen", "")
    if listen.split() not in (["443"], ["443", "reuseport"], ["0.0.0.0:443"], ["0.0.0.0:443", "reuseport"]):
        raise ProfileError("expected reviewed public TCP/443 listener")
    if one(inner, "proxy_pass") != "$backend" or one(inner, "ssl_preread", one(outer,"ssl_preread","off")) != "on":
        raise ProfileError("expected SNI passthrough to reviewed map")
    if "ssl" in inner or "ssl_certificate" in inner:
        raise ProfileError("stream TLS termination is not supported")
    protocol = one(inner, "proxy_protocol", one(outer,"proxy_protocol","off"))
    if protocol not in ("on", "off"):
        raise ProfileError("invalid outgoing PROXY protocol")
    routes = {}
    for line in stream[mapping.end():me].splitlines():
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        pair = PAIR.fullmatch(value)
        if not pair:
            raise ProfileError("map includes, wildcards or regex require separate onboarding")
        name, upstream = pair.groups()
        name = name if name == "default" else domain(name)
        if name in routes or not 1 <= int(upstream.rsplit(":",1)[1]) <= 65535:
            raise ProfileError("duplicate or invalid map entry")
        routes[name] = upstream
    if routes.get("default") != "127.0.0.1:8443":
        raise ProfileError("unexpected default upstream; review required")
    # Insert immediately after the map opening line. Every old byte survives.
    pos = start + mapping.end()
    insertion = "\n" + mapping[1] + "    include " + str(managed_path) + ";"
    candidate = (text[:pos] + insertion + text[pos:]).encode()
    return candidate, routes, protocol == "on"


def run(args):
    try:
        result = subprocess.run(args, capture_output=True, timeout=20, check=True)
    except (OSError, subprocess.SubprocessError):
        raise ProfileError("command failed: " + " ".join(args[:2]) + "; raw output omitted") from None
    return result.stdout


def dump():
    raw = run([NGINX, "-T"])
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise ProfileError("nginx configuration dump invalid")
    return raw


def occupied_ports():
    result = run(["ss", "-H", "-ltn"])
    ports = set()
    for line in result.decode().splitlines():
        fields = line.split()
        if len(fields) >= 4:
            port = fields[3].rsplit(":",1)[-1]
            if port.isdigit():
                ports.add(int(port))
    return sorted(ports)


def approved_names(values, raw, routes):
    # Include literal HTTP vhost names, since moving one of those names
    # out of the default upstream would steal an existing site.
    occupied = set(routes) - {"default"}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        match = re.fullmatch(r"\s*server_name\s+([^;{}]+);\s*(?:#.*)?",line)
        if match:
            for name in match[1].split():
                if name in ("_", "localhost"):
                    continue
                occupied.add(domain(name))
    names = sorted({domain(n) for n in values})
    if any(n in occupied for n in names):
        raise ProfileError("approved SNI collides with existing route or HTTP vhost")
    return names, occupied


def secure_store():
    STORE.mkdir(mode=0o700, exist_ok=True)
    info = STORE.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ProfileError("unsafe private state directory")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--allow-name", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ProfileError("root required")
    raw = dump()
    if sha(raw) != args.expected_sha256:
        raise ProfileError("nginx changed since audit; no files modified")
    original, mode, uid, gid = read_regular(MAIN)
    if uid != 0 or mode & 0o022:
        raise ProfileError("nginx.conf must be root-owned and not group/world writable")
    candidate, routes, proxy = prepare(original)
    names, occupied = approved_names(args.allow_name, raw, routes)
    inventory = {"mode":"external-stream","public_port":443,"allowed_names":names,
                 "existing_routes":[{"public_port":443,"sni":n} for n in sorted(occupied)],
                 "occupied_ports":occupied_ports(),"managed_routes":[],
                 "nginx_config_sha256":sha(raw),"proxy_protocol":proxy}
    validate_inventory(inventory)
    print("AUDITED NGINX SHA256:",sha(raw))
    print("EXISTING SNI ROUTES:",len(routes)-1)
    print("DEFAULT:",routes["default"])
    print("OUTGOING PROXY PROTOCOL:","on" if proxy else "off")
    print("APPROVED NEW SNI:",len(names))
    if not args.apply:
        print("PREVIEW ONLY; no files changed")
        return
    secure_store()
    if MANAGED.exists() or MANAGED.is_symlink() or (STORE/"ingress.json").exists() or (STORE/"ingress.json").is_symlink():
        raise ProfileError("managed ingress already exists; do not overwrite it")
    backup = Path(tempfile.mkdtemp(prefix="nginx-onboarding-",dir=STORE))
    atomic(backup/"nginx.conf.original",original)
    atomic(backup/"nginx.conf.candidate",candidate)
    atomic(backup/"metadata.json",json.dumps({"original_sha256":sha(original),
        "candidate_sha256":sha(candidate),"mode":mode,"uid":uid,"gid":gid}).encode())
    print("BACKUP:",backup,flush=True)
    # Empty fragment changes no route, so runtime reload is unnecessary.
    fd = os.open(MANAGED,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
    with os.fdopen(fd,"wb") as stream:
        stream.write(b"# Router Orchestrator managed SNI entries (empty at onboarding)\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        fd,temp = tempfile.mkstemp(prefix=".ro-ingress-test-",dir=MAIN.parent)
        try:
            with os.fdopen(fd,"wb") as stream:
                stream.write(candidate)
            run([NGINX,"-t","-c",temp])
        finally:
            Path(temp).unlink(missing_ok=True)
        if MAIN.read_bytes() != original or sha(dump()) != args.expected_sha256:
            raise ProfileError("nginx changed during preparation")
        atomic(MAIN,candidate,mode,(uid,gid))
        inventory["nginx_config_sha256"] = sha(dump())
        atomic(STORE/"ingress.json",json.dumps(inventory,indent=2).encode())
    except Exception:
        actual = MAIN.read_bytes()
        if actual == candidate:
            atomic(MAIN,original,mode,(uid,gid))
        elif actual != original:
            raise ProfileError("external nginx edit detected; retain backup and include for manual recovery")
        # Do not leave an ownership inventory pointing to an absent include.
        (STORE/"ingress.json").unlink(missing_ok=True)
        MANAGED.unlink(missing_ok=True)
        raise
    print("MANAGED INCLUDE:",MANAGED)
    print("INVENTORY:",STORE/"ingress.json")
    print("NGINX SYNTAX: PASS")
    print("EXISTING ROUTES: UNCHANGED")
    print("NGINX RELOAD: NOT PERFORMED")
    print("XRAY AND ROUTERS: UNCHANGED")


if __name__ == "__main__":
    try:
        main()
    except (ProfileError,OSError,ValueError) as exc:
        raise SystemExit("INGRESS BOOTSTRAP BLOCKED: "+str(exc))
