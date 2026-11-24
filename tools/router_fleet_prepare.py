#!/usr/bin/env python3
"""Prepare exactly one enrolled OpenWrt device over its existing reverse SSH.

Default: inspect only. --stage also transfers the pinned ten-file bundle and
creates a persistent recovery snapshot. Never installs or restarts anything.
Run on Belt, not in the HUB container. SSH credentials are managed by OpenSSH;
the router host key must already be trusted (or verified interactively).
"""
import argparse
import hashlib
import io
from pathlib import Path
import re
import shlex
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "tools/router_bundle_guard.sh"
REMOTE = r"""
set -eu
umask 077
[ "$(id -u)" = 0 ] || exit 1
work="$(mktemp -d /tmp/ro-fleet-prepare.XXXXXX)"
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM
tar -xf - -C "$work"
export RO_TEST_MODE=0 RO_TEST_ROOT=/
export RO_EXPECT_ROUTER_ID="$1" RO_EXPECT_TRANSPORT="$2" RO_EXPECT_PORT="$3"
cd "$work"
sh guard.sh inspect
if [ "$4" = stage ]; then
    sh guard.sh stage bundle.tar "$5"
    sh guard.sh snapshot
    printf '%s\n' 'REMOTE PREPARATION: PASS (no installed files changed)'
fi
"""


def valid_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value):
        raise argparse.ArgumentTypeError("invalid router identity")
    return value


def port(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid port") from None
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port outside 1..65535")
    return number


def make_payload(bundle=None, expected=None):
    entries = [("guard.sh", GUARD.read_bytes())]
    if bundle is not None:
        if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("exact bundle SHA256 required")
        if not bundle.is_file() or bundle.is_symlink():
            raise ValueError("bundle not a regular file")
        if bundle.stat().st_size > 6291456:
            raise ValueError("bundle too large")
        data = bundle.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("bundle digest mismatch")
        entries.append(("bundle.tar", data))
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, data in entries:
            entry = tarfile.TarInfo(name)
            entry.mode = 0o600
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


def ssh_command(args):
    remote = shlex.join(["sh", "-c", REMOTE, "ro-prepare", args.router,
                        args.transport, str(args.vps_port),
                        "stage" if args.stage else "inspect",
                        args.sha256 or "-"])
    return ["ssh", "-T", "-o", "StrictHostKeyChecking=ask",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=2", "-o", "ClearAllForwardings=yes",
            "-p", str(args.ssh_port), "root@127.0.0.1", remote]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--router", required=True, type=valid_id)
    parser.add_argument("--transport", required=True, choices=("tcp", "ws"))
    parser.add_argument("--vps-port", required=True, type=port)
    parser.add_argument("--ssh-port", required=True, type=port)
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    if args.stage != bool(args.bundle and args.sha256):
        parser.error("--stage requires both --bundle and --sha256")
    if not args.stage and (args.bundle or args.sha256):
        parser.error("bundle options are only for --stage")
    payload = make_payload(args.bundle, args.sha256)
    result = subprocess.run(ssh_command(args), input=payload, check=False)
    if result.returncode:
        raise SystemExit("PREPARATION NOT CONFIRMED: inspect remote state before retry")
    print("FLEET PREPARATION: PASS; no install or transport change")


if __name__ == "__main__":
    main()
