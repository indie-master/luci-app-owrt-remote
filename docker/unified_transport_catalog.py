"""Stack-local Transport Manager drafts and review artifacts."""

import hashlib
import json
from pathlib import Path
import re
import secrets
import time
import uuid

from secure_certificate_inventory import CONTAINER_TLS, inspect_pair, mounted_certificate
from secure_ingress import domain, plan_ingress, render_map_entries
from secure_ingress_inventory import live_tcp_ports
from secure_profile_catalog import _identifier, profile_selection
from secure_profile_store import (
    connection, discard_draft, list_revisions, load_artifact, save_allocated_draft,
)
from stack_runtime import protected
from transport_profiles import ProfileError, render_router_outbound
from unified_xray_state import MAX_CONFIG, encode, read_private
import unified_transport_runtime as lifecycle

FIELDS = {
    "wss": {"path"},
    "xhttp": {"path"},
    "grpc": {"service_name"},
    "reality-tcp": {"target_host", "target_port", "server_names"},
}
COMMON = {"name", "transport", "port", "server_name"}
OPERATIONS = {
    "transport.list": set(),
    "transport.draft": {"profile", "label", "remote_host", "public_port"},
    "transport.detail": {"profile_id", "revision"},
    "transport.confirm_delete": {"profile_id", "revision"},
    "transport.delete": {"profile_id", "revision", "confirmation"},
    "transport.validate": {"profile_id", "revision"},
    "transport.plan": {"profile_id", "revision"},
    "transport.stage": {"profile_id", "revision"},
    "transport.approve": {"profile_id", "revision", "stage_sha256", "confirmed"},
    **{op: {"profile_id", "revision", "confirmed"} for op in lifecycle.OPERATIONS},
    "transport.publish": {"profile_id", "revision", "stage_sha256", "confirmed"},
    "transport.assign": {"router_id", "profiles", "confirmed"},
    "transport.unassign": {"router_id", "confirmed"},
    "transport.router_profiles": {"router_id"},
    "transport.router_portals": {"router_id", "profile"},
}
MAX_DRAFTS = 24
CONFIRMATION_TTL = 600


def validate_request(op, args):
    if op == "transport.draft":
        validate_draft_input(args["profile"])
    if "profile_id" in args:
        _identifier(args["profile_id"])
        if type(args["revision"]) is not int or not 1 <= args["revision"] <= 2**63 - 1:
            raise ProfileError("invalid revision")
    if op in lifecycle.OPERATIONS | {"transport.assign", "transport.unassign"} and args["confirmed"] is not True:
        raise ProfileError("explicit confirmation is required")
    if op == "transport.assign":
        profile_selection(args["profiles"])
    if op == "transport.router_portals" and args["profile"] is not None:
        ref = args["profile"]
        if not isinstance(ref, dict):
            raise ProfileError("invalid profile reference")
        assignment = ref.get("assignment_id")
        if "assignment_id" in ref and (not isinstance(assignment, str) or
                                       not re.fullmatch(r"[0-9a-f]{64}", assignment)):
            raise ProfileError("invalid assignment identity")
        profile_selection([{key: value for key, value in ref.items() if key != "assignment_id"}])
    if op in {"transport.approve", "transport.publish"}:
        digest = args["stage_sha256"]
        if args["confirmed"] is not True or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ProfileError("explicit approval of the staged digest is required")


def initialize_stage(db):
    db.execute("""CREATE TABLE IF NOT EXISTS transport_stage_receipts(
        profile_id TEXT NOT NULL, revision INTEGER NOT NULL,
        stage_sha256 TEXT NOT NULL, staged_at INTEGER NOT NULL, approved_at INTEGER,
        PRIMARY KEY(profile_id, revision))""")


def stage_status(db, profile_id, revision):
    row = db.execute("""SELECT stage_sha256, staged_at, approved_at
        FROM transport_stage_receipts WHERE profile_id=? AND revision=?""",
        (profile_id, revision)).fetchone()
    return dict(zip(("stage_sha256", "staged_at", "approved_at"), row)) if row else None


def record_stage(db, op, ref, stage_sha256, requested_digest=None):
    receipt = stage_status(db, **ref)
    key = (ref["profile_id"], ref["revision"])
    now = int(time.time())
    if op == "transport.approve":
        if not receipt or receipt["stage_sha256"] != stage_sha256 or requested_digest != stage_sha256:
            raise ProfileError("staged configuration changed; stage again before approval")
        db.execute("""UPDATE transport_stage_receipts SET approved_at=COALESCE(approved_at, ?)
            WHERE profile_id=? AND revision=?""", (now, *key))
    elif not receipt or receipt["stage_sha256"] != stage_sha256:
        db.execute("INSERT OR REPLACE INTO transport_stage_receipts VALUES (?,?,?,?,NULL)",
                   (*key, stage_sha256, now))
    return stage_status(db, **ref)


def tls_binding(stack, profile):
    if profile["transport"] == "reality-tcp":
        return None
    root = Path(stack.config["RO_TLS_DIR"])
    paths = [Path(profile[field]).relative_to(CONTAINER_TLS)
             for field in ("certificate_file", "key_file")]
    checked = inspect_pair(profile["server_name"], *(root / path for path in paths), roots=[root])
    hashes = {}
    for field in ("certificate_file", "key_file"):
        with Path(checked[field]).open("rb") as stream:
            data = stream.read(MAX_CONFIG + 1)
        if len(data) > MAX_CONFIG:
            raise ProfileError("TLS material exceeds size limit")
        hashes[field] = hashlib.sha256(data).hexdigest()
    return hashes


def draft_deletion(db, op, profile_id, revision, confirmation=None, record=None):
    _, _, published = load_artifact(db, profile_id, revision)
    if published:
        ref = (profile_id, revision)
        rollback = record.get("profile_rollbacks", {}).get(lifecycle.ref_key(ref))
        if (rollback is not None or any(lifecycle.key(entry) == ref for entry in record.get("profiles", []))
                or lifecycle.used(db, ref)):
            raise ProfileError("active, assigned or rollback revision cannot be deleted")
    db.execute("""CREATE TABLE IF NOT EXISTS transport_delete_confirmations(
        profile_id TEXT NOT NULL, revision INTEGER NOT NULL, digest TEXT NOT NULL,
        expires REAL NOT NULL, PRIMARY KEY(profile_id, revision))""")
    now = time.time()
    if op == "transport.confirm_delete":
        token = secrets.token_urlsafe(32)
        db.execute("DELETE FROM transport_delete_confirmations WHERE expires<=?", (now,))
        db.execute("INSERT OR REPLACE INTO transport_delete_confirmations VALUES (?,?,?,?)",
                   (profile_id, revision, hashlib.sha256(token.encode()).hexdigest(),
                    now + CONFIRMATION_TTL))
        return {"confirmation": token, "expires_in": CONFIRMATION_TTL}
    if not isinstance(confirmation, str) or len(confirmation) != 43:
        raise ProfileError("invalid deletion confirmation")
    used = db.execute("""DELETE FROM transport_delete_confirmations
        WHERE profile_id=? AND revision=? AND digest=? AND expires>?""",
        (profile_id, revision, hashlib.sha256(confirmation.encode()).hexdigest(), now))
    if used.rowcount != 1:
        raise ProfileError("deletion confirmation missing or expired")
    if published:
        db.execute("UPDATE secure_profile_revisions SET published=0 WHERE profile_id=? AND revision=?",
                   (profile_id, revision))
    discard_draft(db, profile_id, revision)
    initialize_stage(db)
    db.execute("DELETE FROM transport_stage_receipts WHERE profile_id=? AND revision=?",
               (profile_id, revision))
    return {"deleted": True}


def validate_draft_input(profile):
    if not isinstance(profile, dict) or not isinstance(profile.get("transport"), str):
        raise ProfileError("invalid transport profile")
    fields = FIELDS.get(profile["transport"])
    if fields is None or set(profile) != COMMON | fields:
        raise ProfileError("invalid fields for transport type")


def handle(adapter, stack, release, state, op, args, *, container=None, runtime=None):
    root = stack.transport_store
    if op == "transport.list" and not root.exists() and not root.is_symlink():
        return {"revisions": [], "public_port": stack.plan["public"]}
    if op == "transport.router_profiles" and not root.exists() and not root.is_symlink():
        return {"profiles": []}
    if op == "transport.router_portals":
        return runtime.router_portals(args["router_id"], args["profile"])
    if op == "transport.draft":
        root.mkdir(mode=0o700, exist_ok=True)
    with connection(protected(root)) as db:
        if op == "transport.router_profiles":
            return runtime.router_profiles(db, args["router_id"])
        if op in {"transport.assign", "transport.unassign"}:
            return runtime.assign(db, args["router_id"], args.get("profiles"))
        if op == "transport.list":
            revisions = list_revisions(db)
            record = state.load()
            active = {lifecycle.key(entry) for entry in record.get("profiles", [])}
            rollbacks = record.get("profile_rollbacks", {})
            rollback_profiles = {lifecycle.key(entry) for entry in rollbacks.values() if entry is not None}
            for item in revisions:
                ref = (item["profile_id"], item["revision"])
                item["active"] = ref in active
                item["rollback_available"] = lifecycle.ref_key(ref) in rollbacks
                item["delete_allowed"] = ref not in active | rollback_profiles and not lifecycle.used(db, ref)
            has_receipts = db.execute("""SELECT 1 FROM sqlite_master
                WHERE type='table' AND name='transport_stage_receipts'""").fetchone()
            if has_receipts:
                for item in revisions:
                    item["stage"] = stage_status(db, item["profile_id"], item["revision"])
            assignments = {router: json.loads(data) for router, data in db.execute(
                "SELECT router_id, desired_profiles FROM secure_router_preferences")}
            for entry in record.get("profiles", []):
                for router, pair in entry.get("assignments", {}).items():
                    for ref in assignments.get(router, []):
                        if (ref["profile_id"], ref["revision"]) == lifecycle.key(entry):
                            ref["assignment_id"] = lifecycle.assignment_id(pair)
            return {"revisions": revisions, "assignments": assignments,
                    "public_port": stack.plan["public"]}
        if op in {"transport.confirm_delete", "transport.delete"}:
            return draft_deletion(db, op, **args, record=state.load())
        if op == "transport.detail":
            artifact, _, _ = load_artifact(db, **args)
            profile = artifact["profile"]
            fields = COMMON | FIELDS[profile["transport"]]
            return {"profile": {key: profile[key] for key in fields},
                    "remote_host": artifact["remote_host"], "public_port": artifact["public_port"]}

        if op in lifecycle.OPERATIONS - {"transport.publish"}:
            ref = (args["profile_id"], args["revision"])
            artifact, digest, published = load_artifact(db, *ref)
            return runtime.change(db, op, ref, artifact, digest, published)

        base_config = read_private(state.config, MAX_CONFIG)
        base = json.loads(base_config)
        occupied = {port for key in ("hub", "wss", "tls", "health", "http", "public")
                    if (port := stack.plan[key]) is not None}
        occupied.update(stack.plan["routes"].values())
        occupied.update(live_tcp_ports())
        occupied.update(item["port"] for item in base["inbounds"] if type(item.get("port")) is int)
        stack_routes = [{"public_port": stack.plan["public"], "sni": name}
                        for name in (stack.plan["host"], *stack.plan["routes"])]
        if op == "transport.draft":
            revisions = list_revisions(db)
            if len(revisions) >= MAX_DRAFTS:
                raise ProfileError("catalog capacity reached")
            profile = dict(args["profile"])
            if args["public_port"] != stack.plan["public"]:
                raise ProfileError("public port is outside the configured ingress")
            # The only certificate inventory this adapter can use is its own mount.
            if profile["transport"] != "reality-tcp":
                profile.update(mounted_certificate(profile["server_name"],
                                                   root=Path(stack.config["RO_TLS_DIR"])))
            elif not isinstance(profile["server_names"], list):
                raise ProfileError("invalid SNI names")
            names = (profile["server_names"] if profile["transport"] == "reality-tcp"
                     else [profile["server_name"]])
            proposed = {domain(name) for name in names}
            used = {route["sni"] for route in stack_routes}
            used.update(name for item in revisions if item["profile_id"] != profile["name"]
                        for name in item["server_names"])
            if proposed & used:
                raise ProfileError("SNI already belongs to another route")
            if profile["port"] != 0 and (type(profile["port"]) is not int or profile["port"] < 1024):
                raise ProfileError("backend listener requires an unprivileged port")
            return {"draft": save_allocated_draft(db, **dict(args, profile=profile),
                                                  occupied_ports=occupied)}

        ref = {field: args[field] for field in ("profile_id", "revision")}
        artifact, digest, published = load_artifact(db, **ref)
        stage_operation = op in {"transport.stage", "transport.approve", "transport.publish"}
        if stage_operation and (published or not container):
            raise ProfileError("staging requires an unpublished revision and verified runtime identity")
        profile = artifact["profile"]
        if artifact["public_port"] != stack.plan["public"]:
            raise ProfileError("public port is outside the configured ingress")
        plan = plan_ingress(profile, public_port=artifact["public_port"],
                            occupied_ports=occupied, existing_routes=stack_routes)
        identity = str(uuid.uuid5(uuid.NAMESPACE_OID, digest))
        tag = "ro-preview-" + profile["name"]
        if any(item.get("tag") == tag for item in base["inbounds"]):
            raise ProfileError("candidate listener tag conflicts")
        base["inbounds"].append({
            "tag": tag, "listen": plan["listen"], "port": plan["backend_port"],
            "protocol": "vless", "settings": {"clients": [{"id": identity}], "decryption": "none"},
            "streamSettings": plan["streamSettings"],
        })
        client = render_router_outbound(dict(profile, port=artifact["public_port"]),
                                        router_uuid=identity, remote_host=artifact["remote_host"])
        # Use an isolated Freedom route so reverse traffic in the syntax fixture
        # cannot select the VLESS outbound as the default route.
        client_config = {"outbounds": [
            {"tag": "preview-local", "protocol": "freedom",
             "settings": {"redirect": "127.0.0.1:80"}},
            client,
        ], "routing": {"rules": [
            {"type": "field", "inboundTag": ["reverse-in"], "outboundTag": "preview-local"},
        ]}}
        server_config = encode(base)
        client_bytes = encode(client_config)
        if len(server_config) > MAX_CONFIG:
            raise ProfileError("candidate exceeds configuration size limit")
        tls = tls_binding(stack, profile) if stage_operation else None
        adapter.test_config(stack, release, server_config)
        adapter.test_config(stack, release, client_bytes)
        result = {"xray_validated": True, "client_syntax_validated": True,
                  "artifact_sha256": digest, "applied": False}
        if op == "transport.plan" or stage_operation:
            result.update(routes=plan["routes"], nginx_stream_map=render_map_entries(plan),
                          stack_mode=stack.plan["mode"], live_listener_verified=False)
        if stage_operation:
            stack.guard()
            stack.verify_release(release)
            if (adapter.owned_xray(stack, release, stack.inspect(release)["xray"]) != container or
                    read_private(state.config, MAX_CONFIG) != base_config or
                    tls_binding(stack, profile) != tls or plan["backend_port"] in live_tcp_ports()):
                raise ProfileError("runtime inputs changed during validation")
            binding = {
                "version": 1, **ref, "artifact_sha256": digest,
                "release": release["revision"], "xray_container": container,
                "baseline_sha256": hashlib.sha256(base_config).hexdigest(),
                "server_sha256": hashlib.sha256(server_config).hexdigest(),
                "client_sha256": hashlib.sha256(client_bytes).hexdigest(),
                "tls": tls, "ingress_mode": result["stack_mode"],
                "ingress_map": result["nginx_stream_map"],
            }
            stage_sha256 = hashlib.sha256(encode(binding)).hexdigest()
            db.execute("BEGIN IMMEDIATE")
            initialize_stage(db)
            _, current_digest, current_published = load_artifact(db, **ref)
            if current_digest != digest or current_published:
                raise ProfileError("artifact changed during validation")
            if op == "transport.publish":
                receipt = stage_status(db, **ref)
                if (not receipt or not receipt["approved_at"] or
                        receipt["stage_sha256"] != stage_sha256 or args["stage_sha256"] != stage_sha256):
                    raise ProfileError("current stage must be approved before publication")
                return runtime.change(db, op, (ref["profile_id"], ref["revision"]), artifact, digest, published)
            result["stage"] = record_stage(db, op, ref, stage_sha256, args.get("stage_sha256"))
            result["published"] = False
        return result
