"""Durable Xray enrollment state, independent of the stack release journal."""

import json
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid

import manager as m
from secure_ingress import plan_ingress

MAX_CONFIG = 2 * 1024**2
MAX_ROUTERS = 512
BACKEND = "unified_adapter"
ROUTER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
TOKEN = re.compile(r"[a-f0-9]{32}")


def encode(document):
    return (json.dumps(document, ensure_ascii=True, allow_nan=False) + "\n").encode()


def read_private(path, size):
    from stack_runtime import protected

    with protected(path).open("rb") as stream:
        info = os.fstat(stream.fileno())
        m.require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and
                  not info.st_mode & 0o007 and info.st_size <= size,
                  "Unsafe or oversized Xray state file")
        return stream.read(size + 1)


def router_spec(value):
    m.require(isinstance(value, dict) and set(value) == {"id", "clients", "ports", "targets"},
              "Invalid router parameters")
    name = value["id"]
    m.require(isinstance(name, str) and ROUTER_ID.fullmatch(name), "Invalid router ID")
    for key in ("clients", "ports", "targets"):
        m.require(isinstance(value[key], list) and len(value[key]) == 2, "Invalid router pair")
    m.require(all(isinstance(uid, str) and len(uid) == 36 and str(uuid.UUID(uid)) == uid
                  for uid in value["clients"]) and len(set(value["clients"])) == 2,
              "Invalid client identity")
    m.require(all(type(port) is int and 1024 <= port <= 65535 for port in value["ports"]) and
              len(set(value["ports"])) == 2, "Invalid listener port")
    for target in value["targets"]:
        m.require(isinstance(target, list) and len(target) == 2 and
                  isinstance(target[0], str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", target[0]) and
                  type(target[1]) is int and 1 <= target[1] <= 65535, "Invalid reverse target")
    return value


def render(baseline, routers, plan):
    m.require(len(routers) <= MAX_ROUTERS, "Managed router limit exceeded")
    if not routers:
        return baseline
    config = json.loads(baseline)
    inbounds = config["inbounds"]
    wss, = [item for item in inbounds if item["port"] == plan["wss"]]
    clients = wss["settings"].setdefault("clients", [])
    routing = config.setdefault("routing", {}).setdefault("rules", [])
    reserved_tags = {item.get("tag") for item in inbounds + config.get("outbounds", [])}
    for rule in routing:
        reserved_tags.update(rule.get("inboundTag") or ())
        reserved_tags.add(rule.get("outboundTag"))
    all_clients = [client for item in inbounds
                   for client in item.get("settings", {}).get("clients", [])]
    reserved_tags.update(client.get("reverse", {}).get("tag") for client in all_clients)
    used_ids = {client.get("id") for client in all_clients}
    used_emails = {client.get("email") for client in all_clients}
    used_ports = {item["port"] for item in inbounds}
    used_ports.update(plan[key] for key in ("hub", "http", "public", "tls", "health"))
    managed_rules = []
    for router_id, spec in sorted(routers.items()):
        router_spec(spec)
        m.require(spec["id"] == router_id, "Invalid managed router ID")
        for index, (prefix, suffix) in enumerate((("entry-", ""), ("ssh-", "-ssh"))):
            tag, reverse = prefix + router_id, "reverse-" + router_id + suffix
            email = router_id + suffix + "@owrt-remote"
            uid, port = spec["clients"][index], spec["ports"][index]
            host, target = spec["targets"][index]
            m.require(tag not in reserved_tags and reverse not in reserved_tags and
                      uid not in used_ids and email not in used_emails and port not in used_ports,
                      "Router conflicts with an existing Xray identity or listener")
            reserved_tags.update((tag, reverse))
            used_ids.add(uid)
            used_emails.add(email)
            used_ports.add(port)
            clients.append({"id": uid, "email": email, "reverse": {"tag": reverse}})
            inbounds.append({"tag": tag, "port": port, "listen": "127.0.0.1", "protocol": "tunnel",
                             "settings": {"allowedNetwork": "tcp", "rewriteAddress": host,
                                          "rewritePort": target}})
            managed_rules.append({"type": "field", "inboundTag": [tag], "outboundTag": reverse})
    routing[0:0] = managed_rules
    data = encode(config)
    m.require(len(data) <= MAX_CONFIG, "Xray configuration exceeds limit")
    return data


def assignment_id(pair):
    return hashlib.sha256(("ro-assignment:" + pair["clients"][0]).encode("ascii")).hexdigest()


def assignment_tag(pair, index):
    return "ro-reverse-" + pair["clients"][index].replace("-", "")


class XrayState:
    def __init__(self, stack):
        self.stack = stack
        self.path = stack.path.parent / (".xray-" + stack.name + ".json")
        self.config = Path(stack.config["RO_XRAY_CONFIG_DIR"]) / "owrt-remote.json"
        self.staging = self.config.parent / ".ro-xray-adapter"

    def exists(self):
        return self.path.exists() or self.path.is_symlink()

    def load(self):
        live = read_private(self.config, MAX_CONFIG)
        if not self.exists():
            return {"version": 1, "config": str(self.config), "base": live.decode(),
                    "routers": {}, "pending": None}
        record = json.loads(read_private(self.path, 6 * MAX_CONFIG))
        m.require(record["version"] == 1 and record["config"] == str(self.config),
                  "Xray journal belongs to a different configuration")
        return record

    def render(self, record, routers, *, profiles=None):
        base = render(record["base"].encode(), routers, self.stack.plan)
        profiles = record.get("profiles", []) if profiles is None else profiles
        if not profiles:
            return base
        config = json.loads(base)
        inbounds = config["inbounds"]
        tags = {item.get("tag") for item in inbounds + config.get("outbounds", [])}
        identities = [client for item in inbounds for client in item.get("settings", {}).get("clients", [])]
        tags.update(item.get("reverse", {}).get("tag") for item in identities)
        ids = {item.get("id") for item in identities}
        routing = config.setdefault("routing", {}).setdefault("rules", [])
        for rule in routing:
            tags.update(rule.get("inboundTag") or ())
            tags.add(rule.get("outboundTag"))
        deny = "ro-profile-deny"
        m.require(deny not in tags, "Profile deny route conflicts with the baseline")
        config.setdefault("outbounds", []).append({"tag": deny, "protocol": "blackhole"})
        for item in profiles:
            profile = item["profile"]
            tag = profile_tag(item)
            occupied = {entry["port"] for entry in inbounds}
            occupied.update(port for port in self.stack.plan["routes"].values())
            occupied.update(self.stack.plan.get(name) for name in ("hub", "wss", "tls", "health", "http", "public"))
            plan = plan_ingress(profile, public_port=item["public_port"], occupied_ports=occupied)
            m.require(tag not in tags, "Profile listener tag conflicts")
            tags.add(tag)
            inbound = {
                "tag": tag, "listen": plan["listen"], "port": plan["backend_port"],
                "protocol": "vless", "settings": {"clients": [], "decryption": "none"},
                "streamSettings": plan["streamSettings"],
            }
            inbounds.append(inbound)
            occupied.add(profile["port"])
            for router_id, pair in sorted(item.get("assignments", {}).items()):
                targets = routers[router_id]["targets"]
                spec = router_spec(dict(pair, id=router_id, targets=targets))
                for index, (uid, port, target) in enumerate(zip(spec["clients"], spec["ports"], targets)):
                    reverse = assignment_tag(spec, index)
                    portal = reverse + "-portal"
                    m.require(uid not in ids and port not in occupied and
                              reverse not in tags and portal not in tags, "Profile router identity conflicts")
                    ids.add(uid)
                    tags.update((reverse, portal))
                    occupied.add(port)
                    inbound["settings"]["clients"].append({
                        "id": uid, "email": reverse + "@owrt-remote", "reverse": {"tag": reverse},
                    })
                    inbounds.append({"tag": portal, "port": port, "listen": "127.0.0.1", "protocol": "tunnel",
                                     "settings": {"allowedNetwork": "tcp", "rewriteAddress": target[0],
                                                  "rewritePort": target[1]}})
                    routing.insert(0, {"type": "field", "inboundTag": [portal], "outboundTag": reverse})
            routing.insert(0, {"type": "field", "inboundTag": [tag], "outboundTag": deny})
        data = encode(config)
        m.require(len(data) <= MAX_CONFIG, "Xray configuration exceeds limit")
        return data

    def baseline(self, *, recover=False):
        if not self.exists():
            return None
        m.require(self.stack.config["RO_XRAY_BACKEND"] == BACKEND, "Xray journal requires its backend")
        record = self.load()
        pending = record["pending"]
        profile_pending = record.get("profile_pending")
        m.require(recover or not pending, "Pending Xray transaction: reconcile from HUB")
        m.require(recover or not profile_pending, "Pending profile transaction: recover through the adapter")
        valid = [self.render(record, record["routers"])]
        if pending:
            valid.append(self.render(record, pending["routers"]))
        live = read_private(self.config, MAX_CONFIG)
        if profile_pending:
            valid.append(self.render(record, record["routers"], profiles=profile_pending["profiles"]))
        m.require(live in valid or (profile_pending and
                  hashlib.sha256(live).hexdigest() == profile_pending.get("trial_sha256")),
                  "Active Xray config differs from journal")
        return record["base"].encode()

    def managed_ports(self):
        if not self.exists():
            return ()
        record = self.load()
        return tuple(port for router in record["routers"].values() for port in router["ports"]) + profile_ports(
            record.get("profiles", []))

    def save(self, record):
        data = encode(record)
        m.require(len(data) <= 6 * MAX_CONFIG, "Xray journal exceeds limit")
        m.durable_write(self.path, data.decode())

    def check_staging(self):
        from stack_runtime import protected

        directory = protected(self.staging)
        m.require(directory.is_dir() and stat.S_IMODE(directory.stat().st_mode) == 0o700,
                  "Unsafe Xray staging directory")
        for item in directory.iterdir():
            m.require(item.name == "candidate" and protected(item).is_file() and
                      item.stat().st_nlink == 1 and not item.stat().st_mode & 0o007,
                      "Unexpected Xray staging entry")
        return directory

    def replace(self, data):
        info = self.config.stat()
        self.staging.mkdir(mode=0o700, exist_ok=True)
        directory = self.check_staging()
        candidate = directory / "candidate"
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            os.fchown(stream.fileno(), 0, info.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(candidate, self.config)
        m.sync_directory(self.config.parent)
        m.sync_directory(directory)
        directory.rmdir()
        m.sync_directory(self.config.parent)


def profile_tag(item):
    return f"ro-profile-{item['profile']['name']}-{item['revision']}"


def profile_ports(profiles):
    return tuple(port for item in profiles for port in (
        item["profile"]["port"], *(port for pair in item.get("assignments", {}).values()
                                   for port in pair["ports"])))


def verify_profile_snapshot(journal, catalog, hub):
    """Cross-check the two databases and the finalized runtime in a bundle."""
    if not journal.exists():
        return
    with journal.open("rb") as stream:
        raw = stream.read(6 * MAX_CONFIG + 1)
    m.require(len(raw) <= 6 * MAX_CONFIG, "Oversized transport journal")
    record = json.loads(raw)
    token = record.get("profile_committed")
    m.require(not record.get("profile_pending") and not record.get("pending"),
              "Cannot archive an incomplete transport transaction")
    if token is None:
        return
    from secure_profile_store import load_artifact

    assigned = {}
    with m.sqlite_read(catalog, immutable=True) as db:
        marker = db.execute("SELECT token FROM unified_transport_commit").fetchall()
        m.require(marker == [(token,)], "Catalog and runtime transaction differ")
        for item in record.get("profiles", []):
            ref = (item["profile"]["name"], item["revision"])
            artifact, _, published = load_artifact(db, *ref)
            expected = {name: value for name, value in item.items() if name != "assignments"}
            m.require(published and dict(artifact, revision=ref[1]) == expected,
                      "Active profile differs from catalog artifact")
            for router, pair in item.get("assignments", {}).items():
                m.require(router in record["routers"], "Assignment references an unknown router")
                assigned.setdefault(router, {})[ref] = pair
        desired = {router: {(entry["profile_id"], entry["revision"]) for entry in json.loads(data)}
                   for router, data in db.execute("SELECT router_id, desired_profiles FROM secure_router_preferences")}
        m.require(desired == {router: set(items) for router, items in assigned.items()},
                  "Router preferences differ from Xray assignments")
    with m.sqlite_read(hub, immutable=True) as db:
        for router, reference, luci, ssh in db.execute("""SELECT id, transport_active_ref,
                transport_luci_port, transport_ssh_port FROM routers WHERE transport_active_ref != ''"""):
            ref = json.loads(reference)
            pair = assigned.get(router, {}).get((ref["profile_id"], ref["revision"]))
            m.require(pair and pair["ports"] == [luci, ssh]
                      and ref.get("assignment_id", assignment_id(pair)) == assignment_id(pair), "Active router ports differ from assigned Xray listeners")
