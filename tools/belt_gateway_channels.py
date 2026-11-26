"""Candidate read-only Gateway probe; the trusted Gateway supplies inventory/config.

This module has no CLI. It must only be called after the Gateway has verified
that the supplied Xray configuration is the active configuration. Never pass
caller-controlled mappings to it.
"""
import http.client
import re
import socket
import time

ID = re.compile(r"[A-Za-z0-9_.-]{1,80}\Z")
HTTP_CODES = {200, 301, 302, 303, 307, 308, 401, 403}


def confirmed_mappings(rows, config):
    """Fail closed on ambiguous DB/config routes; do not return credentials."""
    if not isinstance(config, dict) or not isinstance(config.get("inbounds"), list):
        raise ValueError("invalid configuration")
    routing = config.get("routing")
    if not isinstance(routing, dict) or not isinstance(routing.get("rules"), list):
        raise ValueError("invalid routing")
    inbounds = config["inbounds"]
    rules = routing["rules"]
    if any(not isinstance(x, dict) for x in inbounds + rules):
        raise ValueError("invalid entries")
    mappings, used = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid router row")
        rid = row.get("id")
        if not isinstance(rid, str) or not ID.fullmatch(rid) or rid in mappings:
            raise ValueError("invalid or duplicate router ID")
        ports = []
        for tag, field, target, service_port in (
            ("entry-" + rid, "entry_port", "reverse-" + rid, 80),
            ("ssh-" + rid, "ssh_entry_port", "reverse-" + rid + "-ssh", 22),
        ):
            port = row.get(field)
            if type(port) is not int or not 1 <= port <= 65535 or port in used:
                raise ValueError("invalid or duplicate port")
            matches = [x for x in inbounds if x.get("tag") == tag]
            if len(matches) != 1 or matches[0].get("listen") != "127.0.0.1" or matches[0].get("protocol") != "tunnel" or matches[0].get("port") != port:
                raise ValueError("inbound mismatch")
            if matches[0].get("settings") != {"allowedNetwork": "tcp",
                                               "rewriteAddress": "127.0.0.1",
                                               "rewritePort": service_port}:
                raise ValueError("unexpected service destination")
            if sum(x.get("port") == port for x in inbounds) != 1:
                raise ValueError("ambiguous port")
            # A prior or additional rule could route this inbound elsewhere.
            relevant = [r for r in rules if isinstance(r.get("inboundTag"), list) and tag in r["inboundTag"]]
            if len(relevant) != 1 or relevant[0].get("inboundTag") != [tag] or relevant[0].get("outboundTag") != target:
                raise ValueError("route mismatch")
            index = rules.index(relevant[0])
            if (relevant[0].get("type") != "field" or
                    set(relevant[0]) - {"type", "inboundTag", "outboundTag"} or
                    any(not isinstance(r.get("inboundTag"), list) for r in rules[:index])):
                raise ValueError("ambiguous route order")
            used.add(port)
            ports.append(port)
        mappings[rid] = tuple(ports)
    return mappings


def ssh_check(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=4) as sock:
            deadline, data = time.monotonic() + 5, b""
            while len(data) < 4096 and time.monotonic() < deadline:
                sock.settimeout(max(.01, deadline - time.monotonic()))
                chunk = sock.recv(min(512, 4096 - len(data)))
                if not chunk:
                    break
                data += chunk
                for line in data.split(b"\n")[:-1]:
                    if len(line) <= 254 and re.fullmatch(rb"SSH-(?:2\.0|1\.99)-[!-~]+(?: [ -~]*)?\r?", line):
                        return True
    except OSError:
        pass
    return False


def luci_check(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        conn.request("GET", "/cgi-bin/luci/", headers={"Connection": "close"})
        return conn.getresponse().status in HTTP_CODES
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def survey(rows, config):
    """Only call with trusted, independently confirmed active configuration."""
    mappings = confirmed_mappings(rows, config)  # validate every mapping before any connection
    return {rid: {"luci_http": luci_check(lp), "ssh_banner": ssh_check(sp)}
            for rid, (lp, sp) in sorted(mappings.items())}
