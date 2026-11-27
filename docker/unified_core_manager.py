"""Xray-only release switches for a root-configured unified Stack."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import fcntl
import http.client
import json
import os
import re
import socket
import stat
import time

import manager as m
from stack_runtime import REVISION, Stack, open_settings
from unified_xray_state import MAX_CONFIG, XrayState, read_private
import xray_lifecycle as lifecycle
import xray_releases as releases

MAX_JOURNAL = 1024 * 1024
PINNED_CORE = re.compile(re.escape(releases.REGISTRY) + r"@sha256:[a-f0-9]{64}")
HOST_STACK_ENV = "RO_MANAGED_STACK_ENV"


def configured_stack_env():
    """Use the root service's explicit stack selector, never HUB data."""
    from pathlib import Path

    value = os.environ.get(HOST_STACK_ENV)
    if value is None:
        return None
    m.require(bool(value) and Path(value).is_absolute(), "Invalid host stack env path")
    return Path(value)


@contextmanager
def legacy_exclusion():
    """Join the historical Xray lock after the shared deployment lock."""
    m.require(m.inherited_lock(), "Acquire the deployment lock first")
    path = lifecycle.LOCK
    if not path.parent.exists():
        yield
        return
    m.require(not path.parent.is_symlink(), "Unsafe legacy lifecycle lock directory")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        m.require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1 and
                  not info.st_mode & 0o022, "Unsafe legacy lifecycle lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise m.Blocked("Another legacy lifecycle operation is running") from exc
        token = m.LIFECYCLE_FDS.set((descriptor,))
        try:
            yield
        finally:
            m.LIFECYCLE_FDS.reset(token)
    finally:
        os.close(descriptor)


def router_rows(database):
    with m.sqlite_read(database) as db:
        return db.execute(
            "select id, last_seen, entry_port, ssh_entry_port, status_json from routers "
            "where coalesce(deleted_at, 0) = 0"
        ).fetchall()


def channel_available(channel, port):
    try:
        if channel == "ssh":
            with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
                with sock.makefile("rb") as banner:
                    return banner.read(4) == b"SSH-"
        with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=1)) as conn:
            conn.request("GET", "/cgi-bin/luci/", headers={"Connection": "close"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                return "/cgi-bin/luci" in response.getheader("Location", "")
            return response.status in (200, 401, 403) and b"luci" in response.read(16384).lower()
    except (OSError, http.client.HTTPException):
        return False


def probe_channels(channels):
    with ThreadPoolExecutor(max_workers=16) as pool:
        return dict(zip(channels, pool.map(lambda item: channel_available(*item), channels)))


def preparation_directory(stack):
    return stack.path.parent / (".core-candidates-" + stack.name)


def heartbeat_window(raw):
    try:
        seconds = int(json.loads(raw or "{}").get("heartbeat_interval") or 30)
    except (AttributeError, ValueError, TypeError, OverflowError):
        seconds = 30
    return max(180, min(max(seconds, 10), 3600) * 3 + 30)


def baseline(stack, transport):
    database = stack.state / "hub.db"
    m.require(database.is_file() and not database.is_symlink(), "HUB database unavailable")
    document = json.loads(read_private(XrayState(stack).config, MAX_CONFIG))
    listeners = {(item.get("tag"), item.get("port")) for item in document["inbounds"]
                 if item.get("listen") == "127.0.0.1" and item.get("protocol") == "tunnel"}
    now = int(time.time())
    routers = {}
    tunnel_ports = {port for _, port in listeners}
    for router, seen, luci, ssh, status in router_rows(database):
        channels = []
        for channel, tag, port in (("luci", "entry-", luci), ("ssh", "ssh-", ssh)):
            if (tag + router, port) in listeners and type(port) is int and 0 < port <= 65535:
                channels.append((channel, port))
        for profile in transport.get("profiles", []):
            assignment = profile.get("assignments", {}).get(router)
            if assignment:
                channels.extend((channel, port)
                                for channel, port in zip(("luci", "ssh"), assignment["ports"])
                                if port in tunnel_ports)
        routers[router] = (int(seen or 0) >= now - heartbeat_window(status), channels)
    probes = probe_channels(sorted({tuple(channel) for _, channels in routers.values() for channel in channels}))
    expected = {router: {"channels": [channel for channel in channels if probes[channel]]}
                for router, (online, channels) in routers.items()
                if online or any(probes[channel] for channel in channels)}
    m.require(any({channel for channel, _ in item["channels"]} == {"luci", "ssh"}
                  for item in expected.values()), "No router with verified LuCI and SSH before switch")
    return expected


def wait_routers(stack, expected, started, *, timeout=120):
    database = stack.state / "hub.db"
    deadline = time.monotonic() + timeout
    channels = sorted({tuple(channel) for item in expected.values() for channel in item["channels"]})
    while time.monotonic() < deadline:
        heartbeats = {router: int(seen or 0) for router, seen, *_ in router_rows(database)}
        if all(heartbeats.get(router, 0) > started for router in expected):
            if all(probe_channels(channels).values()) and time.monotonic() < deadline:
                return True
        time.sleep(1)
    return False


def validate_transaction(tx):
    from core_job_runner import checked_job_id

    m.require(tx.get("kind") == "core" and tx.get("action") in {"restart", "upgrade", "rollback"},
              "Expected pending core transaction")
    checked_job_id(tx.get("id"))
    m.require(type(tx.get("started")) is int and tx["started"] > 0 and
              re.fullmatch(r"[a-f0-9]{64}", tx["config_sha256"]),
              "Invalid core transaction metadata")
    routers = tx["baseline"]
    m.require(isinstance(routers, dict), "Invalid core proof baseline")
    paired = False
    for router in routers.values():
        channels = router["channels"]
        m.require(isinstance(channels, list) and
                  all(isinstance(channel, (list, tuple)) and len(channel) == 2 and
                      channel[0] in {"luci", "ssh"} and type(channel[1]) is int and
                      0 < channel[1] <= 65535 for channel in channels),
                  "Invalid core proof channels")
        paired |= {kind for kind, _ in channels} == {"luci", "ssh"}
    m.require(paired, "Core recovery requires a verified LuCI and SSH baseline")


class CoreManager:
    def __init__(self, adapter):
        self.adapter = adapter

    def guard(self, stack):
        stack.guard()
        m.require(not m.pending_transaction(), "Pending HUB-only deployment")
        state = XrayState(stack)
        transport = state.load()
        m.require(not (transport["pending"] or transport.get("profile_pending")),
                  "Pending transport transaction")
        stack.state_permissions()
        return transport

    def candidate_test(self, stack, release, image):
        data = read_private(XrayState(stack).config, MAX_CONFIG)
        self.adapter.test_config(stack, release, data, image=image)

    def candidate(self, stack, version):
        m.require(isinstance(version, str) and releases.VERSION.fullmatch(version), "Invalid Xray version")
        tag = version[1:]
        manifest = json.loads(read_private(preparation_directory(stack) / (tag + ".json"), 8192))
        pinned = manifest.get("official_digest_ref", "")
        m.require(manifest.get("version") == version and manifest.get("validation") == "config-only" and
                  manifest.get("image") == "router-orchestrator-xray:" + tag and
                  manifest.get("official_image") == releases.REGISTRY + ":" + tag and
                  isinstance(pinned, str) and re.fullmatch(re.escape(releases.REGISTRY) + r"@sha256:[a-f0-9]{64}", pinned),
                  "Candidate lacks a verified official manifest")
        return pinned, manifest["image_id"]

    def switch(self, stack, source, target, *, expected_container):
        stack.guard()
        stack.verify_release(target)
        items = stack.inspect(target) if expected_container is None else stack.inspect(source, target)
        item = items.get("xray")
        if item:
            label = item["Config"]["Labels"][REVISION]
            owner_release = source if label == source["revision"] else target
            container = self.adapter.owned_xray(stack, owner_release, item)
        else:
            container = None
        m.require(container == expected_container, "Xray container changed")
        if container is None:
            m.require(set(items) == set(stack.services) - {"xray"},
                      "Cannot confirm ownership of the remaining Stack")
            stack.guard()
            stack.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                          "--no-recreate", "xray", release=target)
            return
        stack.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                      "--force-recreate", "xray", release=target)

    def ready(self, stack, release, routers, *, original=None):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.adapter.probe(stack, release, routers):
                item = stack.inspect(release)["xray"]
                self.adapter.owned_xray(stack, release, item)
                if original is None or item["Id"] != original:
                    return
            time.sleep(0.25)
        raise m.Blocked("Managed Xray did not recover")

    def recover(self, stack, record):
        """Reconcile an interrupted operation without replaying a restart or upgrade."""
        m.require(m.inherited_lock() and (m.LIFECYCLE_FDS.get() or not lifecycle.LOCK.parent.exists()),
                  "Core recovery requires deployment and legacy exclusion")
        tx = record["pending"]
        validate_transaction(tx)
        routers = self.guard(stack)["routers"]
        source, target = record["current"], tx["target"]
        m.require(stack.config in (source["config"], target["config"]), "Stack env changed during core switch")
        m.require(PINNED_CORE.fullmatch(source["config"]["RO_XRAY_IMAGE"]) and
                  PINNED_CORE.fullmatch(target["config"]["RO_XRAY_IMAGE"]) and
                  dict(source["config"], RO_XRAY_IMAGE=target["config"]["RO_XRAY_IMAGE"]) == target["config"] and
                  (tx["action"] != "restart" or source == target),
                  "Invalid core release identity")
        original = Stack(stack.path, source["config"], command_timeout=30)
        original.verify_release(source)
        m.require(m.file_hash(XrayState(original).config) == tx["config_sha256"],
                  "Xray config changed during core switch")
        items = original.inspect(source, target)
        item = items.get("xray")
        if item is None:
            m.require(tx["action"] != "restart", "Managed Xray disappeared during restart")
            self.switch(original, target, source, expected_container=None)
        else:
            label = item["Config"]["Labels"][REVISION]
            observed = source if label == source["revision"] else target
            if observed == target and target != source:
                Stack(stack.path, target["config"], command_timeout=30).verify_release(target)
            container = self.adapter.owned_xray(original, observed, item)
            if tx["action"] != "restart" and (observed != source or item["State"]["Status"] != "running"):
                self.switch(original, target, source, expected_container=container)
        proof_since = int(time.time())
        self.ready(original, source, routers)
        m.require(wait_routers(original, tx["baseline"], proof_since),
                  "Previous Xray is running but router channels remain unverified")
        self.guard(original)
        original.verify_release(source)
        m.require(m.file_hash(XrayState(original).config) == tx["config_sha256"],
                  "Xray config changed during core recovery")
        if stack.config != source["config"]:
            original.publish(source["config"])
        original.save(dict(record, pending=None, core_receipt={"id": tx["id"], "status": "failed"}))

    def execute(self, stack, record, job):
        action = job["action"]
        transport = self.guard(stack)
        routers = transport["routers"]
        source = record["current"]
        m.require(PINNED_CORE.fullmatch(source["config"]["RO_XRAY_IMAGE"]),
                  "Core maintenance requires an official pinned source image")
        stack.verify_release(source)
        items = stack.inspect(source)
        item = items["xray"]
        container = self.adapter.owned_xray(stack, source, item)
        m.require(item["State"]["Status"] == "running" and
                  self.adapter.probe(stack, source, routers), "Current Xray is unavailable")
        if action == "prepare":
            from types import SimpleNamespace
            validator = SimpleNamespace(atomic=lifecycle.atomic,
                                        candidate_test=lambda image: self.candidate_test(stack, source, image))
            from stack_runtime import protected
            directory = preparation_directory(stack)
            directory.mkdir(mode=0o700, exist_ok=True)
            m.require(protected(directory).is_dir() and not directory.stat().st_mode & 0o077,
                      "Unsafe core candidate directory")
            releases.prepare_candidate(job["version"], validator, staged=directory)
            return

        history = record.get("core_known_good", [])
        target, candidate = source, stack
        if action != "restart":
            if action == "upgrade":
                pinned, expected = self.candidate(stack, job["version"])
            else:
                previous = next((entry for entry in history if entry["image_id"] != source["images"]["xray"]), None)
                m.require(previous is not None, "No previous verified core")
                pinned, expected = previous["image"], previous["image_id"]
            candidate = Stack(stack.path, dict(stack.config, RO_XRAY_IMAGE=pinned))
            revision = source.get("nginx_revision", source["revision"]) if "nginx" in stack.services else None
            target = candidate.release(nginx_revision=revision)
            m.require(target["material"] == source["material"] and target["images"]["xray"] == expected and
                      expected != source["images"]["xray"] and
                      all(target["images"][service] == source["images"][service]
                          for service in stack.services if service != "xray"),
                      "Candidate image identity or stack material mismatch")
            self.candidate_test(stack, source, expected)

        config = XrayState(stack).config
        config_sha256 = m.file_hash(config)
        expected_routers = baseline(stack, transport)
        stack.verify_release(source)
        m.require(m.file_hash(config) == config_sha256, "Xray config changed before core switch")
        tx = {"kind": "core", "id": job["id"], "action": action, "target": target,
              "baseline": expected_routers, "started": int(time.time()), "config_sha256": config_sha256}
        pending_record = dict(record, pending=tx)
        m.require(len(json.dumps(pending_record).encode()) < MAX_JOURNAL - 1,
                  "Core transaction journal exceeds its size limit")
        stack.save(pending_record)
        try:
            stack.guard()
            if action == "restart":
                self.adapter.restart(stack, source, container, routers)
            else:
                self.switch(candidate, source, target, expected_container=container)
            proof_since = int(time.time())
            self.ready(candidate, target, routers, original=container if action != "restart" else None)
            m.require(wait_routers(candidate, expected_routers, proof_since),
                      "Heartbeat, LuCI or SSH did not recover for all baseline routers")
            stack.guard()
            candidate.verify_release(target)
            m.require(m.file_hash(config) == config_sha256, "Xray config changed during switch")
            if action != "restart":
                candidate.publish(target["config"])
                history = lifecycle.retain_versions(
                    history, {"image": target["config"]["RO_XRAY_IMAGE"], "image_id": target["images"]["xray"]},
                    {"image": source["config"]["RO_XRAY_IMAGE"], "image_id": source["images"]["xray"]})
            stack.save(dict(record, pending=None, current=target, core_known_good=history,
                            core_receipt={"id": job["id"], "status": "success"}))
        except Exception:
            persisted = load_record(stack)
            if persisted["pending"]:
                self.recover(Stack(stack.path, open_settings(stack.path)), persisted)
            elif persisted.get("core_receipt") == {"id": job["id"], "status": "success"}:
                return
            raise


def load_record(stack):
    record = json.loads(read_private(stack.journal, MAX_JOURNAL))
    m.require(isinstance(record, dict) and type(record.get("version")) is int and
              record["version"] == 1 and isinstance(record.get("current"), dict) and
              "pending" in record and (record["pending"] is None or isinstance(record["pending"], dict)),
              "Stack release unavailable")
    receipt = record.get("core_receipt", {})
    m.require(isinstance(receipt, dict) and
              (not receipt or (set(receipt) == {"id", "status"} and
                               receipt["status"] in {"success", "failed"})),
              "Invalid core receipt")
    if receipt:
        from core_job_runner import checked_job_id
        checked_job_id(receipt["id"])
    return record
