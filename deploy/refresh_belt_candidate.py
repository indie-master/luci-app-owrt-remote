#!/usr/bin/env python3
"""Rebuild a staged candidate without touching live Belt containers.

Preserves the old candidate as a private timestamped rollback directory.
Refuses to refresh after the single Compose cutover is active.
"""
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path("/opt/router-orchestrator")
SRC = ROOT / "src"
ACTIVE = ROOT / "docker-compose.yml"
STAGE = ROOT / "unified-candidate"


def main():
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    if ACTIVE.exists() or ACTIVE.is_symlink():
        raise RuntimeError("root Docker Compose exists; candidate may be active")
    if not (SRC / ".git").exists():
        raise RuntimeError("pinned source worktree missing")
    if not STAGE.is_dir() or STAGE.is_symlink():
        raise RuntimeError("no safe candidate to refresh")
    version = subprocess.run(["git", "-C", str(SRC), "rev-parse", "--short=12", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
    archive = ROOT / ("unified-candidate.previous-" + version + "-" +
                       time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    if archive.exists():
        raise RuntimeError("archive path already exists")
    STAGE.rename(archive)
    print("PREVIOUS CANDIDATE ARCHIVED:", archive, flush=True)
    try:
        result = subprocess.run(
            [sys.executable, str(SRC / "deploy/prepare_belt.py")],
            check=False
        )
        if result.returncode != 0:
            raise RuntimeError("candidate preparation failed")
        print("NEW CANDIDATE: VERIFIED")
        print("LIVE HUB/XRAY/NGINX: UNCHANGED")
    except BaseException:
        if STAGE.exists():
            failed = ROOT / ("unified-candidate.failed-" + str(os.getpid()))
            STAGE.rename(failed)
            print("PARTIAL CANDIDATE RETAINED:", failed, flush=True)
        archive.rename(STAGE)
        print("PREVIOUS CANDIDATE RESTORED", flush=True)
        raise


if __name__ == "__main__":
    main()
