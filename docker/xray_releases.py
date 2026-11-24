#!/usr/bin/env python3
"""Fetch stable Xray releases and prepare a pinned, local Docker candidate.

No auto-update. The fetched image is never started on production ports and
its tag cannot be supplied from untrusted web content.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

RELEASES = "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=60"
VERSION = re.compile(r"^v([0-9]{1,4}\.[0-9]{1,3}\.[0-9]{1,3})$")
REGISTRY = "ghcr.io/xtls/xray-core"
STAGED = Path("/opt/router-orchestrator/data/xray-candidates")
MAX_BYTES = 4 * 1024 * 1024


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def stable_release_tags():
    request = urllib.request.Request(
        RELEASES, headers={"Accept": "application/vnd.github+json",
                           "User-Agent": "Router-Orchestrator-release-checker"})
    with urllib.request.urlopen(request, timeout=12) as response:
        require(response.status == 200, "GitHub releases unavailable")
        raw = response.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES, "GitHub response exceeds limit")
    items = json.loads(raw)
    require(isinstance(items, list), "invalid releases document")
    return [item["tag_name"] for item in items if isinstance(item, dict)
            and not item.get("draft") and not item.get("prerelease")
            and isinstance(item.get("tag_name"), str)
            and VERSION.fullmatch(item["tag_name"])]


def command(*args, timeout=180):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def prepare(version):
    require(os.geteuid() == 0, "root required")
    tags = stable_release_tags()
    require(version in tags, "requested version is not a published stable GitHub release")
    tag = version[1:]
    official = REGISTRY + ":" + tag
    local = "router-orchestrator-xray:" + tag
    pulled = command("docker", "pull", "--platform", "linux/amd64", official,
                     timeout=240)
    require(pulled.returncode == 0, "official OCI image unavailable for stable tag")
    details = command("docker", "image", "inspect", "--format",
                      "{{.Architecture}}|{{.Id}}", official)
    require(details.returncode == 0, "official image inspection failed")
    architecture, digest = details.stdout.strip().split("|", 1)
    require(architecture == "amd64" and digest.startswith("sha256:"),
            "unexpected image architecture or digest")
    # Verify executable compatibility; the image is network-isolated.
    result = command("docker", "run", "--rm", "--network", "none",
                     "--cap-drop", "ALL", official, "version", timeout=35)
    require(result.returncode == 0 and ("Xray " + tag) in result.stdout,
            "official image version does not match selected release")
    command("docker", "tag", official, local)
    # Reuse the exact existing production Xray config with immutable mounts.
    candidate = command("docker", "run", "--rm", "--network", "none",
                        "--cap-drop", "ALL",
                        "-v", "/etc/xray:/etc/xray:ro",
                        "-v", "/usr/local/share/xray:/usr/local/share/xray:ro",
                        *(["-v", "/opt/router-orchestrator/tls:/etc/router-orchestrator/tls:ro"]
                          if Path("/opt/router-orchestrator/tls").is_dir() else []),
                        local, "run", "-test",
                        "-config", "/etc/xray/owrt-remote.json", timeout=45)
    require(candidate.returncode == 0, "candidate core rejected current config")
    STAGED.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = STAGED / (tag + ".json")
    document = {"version": version, "image": local, "image_id": digest,
                "official_image": official, "validation": "config-only",
                "live_routers_verified": False}
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(document, out, indent=2)
        out.write("\n")
    print(json.dumps(document, indent=2))
    print("CANDIDATE PREPARED: do not treat config-only validation as live router acceptance")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["list", "prepare"])
    parser.add_argument("--version", help="vNN.NN.NN (default: latest stable)")
    args = parser.parse_args()
    if args.action == "list":
        print("\n".join(stable_release_tags()))
        return
    available = stable_release_tags()
    require(available, "no stable releases found")
    prepare(args.version or available[0])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("RELEASE PREPARATION ERROR:", exc, file=sys.stderr)
        sys.exit(1)
