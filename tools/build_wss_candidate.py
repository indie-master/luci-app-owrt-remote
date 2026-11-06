#!/usr/bin/env python3
"""Build a WSS reverse candidate without touching live Xray or UCI.

Developer/Belt-side tool: Python is NOT an OpenWrt dependency.
Candidate is NOT approval to deploy: validate with pinned Xray core first.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

REVERSE_TAGS = {"vps-interconn", "vps-ssh-interconn"}
HOST_RE = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")


class CandidateError(ValueError):
    pass


def build_candidate(source, *, host, sni, path, port):
    if not HOST_RE.fullmatch(host) or not HOST_RE.fullmatch(sni):
        raise CandidateError("invalid host/SNI")
    if not path.startswith("/") or "?" in path or "#" in path or len(path) > 512:
        raise CandidateError("invalid websocket path")
    if not 1 <= port <= 65535:
        raise CandidateError("invalid port")
    candidate = json.loads(json.dumps(source))
    outbounds = candidate.get("outbounds")
    if not isinstance(outbounds, list):
        raise CandidateError("missing outbounds")
    links = [item for item in outbounds if isinstance(item, dict) and item.get("tag") in REVERSE_TAGS]
    if len(links) != 2 or {item["tag"] for item in links} != REVERSE_TAGS:
        raise CandidateError("exactly two unique reverse links required")
    for link in links:
        if link.get("protocol") != "vless":
            raise CandidateError("reverse link is not VLESS")
        stream = link.get("streamSettings", {})
        if not isinstance(stream, dict) or stream.get("network", "tcp") != "tcp" or stream.get("security", "none") != "none":
            raise CandidateError("source is not pure TCP/none")
        vless = link.get("settings")
        if not isinstance(vless, dict):
            raise CandidateError("missing VLESS settings")
        if isinstance(vless.get("vnext"), list):
            if len(vless["vnext"]) != 1 or not isinstance(vless["vnext"][0], dict):
                raise CandidateError("unsupported VLESS vnext format")
            vless["vnext"][0]["address"] = host
            vless["vnext"][0]["port"] = port
        elif isinstance(vless.get("address"), str) and isinstance(vless.get("port"), int):
            vless["address"] = host
            vless["port"] = port
        else:
            raise CandidateError("unsupported VLESS endpoint representation")
        link["streamSettings"] = {
            **{k: v for k, v in stream.items() if k not in
               ("network", "security", "tlsSettings", "wsSettings", "tcpSettings", "httpSettings", "grpcSettings")},
            "network": "ws",
            "security": "tls",
            "tlsSettings": {"serverName": sni, "allowInsecure": False, "alpn": ["http/1.1"]},
            "wsSettings": {"path": path, "host": sni},
        }
    return candidate


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--host", required=True)
    p.add_argument("--sni", required=True)
    p.add_argument("--path", default="/owrt-ws/")
    p.add_argument("--port", type=int, default=443)
    a = p.parse_args()
    try:
        src, out = a.source.absolute(), a.out.absolute()
        if src == out or src.is_symlink() or not src.is_file():
            raise CandidateError("source must be a different regular file")
        if out.is_symlink() or out.exists():
            raise CandidateError("output already exists or is symlink")
        original = src.read_bytes()
        candidate = build_candidate(json.loads(original), host=a.host, sni=a.sni, path=a.path, port=a.port)
        rendered = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode()
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(rendered)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        print("WSS-CANDIDATE: STAGED ONLY — live config unchanged")
        print("WSS-CANDIDATE: source SHA256:", hashlib.sha256(original).hexdigest())
        print("WSS-CANDIDATE: candidate SHA256:", hashlib.sha256(rendered).hexdigest())
        print("WSS-CANDIDATE: validate with pinned Xray before guarded apply")
        return 0
    except (OSError, ValueError) as exc:
        print("WSS-CANDIDATE: BLOCKED —", type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
