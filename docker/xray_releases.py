#!/usr/bin/env python3
"""Fetch stable Xray releases and prepare a pinned, local Docker candidate.

No auto-update. The fetched image is never started on production ports and
its tag cannot be supplied from untrusted web content.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
import urllib.error

RELEASES = "https://api.github.com/repos/XTLS/Xray-core/releases?per_page=10"
VERSION = re.compile(r"^v([0-9]{1,4}\.[0-9]{1,3}\.[0-9]{1,3})$")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REGISTRY = "ghcr.io/xtls/xray-core"
STAGED = Path("/opt/router-orchestrator/data/xray-candidates")
MAX_BYTES = 4 * 1024 * 1024


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def stable_release_tags():
    # Bound each page independently: release documents can contain large
    # asset lists. Fetch only as many pages as needed for the stable choices.
    tags = []
    for page in range(1, 4):
        request = urllib.request.Request(
            RELEASES + "&page=" + str(page),
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "Router-Orchestrator-release-checker"})
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                require(response.status == 200, "GitHub releases unavailable")
                raw = response.read(MAX_BYTES + 1)
                require(len(raw) <= MAX_BYTES, "GitHub release page exceeds limit")
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                raise RuntimeError("GitHub API denied request or rate limit reached (HTTP "+str(exc.code)+")") from None
            raise RuntimeError("GitHub release API HTTP "+str(exc.code)) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("GitHub release API connection failed or timed out") from None
        try:
            items = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RuntimeError("GitHub release API returned invalid JSON") from None
        require(isinstance(items, list), "invalid releases document")
        for item in items:
            if (isinstance(item,dict) and not item.get("draft") and not item.get("prerelease")
                    and isinstance(item.get("tag_name"),str) and VERSION.fullmatch(item["tag_name"])
                    and item["tag_name"] not in tags):
                tags.append(item["tag_name"])
        if len(items)<10 or len(tags)>=8:
            break
    require(tags,"no stable releases in reviewed GitHub pages")
    return tags


def command(*args, timeout=180):
    import manager as m

    return subprocess.run(("/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args[1:]),
                          env=m.local_docker_env(), pass_fds=m.inherited_lock(),
                          capture_output=True, text=True, timeout=timeout)


def prepare(version):
    raise RuntimeError("Core preparation requires a managed unified Docker stack")


def prepare_candidate(version, lifecycle, *, staged=None):
    tags = stable_release_tags()
    require(version in tags, "requested version is not a published stable GitHub release")
    tag = version[1:]
    official = REGISTRY + ":" + tag
    local = "router-orchestrator-xray:" + tag
    pulled = command("docker", "pull", "--platform", "linux/amd64", official,
                     timeout=240)
    require(pulled.returncode == 0, "official OCI image unavailable for stable tag")
    details = command("docker", "image", "inspect", official)
    require(details.returncode == 0, "official image inspection failed")
    metadata, = json.loads(details.stdout)
    digest = metadata.get("Id", "")
    require(metadata.get("Architecture") == "amd64" and metadata.get("Os") == "linux"
            and DIGEST.fullmatch(digest), "unexpected image platform or digest")
    pinned_refs = {ref for ref in metadata.get("RepoDigests") or []
                   if ref.startswith(REGISTRY + "@")
                   and DIGEST.fullmatch(ref.split("@", 1)[1])}
    require(len(pinned_refs) == 1, "official registry digest unavailable or ambiguous")
    pinned, = pinned_refs
    verified = command("docker", "pull", "--platform", "linux/amd64", pinned, timeout=240)
    require(verified.returncode == 0, "official pinned image unavailable")
    inspected = command("docker", "image", "inspect", "--format", "{{.Id}}", pinned)
    require(inspected.returncode == 0 and inspected.stdout.strip() == digest,
            "official image digest mismatch")
    # All executable checks and local tagging refer to the verified image ID.
    result = command("docker", "run", "--rm", "--pull", "never", "--network", "none",
                     "--cap-drop", "ALL", digest, "version", timeout=35)
    fields = result.stdout.splitlines()[0].split() if result.stdout else []
    require(result.returncode == 0 and fields[:2] == ["Xray", tag],
            "official image version does not match selected release")
    lifecycle.candidate_test(digest)
    # A repeated prepare may refresh a manifest for the same image, but
    # must never replace a retained version tag with different bytes.
    current = command("docker", "image", "inspect", "--format", "{{.Id}}", local)
    require(current.returncode != 0 or current.stdout.strip() == digest,
            "local version tag already points to a different image")
    tagged = command("docker", "tag", digest, local)
    require(tagged.returncode == 0, "verified image could not be tagged")
    if staged is None:
        staged = STAGED
    staged.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = staged / (tag + ".json")
    document = {"version": version, "image": local, "image_id": digest,
                "official_image": official, "official_digest_ref": pinned, "validation": "config-only",
                "live_routers_verified": False}
    lifecycle.atomic(destination, (json.dumps(document, indent=2) + "\n").encode())
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
    prepare(args.version or stable_release_tags()[0])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("RELEASE PREPARATION ERROR:", exc, file=sys.stderr)
        sys.exit(1)
