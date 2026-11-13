#!/usr/bin/env python3
"""Prepare a private, source-exact Docker HUB image context from a shadow copy.

Never runs Docker, never touches production. Only copies the *archived* HUB
Python source and selected static assets, not state, credentials or Xray.
The Docker image must still undergo isolated runtime tests before deployment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

import orchestrator_docker_rehearsal_restore as shadow

ALLOWED_EXTENSIONS = {".png", ".ico", ".svg", ".webp", ".css", ".js", ".html", ".txt"}
MAX_STATIC_TOTAL = 64 * 1024 * 1024


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise shadow.Blocked(reason)


def build_context(stage: Path, static_root: Path, dockerfile: Path,
                  destination_parent: Path) -> tuple[Path, str]:
    report = shadow.verify_staged(stage)
    require(static_root.is_dir() and not static_root.is_symlink(),
            "static assets root missing/unsafe")
    require(dockerfile.is_file() and not dockerfile.is_symlink(),
            "rehearsal Dockerfile missing/unsafe")
    require(destination_parent.is_dir() and not destination_parent.is_symlink(),
            "destination parent missing/unsafe")
    require(destination_parent.stat().st_uid == os.geteuid() and
            stat.S_IMODE(destination_parent.stat().st_mode) & 0o022 == 0,
            "destination parent must not be group/world writable")
    template = dockerfile.read_text(encoding="utf-8")
    require("org.router-orchestrator.purpose=\"isolated-hub-rehearsal\"" in template
            and "OWRT_REMOTE_XRAY_CONTROL_BACKEND=external_readonly" in template
            and "COPY --chmod=0755 hub-source.py " in template
            and "COPY static/ " in template
            and "COPY . " not in template,
            "Dockerfile is not the expected isolated source-only recipe")
    source = stage / "reference" / "hub-source.py"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    require(digest == report["source_sha256"], "HUB source changed")
    assets = []
    total = 0
    for root, dirs, files in os.walk(static_root, followlinks=False):
        for item in dirs:
            p = Path(root) / item
            require(p.is_dir() and not p.is_symlink(), "static directory symlink/special")
        for item in files:
            p = Path(root) / item
            require(p.is_file() and not p.is_symlink()
                    and p.suffix.lower() in ALLOWED_EXTENSIONS,
                    "unapproved/special static asset")
            relative = p.relative_to(static_root)
            total += p.stat().st_size
            require(total <= MAX_STATIC_TOTAL, "unexpectedly large static assets")
            assets.append((p, relative))
    require(assets, "static directory must contain usable UI assets")
    context = Path(tempfile.mkdtemp(prefix="orchestrator-shadow-image-",
                                    dir=destination_parent))
    context.chmod(0o700)
    try:
        shutil.copyfile(dockerfile, context / "Dockerfile")
        shutil.copyfile(source, context / "hub-source.py")
        target_assets = context / "static"
        target_assets.mkdir(mode=0o700)
        manifest = {}
        for p, relative in assets:
            destination = target_assets / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(p, destination)
            src_hash = hashlib.sha256(p.read_bytes()).hexdigest()
            dst_hash = hashlib.sha256(destination.read_bytes()).hexdigest()
            require(src_hash == dst_hash, "static asset changed during copy")
            manifest[relative.as_posix()] = dst_hash
        require(hashlib.sha256((context / "hub-source.py").read_bytes()).hexdigest()
                == digest, "image source differs from snapshot")
        (context / "context-report.json").write_text(json.dumps({
            "kind": "ISOLATED_REHEARSAL_ONLY",
            "hub_source_sha256": digest,
            "static_sha256": manifest,
            "excluded": ["state", "xray.json", "hub-auth.json", "agent.token"],
        }, indent=2) + "\n")
        for p in context.rglob("*"):
            if p.is_file():
                p.chmod(0o600)
        return context, digest
    except BaseException:
        shutil.rmtree(context)
        raise


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--stage", type=Path, required=True)
    p.add_argument("--static-dir", type=Path, required=True)
    p.add_argument("--dockerfile", type=Path, required=True)
    p.add_argument("--output-root", type=Path, default=Path("/root"))
    args = p.parse_args()
    try:
        context, digest = build_context(
            args.stage, args.static_dir, args.dockerfile, args.output_root)
        print("IMAGE CONTEXT:", context)
        print("SOURCE SHA256:", digest)
        print("BUILD REQUIREMENT: --build-arg SOURCE_SHA256=" + digest)
        print("No container started; review Dockerfile and select trusted Python base image")
        print("NEVER commit an image containing a private HUB source to a public registry")
        return 0
    except (shadow.Blocked, OSError, ValueError, KeyError, TypeError) as err:
        print("BLOCKED: " + type(err).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
