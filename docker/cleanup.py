#!/usr/bin/env python3
"""Project-only Docker cleanup, dry-run unless the operator passes --apply."""
from __future__ import annotations

import argparse
import json
import re

import manager as m
from stack_runtime import IMAGES, REVISION, Stack, digest, docker, open_settings
from unified_core_manager import (
    MAX_JOURNAL, PINNED_CORE, CoreManager, configured_stack_env, legacy_exclusion,
    load_record, preparation_directory, validate_transaction,
)
from unified_xray_adapter import Adapter
from unified_xray_state import read_private
import xray_lifecycle as life
import xray_releases as releases

KEEP_LIVE = ("router-orchestrator-hub", "router-orchestrator-xray", "router-orchestrator-nginx")
STAGING = ("router-orchestrator-staging", "router-orchestrator-proxy-staging")
PROJECT = "com.docker.compose.project"
SERVICE = "com.docker.compose.service"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_REF = re.compile(r"[a-z0-9][a-z0-9./_-]*(?::[A-Za-z0-9_.-]+|@sha256:[0-9a-f]{64})")
PROJECT_HUB = re.compile(r"router-orchestrator-hub:[A-Za-z0-9_.-]+")


def exists(path):
    return path.exists() or path.is_symlink()


def document(path):
    data = json.loads(read_private(path, MAX_JOURNAL))
    m.require(isinstance(data, dict), "Invalid cleanup metadata")
    return data


def image(reference):
    m.require(isinstance(reference, str) and IMAGE_REF.fullmatch(reference), "Invalid retained image reference")
    item, = json.loads(docker("image", "inspect", reference))
    m.require(IMAGE_ID.fullmatch(item["Id"]), "Invalid Docker image ID")
    return item


def retain(refs, ids, reference, expected=None):
    observed = image(reference)["Id"]
    m.require(expected is None or observed == expected, "Retained image identity changed")
    refs.add(reference)
    ids.add(observed)


def retain_release(stack, release, refs, ids):
    config, images = release["config"], release["images"]
    known = Stack(stack.path, config)
    m.require(known.name == stack.name and known.state == stack.state and known.services == stack.services and
              set(images) == set(stack.services) and
              release["revision"] == digest({key: value for key, value in release.items()
                                             if key not in {"revision", "model"}}),
              "Invalid retained Stack release")
    for service, image_id in images.items():
        m.require(IMAGE_ID.fullmatch(image_id), "Invalid Stack rollback image ID")
        retain(refs, ids, config[IMAGES[service]], image_id)


def keep_stack(adapter, refs, ids):
    env = adapter.env
    stack = Stack(env, open_settings(env))
    CoreManager(adapter).guard(stack)
    m.require(stack.config["RO_XRAY_BACKEND"] == "unified_adapter", "Managed Stack backend mismatch")
    record = load_record(stack)
    active = record["current"]
    pending = record["pending"]
    allowed = [active]
    if pending is not None:
        if pending.get("kind") == "core":
            validate_transaction(pending)
        else:
            m.require("kind" not in pending and "schema" in pending and "target_schema" in pending and
                      "snapshot" in pending, "Invalid pending Stack deployment")
        allowed.append(pending["target"])
    selected = next((release for release in allowed if release["config"] == stack.config), None)
    m.require(selected is not None, "Active env differs from Stack journal")
    stack.verify_release(selected)
    for release in allowed:
        retain_release(stack, release, refs, ids)
    if record.get("previous") is not None:
        retain_release(stack, record["previous"], refs, ids)
    history = record.get("core_known_good", [])
    m.require(isinstance(history, list) and len(history) <= 3, "Invalid Stack core history")
    for entry in history:
        m.require(PINNED_CORE.fullmatch(entry["image"]) and IMAGE_ID.fullmatch(entry["image_id"]),
                  "Invalid known-good core image")
        retain(refs, ids, entry["image"], entry["image_id"])

    objects = stack.inspect(*allowed)
    m.require(set(objects) == set(stack.services), "Managed Stack containers are missing")
    for service, item in objects.items():
        if service == "xray":
            owned = next((release for release in allowed
                          if release["revision"] == item["Config"]["Labels"].get(
                              REVISION)), None)
            m.require(owned is not None, "Xray release identity is unknown")
            adapter.owned_xray(stack, owned, item)
        if pending is None:
            m.require(item["State"]["Running"], "Required managed Stack container is stopped")
    return stack, pending


def verify_legacy_runtime(containers):
    for name, service in zip(KEEP_LIVE, ("hub", "xray", "nginx")):
        item = containers.get(name)
        m.require(item is not None and item["State"]["Running"] and
                  (item["Config"]["Labels"] or {}).get(PROJECT) == m.PROJECT and
                  (item["Config"]["Labels"] or {}).get(SERVICE) == service,
                  "Required legacy project container is missing, stopped or unowned")


def keep_legacy_images(refs, ids, *, applying):
    if exists(life.JOURNAL):
        record = document(life.JOURNAL)
        m.require(record["phase"] in {"committed", "rolled-back", "switching", "rolling-back"},
                  "Invalid legacy core transaction")
        m.require(not applying or record["phase"] in {"committed", "rolled-back"},
                  "Pending legacy core transaction")
        for field in ("old_image", "new_image"):
            m.require(life.IMAGE_RE.fullmatch(record[field]) and IMAGE_ID.fullmatch(record[field + "_id"]),
                      "Invalid legacy rollback image")
            retain(refs, ids, record[field], record[field + "_id"])
        for entry in record.get("known_good", []):
            m.require(life.IMAGE_RE.fullmatch(entry["image"]) and IMAGE_ID.fullmatch(entry["image_id"]),
                      "Invalid legacy known-good image")
            retain(refs, ids, entry["image"], entry["image_id"])


def inventory():
    identifiers = docker("ps", "-aq", "--no-trunc").split()
    if not identifiers:
        return {}
    return {item["Name"].lstrip("/"): item
            for item in json.loads(docker("container", "inspect", *identifiers))}


def owned_image(tag, item, manifests):
    if PROJECT_HUB.fullmatch(tag):
        labels = item["Config"].get("Labels") or {}
        return re.fullmatch(r"[a-f0-9]{64}", labels.get(m.SOURCE_LABEL, "")) is not None
    if not life.IMAGE_RE.fullmatch(tag):
        return False
    version = "v" + tag.split(":", 1)[1]
    if not releases.VERSION.fullmatch(version):
        return False
    path = manifests / (version[1:] + ".json")
    if not exists(path):
        return False
    record = document(path)
    pinned = record["official_digest_ref"]
    return (record["version"] == version and record["image"] == tag and
            record["image_id"] == item["Id"] and record["validation"] == "config-only" and
            PINNED_CORE.fullmatch(pinned) is not None and pinned in (item.get("RepoDigests") or []))


def plan(env, *, applying=False):
    m.require(not m.pending_transaction(), "Pending HUB-only deployment")
    m.require(not exists(life.TRANSPORT_HOLD), "Xray maintenance HOLD is active")
    refs, ids = {"router-orchestrator-hub:release"}, set()
    baseline = life.ROOT / "unified-candidate/cutover-rollback/images.json"
    if exists(baseline):
        data = document(baseline)
        retain(refs, ids, data["hub"])
        m.require(IMAGE_ID.fullmatch(data["xray_id"]), "Invalid cutover rollback image")
        retain(refs, ids, data["xray_id"], data["xray_id"])

    keep_legacy_images(refs, ids, applying=applying)
    if exists(m.RELEASE_STATE):
        history = document(m.RELEASE_STATE)
        for field in ("current", "previous"):
            image_id = history[field + "_id"]
            m.require(IMAGE_ID.fullmatch(image_id), "Invalid HUB rollback image ID")
            retain(refs, ids, history[field + "_image"], image_id)
    containers = inventory()
    if env is None:
        verify_legacy_runtime(containers)
        manifests = releases.STAGED
    else:
        stack, pending = keep_stack(Adapter(env), refs, ids)
        m.require(not applying or pending is None, "Pending Stack transaction")
        manifests = preparation_directory(stack)

    staging = {}
    for name in STAGING:
        item = containers.get(name)
        if item is not None:
            labels = item["Config"]["Labels"] or {}
            if labels.get(PROJECT) == name:
                staging[name] = item["Id"]
    ids.update(item["Image"] for name, item in containers.items() if name not in staging)
    refs.update(item["Config"]["Image"] for name, item in containers.items() if name not in staging)
    candidates = {}
    tags = set(docker("images", "--format", "{{.Repository}}:{{.Tag}}").splitlines())
    if "router-orchestrator-hub:release" in tags:
        retain(refs, ids, "router-orchestrator-hub:release")
    for tag in sorted(tags):
        if tag in refs or not (PROJECT_HUB.fullmatch(tag) or life.IMAGE_RE.fullmatch(tag)):
            continue
        item = image(tag)
        if item["Id"] not in ids and owned_image(tag, item, manifests):
            candidates[tag] = item["Id"]
    return staging, candidates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    env = configured_stack_env()
    with m.deployment_lock(), legacy_exclusion():
        staging, candidates = plan(env, applying=args.apply)
        print("STAGING CONTAINERS:", sorted(staging))
        print("UNUSED PROJECT IMAGE TAGS:", sorted(candidates))
        if not args.apply:
            print("DRY-RUN ONLY: no Docker objects removed")
            return
        state = document(life.ROOT / "data/release-acceptance.json")
        required = ("fleet_verified", "rollback_verified", "reboot_verified",
                    "backup_restorable", "global_logs_verified", "wol_verified")
        m.require(all(state.get(key) is True for key in required), "Release acceptance gates not passed")
        for name, identifier in staging.items():
            if plan(env, applying=True)[0].get(name) == identifier:
                docker("container", "rm", "-f", identifier)
                print("REMOVED STAGING:", name)
        for tag, identifier in candidates.items():
            if plan(env, applying=True)[1].get(tag) == identifier:
                docker("image", "rm", tag)
                print("REMOVED UNUSED PROJECT IMAGE TAG:", tag)


if __name__ == "__main__":
    main()
