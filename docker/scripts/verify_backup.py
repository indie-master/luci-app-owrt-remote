#!/usr/bin/env python3
"""Fail-closed restoration of a cold Compose backup."""
from pathlib import Path
import sys
import tarfile

def extract(source, target):
    source, target = Path(source), Path(target)
    with tarfile.open(source, "r:gz") as tar:
        members = tar.getmembers()
        assert members, "Empty backup"
        seen = set()
        total = 0
        for item in members:
            name = item.name
            parts = name.split("/")
            if (name.startswith("/") or "\\" in name or
                any(part in ("", ".", "..") for part in parts) or
                parts[0] not in ("state", "xray") or
                not (item.isdir() or item.isfile())):
                raise ValueError("Unsafe archive member")
            if name in seen:
                raise ValueError("Duplicate archive member")
            seen.add(name)
            total += item.size
            if total > 2 * 1024**3:
                raise ValueError("Archive uncompressed size limit exceeded")
        if not any(x.startswith("state/") for x in seen) or not any(x.startswith("xray/") for x in seen):
            raise ValueError("Archive missing state/ or xray/")
        for item in members:
            path = target / item.name
            if item.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                stream = tar.extractfile(item)
                if stream is None:
                    raise ValueError("Missing archive data")
                with path.open("xb") as output:
                    while chunk := stream.read(1024 * 1024):
                        output.write(chunk)
                path.chmod(0o600)
    if not (target / "state/hub.db").is_file() or not (target / "xray/owrt-remote.json").is_file():
        raise ValueError("Required hub.db or owrt-remote.json missing")
    return True

if __name__ == "__main__":
    extract(sys.argv[1], sys.argv[2])
    print("Validated and extracted into temporary directory")
