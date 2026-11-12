#!/usr/bin/env python3
"""Preview a pin-and-test Xray-core update on its NATIVE architecture.

This stages *only verification*, never downloads or installs binaries.
The candidate must be an already extracted executable and its SHA-256
must be supplied by the operator from a trusted release manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")
VERSION = re.compile(r"^Xray ([0-9]+\.[0-9]+\.[0-9]+)\b")


class GateError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise GateError(reason)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def version_of(path: Path) -> str:
    p = subprocess.run([str(path), "version"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=12)
    require(p.returncode == 0, "candidate Xray version command failed")
    first = p.stdout.decode("utf-8", "replace").splitlines()
    require(first, "empty Xray version output")
    match = VERSION.match(first[0])
    require(match is not None, "unexpected Xray version output")
    return match.group(1)


def test_config(path: Path, config: Path) -> None:
    p = subprocess.run([str(path), "run", "-test", "-config", str(config)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       timeout=35)
    # No stdout/stderr: config/test failures may contain private details.
    require(p.returncode == 0, "candidate Xray rejected the supplied config")


def preview(candidate: Path, expected_sha: str, expected_version: str,
            config: Path, installed: Path | None = None) -> dict:
    require(candidate.is_file() and not candidate.is_symlink(),
            "candidate binary missing or symlink")
    require(config.is_file() and not config.is_symlink(),
            "test configuration missing or symlink")
    require(os.access(candidate, os.X_OK), "candidate executable permission missing")
    require(DIGEST.fullmatch(expected_sha) is not None,
            "full 64-character SHA256 required")
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_version)
            is not None, "full version pin required")
    with candidate.open("rb") as f:
        require(f.read(4) == b"\x7fELF",
                "candidate must be an extracted native ELF Xray executable")
    if installed is not None:
        require(installed.is_file() and not installed.is_symlink(),
                "installed binary is missing")
        require(candidate.resolve() != installed.resolve(),
                "candidate must not replace or be the running binary")
    digest = sha256(candidate)
    require(hashlib.compare_digest(digest.lower(), expected_sha.lower())
            if hasattr(hashlib, "compare_digest")
            else digest.lower() == expected_sha.lower(),
            "candidate SHA256 does not match the trusted release manifest")
    current = version_of(installed) if installed is not None else None
    proposed = version_of(candidate)
    require(proposed == expected_version, "candidate version mismatch")
    test_config(candidate, config)
    return {"candidate_version": proposed, "current_version": current,
            "sha256_verified": True, "syntax_accepted": True,
            "installed": False, "restart": False}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", choices=("hub", "router"), required=True)
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--sha256", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--installed", type=Path)
    args = p.parse_args()
    try:
        result = preview(args.candidate, args.sha256, args.version,
                         args.config, args.installed)
        print("XRAY CORE STAGE: PASS")
        print("ROLE:", args.role)
        print("VERSION:", result["current_version"] or "not supplied",
              "->", result["candidate_version"])
        print("SHA256: MATCH | Xray config syntax: PASS")
        print("READ ONLY: no binary, config, service or router changes")
        print("WARNING: Xray config syntax alone does NOT confirm live reverse compatibility")
        return 0
    except (GateError, OSError, subprocess.TimeoutExpired, ValueError) as e:
        print("XRAY CORE STAGE: BLOCKED — " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
