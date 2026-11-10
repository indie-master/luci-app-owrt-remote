#!/usr/bin/env python3
"""Belt-side, READ-ONLY network acceptance for the Cudy canary.

No SSH authentication, private key, token, or remote command is used.
Does not access the router's UCI, modify the router or confirm a release.
"""
from __future__ import annotations

import argparse
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 6.0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def ssh_banner(host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> str:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        data = bytearray()
        while len(data) < 128:
            b = sock.recv(1)
            if not b:
                break
            data.extend(b)
            if b == b"\n":
                break
    try:
        banner = data.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ValueError("SSH banner contains non-ASCII bytes") from exc
    if not banner.startswith("SSH-2.0-"):
        raise ValueError("target did not return an SSH-2.0 banner")
    return "SSH-2.0 (valid banner)"


def luci_status(url: str, timeout: float = DEFAULT_TIMEOUT) -> int:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
    ):
        raise ValueError("LuCI URL must be a plain http(s) URL without credentials/query")
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "ro-release-check/1"})
    opener = urllib.request.build_opener(
        NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context())
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            code = response.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    if code != 200:
        raise ValueError(f"LuCI expected HTTP 200, received {code}")
    return code


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Read-only Belt-side router post-activation acceptance")
    p.add_argument("--ssh-host", required=True)
    p.add_argument("--ssh-port", type=int, required=True)
    p.add_argument("--luci-url", required=True)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    a = p.parse_args(argv)
    if not (1 <= a.ssh_port <= 65535 and 1 <= a.timeout <= 20 and a.ssh_host.strip()):
        p.error("invalid host/port or timeout")
    try:
        banner = ssh_banner(a.ssh_host, a.ssh_port, a.timeout)
        print(f"BELT-ACCEPTANCE: PASS - {banner}; no SSH login performed")
        status = luci_status(a.luci_url, a.timeout)
        print(f"BELT-ACCEPTANCE: PASS - LuCI HTTP {status} over existing reverse route")
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print(f"BELT-ACCEPTANCE: BLOCKED - {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print("BELT-ACCEPTANCE: NETWORK CHECK PASSED; external operator must still authenticate release confirmation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
