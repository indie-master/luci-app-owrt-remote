#!/usr/bin/env python3
"""Synthetic seventh WSS router -> real Xray -test via read-only adapter.

Requires the already-reviewed six-router Belt rehearsal snapshot. The
snapshot's SQLite is copied to a disposable directory. The synthetic
router exists ONLY there. The additive Xray JSON is passed in-memory over
the UID-23088 UNIX socket to xray.preview; root-owned adapter writes it to a
private temporary file in /run, invokes xray run -test, and deletes it.

NEVER calls xray.apply/restart or writes production state/config.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys

import orchestrator_host_adapter_readonly_acceptance as acceptance
import orchestrator_docker_wss_enrollment_preview as synthetic

UID = 23088
SOCKET = Path("/run/router-orchestrator/control.sock")
MAX_RESPONSE = 4096


class Blocked(RuntimeError):
    pass


def require(value, description):
    if not value:
        raise Blocked(description)


def send_preview_as_peer(config: dict, expected_sha256: str, _router_id: str):
    """Pass secret-bearing Xray candidate only through the authorized socket.

    The child returns ONLY fixed status booleans. Raw JSON never enters the
    parent's stdout/stderr, a file, or the subprocess command line.
    """
    require(os.geteuid() == 0, "UID-restricted probe must begin as root")
    fd_read, fd_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(fd_read)
            os.setgroups([])
            os.setgid(UID)
            os.setuid(UID)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(40)
                client.connect(str(SOCKET))
                request = {"op": "xray.preview",
                           "expected_sha256": expected_sha256,
                           "config": config}
                client.sendall(json.dumps(
                    request, ensure_ascii=False, separators=(",", ":")
                ).encode() + b"\n")
                with client.makefile("rb") as reader:
                    raw = reader.readline(MAX_RESPONSE + 1)
            require(raw.endswith(b"\n") and len(raw) <= MAX_RESPONSE,
                    "adapter preview response is invalid")
            reply = json.loads(raw)
            require(reply.get("ok") is True and reply.get("validated") is True
                    and reply.get("xray_config_unchanged") is True
                    and reply.get("restart_performed") is False
                    and reply.get("apply_performed") is False,
                    "adapter refused Xray preview")
            verdict = {"ok": True}
        except BaseException as exc:
            verdict = {"ok": False, "reason": type(exc).__name__}
        try:
            os.write(fd_write, json.dumps(verdict).encode())
        finally:
            os.close(fd_write)
            os._exit(0)
    os.close(fd_write)
    try:
        result = os.read(fd_read, 1024)
    finally:
        os.close(fd_read)
    _, status = os.waitpid(pid, 0)
    require(os.WIFEXITED(status) and bool(result),
            "Xray preview child exited abnormally")
    verdict = json.loads(result)
    require(verdict.get("ok") is True,
            "UID-23088 Xray preview was blocked (" +
            str(verdict.get("reason", "unknown")) + ")")


def live_preview(snapshot: Path, expect_commit: str, backup_root: Path) -> dict:
    acceptance.preflight(expect_commit)
    acceptance.check_systemd("router-orchestrator-host-adapter.service")
    require(acceptance.PINNED.is_file(), "pinned adapter is not installed")
    require(acceptance.root_peer_rejected(),
            "adapter does not reject root peer")
    current = acceptance.restricted_probe()
    require(current["xray_sha256"] == acceptance.sha256(acceptance.XRAY),
            "live Xray config differs from read-only socket checksum")
    return synthetic.preview(
        snapshot, expect_commit, backup_root,
        live_validator=send_preview_as_peer,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--expect-commit", required=True)
    parser.add_argument("--backup-root", type=Path, default=synthetic.BACKUPS)
    args = parser.parse_args()
    try:
        result = live_preview(args.snapshot, args.expect_commit, args.backup_root)
        print("BELT XRAY LIVE DRY-RUN: PASS")
        print("FLEET: {routers} unchanged | WSS={wss} TCP={tcp}".format(**result))
        print("SYNTHETIC: one new WSS LuCI/SSH router validated by installed Xray")
        print("AUTH: root denied; numeric UID-23088 read-only preview accepted")
        print("XRAY: -test passed without config apply or restart")
        print("STATE: restored copy cleaned; live database untouched")
        print("NO CUTOVER: systemd HUB and host Xray kept active")
        return 0
    except (Blocked, synthetic.Blocked, acceptance.Blocked) as exc:
        # All messages here are authored and do not contain credentials.
        print("BELT XRAY LIVE DRY-RUN: BLOCKED — " + str(exc)[:190],
              file=sys.stderr)
        return 2
    except (OSError, ValueError, TimeoutError) as exc:
        print("BELT XRAY LIVE DRY-RUN: BLOCKED — " + type(exc).__name__,
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
