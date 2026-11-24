"""Validate exact external nginx candidate in a private mount namespace.

Root operator/worker only. The host adapter is intentionally not granted
CAP_SYS_ADMIN. No reload, host mount or write to the live include.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from secure_publication_transaction import read_regular
from transport_profiles import ProfileError

MANAGED = Path("/etc/nginx/router-orchestrator-managed.map")
SCRIPT = 'set -eu; /usr/bin/mount --bind "$1" "$2"; exec /usr/sbin/nginx -T'


def candidate_digest(fragment, *, expected_live_sha, expected_fragment_sha,
                     nginx_digest, target=MANAGED):
    if not isinstance(fragment, bytes) or len(fragment)>1024*1024:
        raise ProfileError("invalid managed nginx candidate")
    target=Path(target)
    if target != MANAGED:
        raise ProfileError("unexpected managed include path")
    current, mode, uid, gid = read_regular(target)
    if uid != os.geteuid() or mode & 0o022:
        raise ProfileError("unsafe managed nginx include")
    if hashlib.sha256(current).hexdigest()!=expected_fragment_sha:
        raise ProfileError("managed nginx include changed")
    if nginx_digest("external-stream")!=expected_live_sha:
        raise ProfileError("nginx baseline changed before preflight")
    # A separate namespace with private propagation ensures mount --bind
    # can never replace the live include in the host mount namespace.
    with tempfile.TemporaryDirectory(prefix="ro-nginx-preflight-") as temp:
        staged=Path(temp)/"managed.map"
        staged.write_bytes(fragment)
        staged.chmod(0o600)
        try:
            result=subprocess.run(
                ["/usr/bin/unshare","--mount","--propagation","private","--",
                 "/bin/sh","-c",SCRIPT,"ro-nginx-preflight",str(staged),str(target)],
                capture_output=True,timeout=25,check=True)
        except (OSError,subprocess.SubprocessError):
            raise ProfileError("isolated nginx candidate validation unavailable or failed") from None
        raw=result.stdout
        if not raw or len(raw)>16*1024*1024:
            raise ProfileError("invalid nginx candidate dump")
    if read_regular(target)[0]!=current or nginx_digest("external-stream")!=expected_live_sha:
        raise ProfileError("nginx baseline changed during preflight")
    return hashlib.sha256(raw).hexdigest()
