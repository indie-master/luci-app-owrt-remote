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

MOUNTED_TLS = Path("/opt/router-orchestrator/tls")
CONTAINER_TLS = Path("/etc/router-orchestrator/tls")


def mounted_certificate(hostname, *, root=MOUNTED_TLS):
    """Bounded discovery of certificate pairs already mounted into Xray.

    Only root and immediate children of the dedicated mount are scanned.
    External nginx certificates must first be provisioned in this directory
    by a separate privileged import/renewal operation. No arbitrary host path
    from the browser is read.
    """
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise ProfileError("dedicated TLS directory is unavailable")
    folders = [root]
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                folders.append(Path(entry.path))
                if len(folders) > 128:
                    raise ProfileError("TLS inventory exceeds bounded scan limit")
    name = domain(hostname)
    folders.sort(key=lambda folder: (folder.name != name, folder != root, str(folder)))
    pairs = []
    for folder in folders:
        for cert_name, key_name in (("fullchain.pem", "privkey.pem"), ("cert.pem", "key.pem")):
            cert, key = folder / cert_name, folder / key_name
            if cert.is_file() and key.is_file():
                pairs.append((cert, key))
    # Resolve paths for checking but retain mount-relative source names for Xray.
    for cert, key in pairs:
        try:
            inspect_pair(name, cert, key, roots=[root])
        except ProfileError:
            continue
        return {"certificate_file": str(CONTAINER_TLS / cert.relative_to(root)),
                "key_file": str(CONTAINER_TLS / key.relative_to(root))}
    raise ProfileError("no matching TLS certificate in dedicated mount; import required")
