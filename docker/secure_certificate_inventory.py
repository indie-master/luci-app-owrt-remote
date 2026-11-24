"""Host-side TLS certificate inventory helpers.

Only trusted administrator-provisioned source directories are accepted.
No certificate issuance, filesystem mutations, key export or nginx reload.
"""
from __future__ import annotations

import os
from pathlib import Path
import ssl
import subprocess

from secure_ingress import domain
from transport_profiles import ProfileError


def _source(path, roots):
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file() or not any(
        resolved.is_relative_to(Path(root).resolve(strict=True)) for root in roots
    ):
        raise ProfileError("certificate source outside approved roots")
    return resolved


def inspect_pair(hostname, certificate, key, *, roots, minimum_seconds=86400):
    """Validate name, expiry and matching private key without returning key data."""
    name = domain(hostname)
    if type(minimum_seconds) is not int or not 0 <= minimum_seconds <= 31536000:
        raise ProfileError("invalid minimum certificate lifetime")
    try:
        cert = _source(certificate, roots)
        private = _source(key, roots)
        # load_cert_chain validates the matching private key. Refuse encrypted
        # keys rather than allowing an interactive password prompt.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(private), password=lambda: "")
        command = ["openssl", "x509", "-in", str(cert), "-noout"]
        match = subprocess.run(command + ["-checkhost", name], capture_output=True,
                               text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
        if match.returncode != 0 or match.stdout.strip() != f"Hostname {name} does match certificate":
            raise ProfileError("certificate does not cover selected DNS hostname")
        expiry = subprocess.run(command + ["-checkend", str(minimum_seconds)],
                                capture_output=True, timeout=5)
        if expiry.returncode != 0:
            raise ProfileError("certificate expires too soon or cannot be read")
    except (OSError, ssl.SSLError, subprocess.SubprocessError, ValueError) as exc:
        if isinstance(exc, ProfileError):
            raise
        raise ProfileError("TLS certificate pair could not be validated") from None
    return {"server_name": name, "valid": True, "certificate_file": str(cert),
            "key_file": str(private)}


def find_certificate(hostname, pairs, *, roots, minimum_seconds=86400):
    """Return the first valid pair in trusted inventory priority order."""
    for certificate, key in pairs:
        try:
            return inspect_pair(hostname, certificate, key, roots=roots,
                                minimum_seconds=minimum_seconds)
        except ProfileError:
            continue
    raise ProfileError("no valid matching certificate in approved inventory")
