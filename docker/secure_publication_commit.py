"""Finalize a durable server publication decision without assigning routers.

Only called by the privileged publication worker while holding the common
Xray lifecycle and ingress locks. The FILES_ACCEPTED journal must already
be durable and bound to the exact private artifact and original inventory.
"""
from __future__ import annotations

import json
from pathlib import Path
import secrets

import secure_profile_catalog as catalog
from secure_profile_store import load_artifact
from secure_publication import validate_inventory
from secure_ingress_inventory import read_inventory
from secure_publication_transaction import atomic, sha, read_regular
from transport_profiles import ProfileError


def binding_for(plan, inventory, *, profile_id, revision):
    """Construct metadata before any file mutation, with no secret config."""
    catalog._identifier(profile_id)
    if type(revision) is not int or revision < 1:
        raise ProfileError("invalid revision")
    validate_inventory(inventory)
    return {"profile_id": profile_id, "revision": revision,
            "artifact_sha256": plan["artifact_sha256"],
            "original_inventory": inventory,
            "routes": plan["routes"], "tag": plan["tag"]}


def finalize_publication(record, *, db, inventory_path, xray_path, nginx_path,
                         nginx_digest, verify_runtime):
    """Idempotent forward commit: inventory first, catalog visibility last.

    A crash between metadata writes can be retried. Never revert the
    accepted runtime after the revision could have been assigned. The
    independent recovery worker must block all competing mutations until
    this decision is finalized. A failed runtime check leaves it pending.
    """
    if record.get("phase") != "FILES_ACCEPTED":
        raise ProfileError("no durable accepted publication decision")
    binding = record.get("binding")
    if not isinstance(binding, dict) or set(binding) != {
        "profile_id", "revision", "artifact_sha256",
        "original_inventory", "routes", "tag"
    }:
        raise ProfileError("invalid publication binding")
    artifact, digest, published = load_artifact(db, binding["profile_id"], binding["revision"])
    if not secrets.compare_digest(digest, binding["artifact_sha256"]):
        raise ProfileError("accepted artifact digest mismatch")
    original = binding["original_inventory"]
    validate_inventory(original)
    combined = sorted(original["managed_routes"] + binding["routes"], key=lambda r:r["sni"])
    candidate = dict(original, managed_routes=combined)
    validate_inventory(candidate)
    expected_map = "".join(f"{r['sni']} {r['upstream']};\n" for r in combined).encode()
    xray = read_regular(xray_path)[0]
    fragment = read_regular(nginx_path)[0]
    for name, data in (("xray",xray),("nginx",fragment)):
        if sha(data) != record["files"][name]["after"]:
            raise ProfileError("accepted publication file digest mismatch")
    if fragment != expected_map:
        raise ProfileError("managed fragment differs from accepted routes")
    config = json.loads(xray)
    tag = f"ro-secure-{binding['profile_id']}-r{binding['revision']}"
    inbounds = [i for i in config["inbounds"] if i.get("tag") == tag]
    if binding["tag"] != tag or len(inbounds) != 1:
        raise ProfileError("accepted inbound identity missing")
    from transport_profiles import render_stream_settings
    stream = render_stream_settings(artifact["profile"])
    if original.get("proxy_protocol", False):
        stream["sockopt"] = {"acceptProxyProtocol": True}
    expected = {"tag":tag,"listen":"127.0.0.1","port":artifact["profile"]["port"],
                "protocol":"vless","settings":{"decryption":"none","clients":[]},
                "streamSettings":stream}
    if inbounds[0] != expected:
        raise ProfileError("accepted inbound differs from sealed profile")
    from secure_ingress import plan_ingress
    routes = plan_ingress(artifact["profile"],public_port=artifact["public_port"])["routes"]
    if routes != binding["routes"]:
        raise ProfileError("accepted route differs from sealed profile")
    candidate["nginx_config_sha256"] = nginx_digest(original["mode"])
    validate_inventory(candidate)
    current = read_inventory(Path(inventory_path))
    if current not in (original,candidate):
        raise ProfileError("ingress inventory changed outside publication")
    if published and current != candidate:
        raise ProfileError("visible revision has inconsistent inventory")
    if not verify_runtime():
        raise ProfileError("accepted runtime could not be reverified; metadata commit blocked")
    if nginx_digest(original["mode"]) != candidate["nginx_config_sha256"]:
        raise ProfileError("nginx changed during metadata finalization")
    if current == original:
        atomic(Path(inventory_path),json.dumps(candidate,indent=2).encode())
    if not published:
        catalog.publish_verified(db,binding["profile_id"],binding["revision"],
                                 verified_artifact_sha256=digest)
    # Must commit before the journal can become ACCEPTED.
    db.commit()
    return {"profile_id":binding["profile_id"],"revision":binding["revision"],
            "published":True,"routers_changed":False}
