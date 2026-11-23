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


def protected_tags():
    tags = {"router-orchestrator-hub:release"}
    for name in KEEP_LIVE:
        item = inspect(name)
        if item:
            tags.add(item["Config"].get("Image", ""))
    root = Path("/opt/router-orchestrator")
    baseline = root / "unified-candidate/cutover-rollback/images.json"
    if baseline.is_file():
        data = json.loads(baseline.read_text())
        tags.add(data.get("hub", ""))
    journal = root / "data/xray-lifecycle.json"
    if journal.is_file():
        data = json.loads(journal.read_text())
        tags.add(data.get("old_image", ""))
        tags.add(data.get("new_image", ""))
    return tags


def plan():
    for name in KEEP_LIVE:
        state = inspect(name)
        if not state or not state["State"]["Running"]:
            raise RuntimeError(f"required production container not running: {name}")
    staging = [name for name in STAGING if inspect(name) is not None]
    used_after_staging = set()
    for identifier in docker("ps", "-aq").splitlines():
        item = inspect(identifier)
        if item and item["Name"].lstrip("/") not in STAGING:
            used_after_staging.add(item["Image"])
    keep = protected_tags()
    candidates = []
    for line in docker("images", "--format",
                       "{{.Repository}}:{{.Tag}}").splitlines():
        tag = line.strip()
        if not (tag.startswith("router-orchestrator-hub:")
                or tag.startswith("router-orchestrator-xray:")):
            continue
        if tag in keep:
            continue
        image_id = docker("image", "inspect", "--format", "{{.Id}}", tag).strip()
        if image_id not in used_after_staging:
            candidates.append(tag)
    return staging, sorted(set(candidates))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    staging, candidates = plan()
    print("STAGING CONTAINERS:", staging)
    print("UNUSED PROJECT IMAGE TAGS:", candidates)
    print("REMNAWAVE AND GLOBAL DOCKER PRUNE: NOT TOUCHED")
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
    for name in staging:
        docker("rm", "-f", name)
        print("REMOVED STAGING:", name)
    for tag in candidates:
        # Recheck live references after staging removal, fail closed on races.
        current = plan()[1]
        if tag not in current:
            print("SKIPPED CHANGED IMAGE:", tag)
            continue
        docker("image", "rm", tag)
        print("REMOVED UNUSED PROJECT IMAGE TAG:", tag)
    print("KNOWN-GOOD XRAY AND PRE-CUTOVER HUB IMAGES: RETAINED")


if __name__ == "__main__":
    main()
