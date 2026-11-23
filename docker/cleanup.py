#!/usr/bin/env python3
"""Project-only Docker cleanup. Dry-run unless explicitly approved.

Never invokes docker prune and never touches other stacks on a shared VPS.
Requires an acceptance marker written after verified reboot and rollback.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess

KEEP_LIVE = ("router-orchestrator-hub", "router-orchestrator-xray", "router-orchestrator-nginx")
STAGING = ("router-orchestrator-staging", "router-orchestrator-proxy-staging")
ACCEPTANCE = Path("/opt/router-orchestrator/data/release-acceptance.json")


def docker(*args):
    result = subprocess.run(("docker", *args), capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Docker inspection failed")
    return result.stdout


def inspect(name):
    result = subprocess.run(("docker", "inspect", name),
                            capture_output=True, text=True)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def plan():
    for name in KEEP_LIVE:
        state = inspect(name)
        if not state or not state["State"]["Running"]:
            raise RuntimeError(f"required production container not running: {name}")
    # Docker inspect is authoritative; avoid parsing human-formatted columns.
    staging = [name for name in STAGING if inspect(name) is not None]
    running_images = set()
    for identifier in docker("ps", "-aq").splitlines():
        item = inspect(identifier)
        if item:
            running_images.add(item["Image"])
    candidates = []
    for line in docker("images", "--format", "{{.Repository}}:{{.Tag}}|{{.ID}}").splitlines():
        if "|" not in line:
            continue
        name, image_id = line.split("|", 1)
        if not (name.startswith("router-orchestrator-hub:")
                or name.startswith("router-orchestrator-xray:")):
            continue
        if image_id not in running_images and name not in {
            "router-orchestrator-hub:release",
        }:
            candidates.append(name)
    return staging, sorted(set(candidates))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    staging, candidates = plan()
    print("STAGING CONTAINERS:", staging)
    print("UNUSED PROJECT IMAGE TAGS:", candidates)
    print("REMMNAWAVE AND GLOBAL DOCKER PRUNE: NOT TOUCHED")
    if not args.apply:
        print("DRY-RUN ONLY: no Docker objects removed")
        return
    if not ACCEPTANCE.is_file():
        raise RuntimeError("release acceptance marker missing")
    state = json.loads(ACCEPTANCE.read_text())
    required = ("fleet_verified", "rollback_verified", "reboot_verified",
                "backup_restorable", "global_logs_verified", "wol_verified")
    if not all(state.get(key) is True for key in required):
        raise RuntimeError("release acceptance gates not passed")
    # Keep historic tags for rollback until explicitly archived by operator.
    for name in staging:
        docker("rm", "-f", name)
        print("REMOVED STAGING:", name)
    print("UNUSED IMAGE TAGS RETAINED AS ROLLBACK UNTIL OPERATOR APPROVAL")


if __name__ == "__main__":
    main()
