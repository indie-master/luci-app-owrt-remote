#!/usr/bin/env python3
"""Build a deterministic, fixed-file OpenWrt client candidate for guarded pilots.

Runs on a developer/CI machine, NEVER on the router. This does not install.
"""
from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
# Exact files installed by the original install.sh, excluding UCI and optional packages.
FILES = (
    ("usr/sbin/owrt-remote", 0o755),
    ("usr/lib/owrt-remote/wan.sh", 0o644),
    ("usr/lib/owrt-remote/logs.sh", 0o644),
    ("etc/init.d/owrt-remote-logs", 0o755),
    ("etc/init.d/owrt-remote", 0o755),
    ("etc/hotplug.d/iface/99-owrt-remote", 0o755),
    ("www/cgi-bin/owrt-remote", 0o755),
    ("usr/share/luci/menu.d/luci-app-owrt-remote.json", 0o644),
    ("usr/share/rpcd/acl.d/luci-app-owrt-remote.json", 0o644),
    ("www/luci-static/resources/view/owrt_remote.js", 0o644),
)

def build(out: Path, root: Path) -> str:
    entries = []
    manifest = []
    for name, mode in FILES:
        path = root / 'files' / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing or unsafe source file: {path}")
        data = path.read_bytes()
        if not data:
            raise ValueError(f"Refusing empty source: {path}")
        entries.append((f'payload/{name}', mode, data))
        manifest.append(f"{hashlib.sha256(data).hexdigest()} {mode:04o} {name}\n")
    entries.insert(0, ('manifest.txt', 0o600, ''.join(manifest).encode('ascii')))
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, 'w', format=tarfile.USTAR_FORMAT) as archive:
        for path, mode, data in entries:
            info = tarfile.TarInfo(path)
            info.mode = mode
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mtime = 0
            info.size = len(data)
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(out.read_bytes()).hexdigest()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    digest = build(args.out, args.root)
    print(f'BUNDLE: {args.out}\nSHA256: {digest}\nFILES: {len(FILES)}')
    print('A bundle and a digest are build artifacts, not approval to deploy.')
