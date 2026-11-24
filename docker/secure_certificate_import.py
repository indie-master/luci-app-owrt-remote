"""Import an existing nginx/Certbot TLS pair into an isolated immutable slot.

Only literal paths from nginx's tested configuration or standard Certbot
lineages are candidates. Browser requests supply a hostname, never a path.
No certificate issuance, reload or overwrite of an existing slot.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile

from secure_certificate_inventory import inspect_pair, MOUNTED_TLS, CONTAINER_TLS
from secure_ingress import domain
from secure_publication_transaction import atomic, sync_directory
from transport_profiles import ProfileError

SOURCE_ROOTS = (Path("/etc/nginx"), Path("/etc/letsencrypt"), Path("/opt/ssl-renewal"))
CERTBOT = Path("/etc/letsencrypt/live")
MAX_PEM = 1024 * 1024


def nginx_pairs(raw):
    """Conservatively recognize adjacent literal certificate/key directives."""
    pending = None
    pairs = []
    for line in raw.decode("utf-8", errors="strict").splitlines():
        if line.lstrip().startswith("# configuration file ") or "{" in line or "}" in line:
            pending = None
        stripped = line.strip()
        if not stripped.startswith(("ssl_certificate ", "ssl_certificate_key ")):
            continue
        try:
            words = shlex.split(stripped, comments=True)
        except ValueError:
            pending = None
            continue
        if len(words) != 2 or not words[1].endswith(";"):
            pending = None
            continue
        path = words[1][:-1]
        if not path.startswith("/") or "$" in path or any(c in path for c in "\r\n\0"):
            pending = None
            continue
        if words[0] == "ssl_certificate":
            pending = Path(path)
        elif pending is not None:
            pairs.append((pending, Path(path)))
            pending = None
        if len(pairs) > 128:
            raise ProfileError("nginx certificate inventory exceeds limit")
    return pairs


def discover_pairs():
    try:
        checked = subprocess.run(["/usr/sbin/nginx", "-T"], capture_output=True,
                                 timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        raise ProfileError("nginx certificate inventory unavailable") from None
    if not checked.stdout or len(checked.stdout) > 16 * 1024 * 1024:
        raise ProfileError("nginx inventory exceeds limit")
    pairs = nginx_pairs(checked.stdout)
    if CERTBOT.is_dir():
        for entry in sorted(CERTBOT.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                pairs.append((entry / "fullchain.pem", entry / "privkey.pem"))
            if len(pairs) > 256:
                raise ProfileError("certificate inventory exceeds limit")
    return pairs


def read_pem(path, roots):
    resolved = Path(path).resolve(strict=True)
    if not any(resolved.is_relative_to(Path(r).resolve()) for r in roots):
        raise ProfileError("source outside approved certificate roots")
    # Every resolved ancestor must be trusted against non-root replacement.
    for parent in (resolved.parent, *resolved.parent.parents):
        info = parent.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ProfileError("untrusted certificate source directory")
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o022 or not 0 < info.st_size <= MAX_PEM):
            raise ProfileError("unsafe certificate source file")
        data = stream.read(MAX_PEM + 1)
        if not 0 < len(data) <= MAX_PEM:
            raise ProfileError("certificate source exceeds limit")
        return data


def import_certificate(hostname, *, root=MOUNTED_TLS, pairs=None, roots=SOURCE_ROOTS):
    name = domain(hostname)
    root = Path(root)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ProfileError("unsafe dedicated TLS mount")
    for certificate, key in (discover_pairs() if pairs is None else pairs):
        try:
            cert_bytes = read_pem(certificate, roots)
            key_bytes = read_pem(key, roots)
        except (OSError, ProfileError):
            continue
        # Immutable slots avoid replacing a pair used by an existing profile.
        slot = "import-" + hashlib.sha256(cert_bytes + b"\0" + key_bytes).hexdigest()
        destination = root / slot
        temp = Path(tempfile.mkdtemp(prefix=".ro-tls-", dir=root))
        try:
            atomic(temp / "fullchain.pem", cert_bytes, 0o644)
            atomic(temp / "privkey.pem", key_bytes, 0o600)
            try:
                inspect_pair(name, temp/"fullchain.pem", temp/"privkey.pem", roots=[root])
            except ProfileError:
                continue
            try:
                os.rename(temp, destination)
                sync_directory(root)
            except OSError:
                # Existing slot must be identical. Never overwrite or follow
                # a symlink placed at the content-addressed destination.
                if not destination.is_dir() or destination.is_symlink():
                    raise
                existing = destination.lstat()
                if existing.st_uid != os.geteuid() or existing.st_mode & 0o077:
                    raise ProfileError("unsafe existing TLS slot")
                if (read_pem(destination/"fullchain.pem",[root]) != cert_bytes
                        or read_pem(destination/"privkey.pem",[root]) != key_bytes):
                    raise ProfileError("existing TLS slot integrity mismatch")
            return {"server_name":name,
                    "certificate_file":str(CONTAINER_TLS/slot/"fullchain.pem"),
                    "key_file":str(CONTAINER_TLS/slot/"privkey.pem"),
                    "imported":True,"applied":False}
        finally:
            if temp.exists():
                shutil.rmtree(temp)
    raise ProfileError("no valid existing certificate found in approved source directories")
