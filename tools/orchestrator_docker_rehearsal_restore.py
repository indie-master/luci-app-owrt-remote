#!/usr/bin/env python3
"""Stage a verified HUB snapshot for an isolated Docker rehearsal.

Requires an archive produced by orchestrator_belt_docker_migration_gate.py.
Creates a NEW private directory containing the full SQLite-backed state and
references. Never writes to the active HUB state, Xray, Nginx, Docker or systemd.

DO NOT use as a live cutover/rollback utility: the source snapshot can be
stale because the running HUB continues to receive heartbeat/history writes.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile

import orchestrator_belt_docker_migration_gate as gate

MAX_TOTAL = 4 * 1024**3  # fail closed on a suspiciously huge rehearsal
MAX_ITEM = 2 * 1024**3
REFERENCE_FILES = {"reference/hub-source.py", "reference/xray.json"}


class Blocked(RuntimeError):
    pass


def require(condition: bool, msg: str) -> None:
    if not condition:
        raise Blocked(msg)


def normalize_member(name: str) -> tuple[str, ...]:
    require(isinstance(name, str) and 1 <= len(name) <= 512,
            "invalid archived path")
    require(not name.startswith("/") and "\\" not in name and "\x00" not in name,
            "invalid path separator")
    items = tuple(name.split("/"))
    require(len(items) >= 2 and all(part not in ("", ".", "..") for part in items),
            "unsafe archive member")
    require(items[0] in ("state", "reference"), "unexpected archive namespace")
    require(items[0] != "reference" or name in REFERENCE_FILES,
            "unexpected reference file")
    return items


def _private_directory(path: Path) -> None:
    require(path.is_dir() and not path.is_symlink(), "private directory missing")
    require(path.stat().st_uid == os.geteuid() and
            stat.S_IMODE(path.stat().st_mode) & 0o077 == 0,
            "private directory must be owned by current user, mode 0700")


def _extract(tar: tarfile.TarFile, manifest: dict, output: Path) -> None:
    expected = manifest["files_sha256"]
    require(isinstance(expected, dict) and expected, "empty file digest manifest")
    digests = {}
    total = 0
    for info in tar:
        parts = normalize_member(info.name)
        require(info.isfile() and info.name in expected and
                info.name not in digests, "unknown, duplicate or special member")
        require(0 <= info.size <= MAX_ITEM, "oversized archived file")
        total += info.size
        require(total <= MAX_TOTAL, "archive exceeds rehearsal size policy")
        folder = output
        for part in parts[:-1]:
            folder = folder / part
            if not folder.exists():
                folder.mkdir(mode=0o700)
            _private_directory(folder)
        path = output.joinpath(*parts)
        fd = os.open(str(path),
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        digest = hashlib.sha256()
        original = tar.extractfile(info)
        require(original is not None, "tar member is unreadable")
        with os.fdopen(fd, "wb") as dest, original:
            copied = 0
            while True:
                part = original.read(1024 * 1024)
                if not part:
                    break
                copied += len(part)
                require(copied <= info.size, "member exceeds declared size")
                digest.update(part)
                dest.write(part)
            require(copied == info.size, "truncated member")
            dest.flush()
            os.fsync(dest.fileno())
        require(digest.hexdigest() == expected[info.name],
                "member hash mismatch after extraction")
        digests[info.name] = digest.hexdigest()
    require(digests == expected, "incomplete snapshot extraction")


def verify_staged(folder: Path) -> dict:
    """Verify full archived bytes, DB row counts and live-Xray parity in copy."""
    _private_directory(folder)
    state = folder / "state"
    reference = folder / "reference"
    _private_directory(state)
    _private_directory(reference)
    manifest_path = folder / "manifest.json"
    require(manifest_path.is_file() and not manifest_path.is_symlink(),
            "staged manifest missing")
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("schema") == "orchestrator-docker-rehearsal/1" and
            manifest.get("kind") == "REHEARSAL_ONLY_NOT_CUTOVER",
            "not an approved rehearsal manifest")
    expected = manifest["files_sha256"]
    actual = {}
    for root, dirs, files in os.walk(folder, followlinks=False):
        for d in dirs:
            _private_directory(Path(root) / d)
        for name in files:
            p = Path(root) / name
            require(p.is_file() and not p.is_symlink(), "unexpected staged file")
            relative = p.relative_to(folder).as_posix()
            if relative == "manifest.json":
                continue
            require(relative in expected, "extra file in staging area")
            actual[relative] = gate.file_sha(p)
    require(expected == actual, "staged file digest differs from archive")
    db = gate.db_inventory(state / "hub.db")
    for key, actual_key in (("routers", "active_count"), ("tcp", "tcp"),
                            ("wss", "wss"), ("deleted", "deleted_count")):
        require(manifest["database"][key] == db[actual_key],
                "SQLite inventory mismatches manifest")
    require(manifest["database"]["tables"] == db["tables"],
            "SQLite table counts/history differ from snapshot")
    config = json.loads((reference / "xray.json").read_text())
    gate.xray_parity(db, config)
    for name in gate.MUST_KEEP:
        require((state / name).is_file(), "snapshot missing required state file")
    return {
        "routers": db["active_count"], "wss": db["wss"], "tcp": db["tcp"],
        "tables": db["tables"],
        "source_sha256": actual["reference/hub-source.py"],
        "xray_sha256": actual["reference/xray.json"],
        "file_count": len(expected),
        "live_state_changed": False,
    }


def restore(snapshot_dir: Path, output_root: Path) -> tuple[Path, dict]:
    """Extract to a NEW private rehearsal folder, never overwrite any target."""
    _private_directory(snapshot_dir)
    require(output_root.is_dir() and not output_root.is_symlink(),
            "output parent missing/unsafe")
    require(os.geteuid() == output_root.stat().st_uid,
            "output parent must be owned by current user")
    require(stat.S_IMODE(output_root.stat().st_mode) & 0o022 == 0,
            "output parent must not be group/world writable")
    manifest = gate.inspect_archive(snapshot_dir)
    require(manifest.get("kind") == "REHEARSAL_ONLY_NOT_CUTOVER",
            "snapshot is not explicitly a rehearsal archive")
    folder = Path(tempfile.mkdtemp(prefix="orchestrator-shadow-state-",
                                   dir=output_root))
    folder.chmod(0o700)
    try:
        with tarfile.open(snapshot_dir / "state-and-references.tar.gz", "r:gz") as tar:
            _extract(tar, manifest, folder)
        path = folder / "manifest.json"
        with path.open("x", encoding="utf-8") as out:
            os.chmod(path, 0o600)
            json.dump(manifest, out, indent=2, sort_keys=True)
            out.write("\n")
        report = verify_staged(folder)
        return folder, report
    except BaseException:
        # Never leave a partially extracted directory containing credentials.
        shutil.rmtree(folder)
        raise


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("restore", help="create private Docker rehearsal copy")
    prep.add_argument("--snapshot", type=Path, required=True)
    prep.add_argument("--output-root", type=Path, default=Path("/root"))
    check = sub.add_parser("verify", help="verify untouched staged copy")
    check.add_argument("--stage", type=Path, required=True)
    args = p.parse_args()
    try:
        if args.command == "restore":
            folder, report = restore(args.snapshot, args.output_root)
            print("RESTORED REHEARSAL COPY:", folder)
        else:
            report = verify_staged(args.stage)
        print("STAGE: PASS | routers={routers}, WSS={wss}, TCP={tcp}, "
              "tables={tables}, archived_files={file_count}".format(
                **{**report, "tables": len(report["tables"])}))
        print("SOURCE SHA256:", report["source_sha256"])
        print("XRAY JSON SHA256:", report["xray_sha256"])
        print("PRIVATE DATA: never put staging directory or image with state in GitHub")
        print("READ ONLY TO BELT: No production services, databases or ports modified")
        return 0
    except (Blocked, gate.GateError, OSError, ValueError,
            sqlite3.Error, tarfile.TarError, KeyError, TypeError) as exc:
        # No paths/tokens/IDs in exception logs; the snapshot can be sensitive.
        print("BLOCKED: " + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
