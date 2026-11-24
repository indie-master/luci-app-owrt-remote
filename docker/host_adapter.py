#!/usr/bin/env python3
"""Host-only Router Orchestrator capability adapter.

No docker.sock, no arbitrary shell command, no dynamic paths or systemd units.
The write capabilities are OFF unless explicitly enabled by the operator.
Requests are accepted only from a dedicated Docker HUB peer UID over a
mode-0660 Unix socket. Mount /run/router-orchestrator read-only in HUB.
"""
from __future__ import annotations
import argparse
import datetime
import uuid
import re
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import socket
import stat
import struct
import subprocess
import tempfile
import time

SOCKET = Path("/run/router-orchestrator/control.sock")
CONFIG = Path("/etc/xray/owrt-remote.json")
BINARY = "/usr/local/bin/xray"
SERVICE = "owrt-remote-xray"
HOLD = Path("/opt/router-orchestrator/data/.xray-maintenance-hold")
MAX_REQUEST = 2 * 1024 * 1024
MAX_RESPONSE = 2 * 1024 * 1024

class Rejected(RuntimeError):
    pass

def require(value, reason):
    if not value:
        raise Rejected(reason)

def sha(content):
    return hashlib.sha256(content).hexdigest()

def systemctl(*args, timeout=35):
    return subprocess.run(["systemctl", *args, SERVICE], capture_output=True,
                          timeout=timeout, text=True, check=False)

def config_inbounds(value):
    inbounds = value.get("inbounds") if isinstance(value, dict) else None
    require(isinstance(inbounds, list), "missing Xray inbound list")
    by_tag = {}
    for item in inbounds:
        require(isinstance(item, dict), "invalid inbound object")
        tag = item.get("tag")
        require(isinstance(tag, str) and tag not in by_tag, "duplicate/invalid inbound tag")
        by_tag[tag] = item
    return by_tag

def _client_map(inbound):
    """Index clients without exposing credentials in validation errors."""
    settings = inbound.get("settings", {})
    require(isinstance(settings, dict), "invalid Xray inbound settings")
    clients = settings.get("clients", [])
    require(isinstance(clients, list), "invalid Xray client collection")
    indexed = {}
    emails = set()
    for entry in clients:
        require(isinstance(entry, dict), "invalid Xray client")
        identity = entry.get("id")
        email = entry.get("email")
        require(isinstance(identity, str) and identity and identity not in indexed,
                "missing/duplicate Xray client identity")
        require(isinstance(email, str) and email and email not in emails,
                "missing/duplicate Xray client label")
        indexed[identity] = entry
        emails.add(email)
    return indexed


def validate_transition(previous, candidate):
    """Fail closed: the only approved mutation enrolls ONE new WSS router.

    Existing LuCI/SSH listeners, identities and routing remain unchanged.
    An unrelated Xray change needs an independently reviewed transaction.
    """
    require(isinstance(previous, dict) and isinstance(candidate, dict),
            "invalid Xray config")
    require(previous.keys() == candidate.keys(),
            "unexpected top-level Xray config change")
    for key in previous:
        if key not in {"inbounds", "routing"}:
            require(previous[key] == candidate[key],
                    "unrelated Xray setting changed")
    old = config_inbounds(previous)
    new = config_inbounds(candidate)
    require(set(old).issubset(new), "existing Xray inbound removed")
    for tag, earlier in old.items():
        latest = new[tag]
        if tag in ("owrt-remote-vless", "owrt-remote-wss"):
            require({k: v for k, v in earlier.items() if k != "settings"} ==
                    {k: v for k, v in latest.items() if k != "settings"},
                    "existing transport listener/TLS/WS policy changed")
            original_settings = dict(earlier.get("settings", {}))
            updated_settings = dict(latest.get("settings", {}))
            original_settings.pop("clients", None)
            updated_settings.pop("clients", None)
            require(original_settings == updated_settings,
                    "existing transport settings changed")
        else:
            require(latest == earlier,
                    "existing LuCI/SSH reverse portal changed")
    for tag in ("owrt-remote-vless", "owrt-remote-wss"):
        require(tag in old and tag in new, "missing required Xray transport")

    legacy = _client_map(old["owrt-remote-vless"])
    next_legacy = _client_map(new["owrt-remote-vless"])
    require(legacy == next_legacy,
            "TCP identities changed; new routers must use WSS")
    wss = _client_map(old["owrt-remote-wss"])
    next_wss = _client_map(new["owrt-remote-wss"])
    require(all(next_wss.get(uid) == row for uid, row in wss.items()),
            "existing WSS router identity changed or removed")
    added = [row for uid, row in next_wss.items() if uid not in wss]

    new_tags = set(new) - set(old)
    routing_before = previous.get("routing", {})
    routing_after = candidate.get("routing", {})
    require(isinstance(routing_before, dict) and isinstance(routing_after, dict),
            "invalid routing settings")
    require({k: v for k, v in routing_before.items() if k != "rules"} ==
            {k: v for k, v in routing_after.items() if k != "rules"},
            "unrelated routing settings changed")
    before_rules = routing_before.get("rules", [])
    after_rules = routing_after.get("rules", [])
    require(isinstance(before_rules, list) and isinstance(after_rules, list),
            "invalid routing rules")

    # Preserve the relative order of existing routing rules even when the
    # generated config inserts new router rules in the middle of the list.
    pending = iter(after_rules)
    for rule in before_rules:
        for found in pending:
            if rule == found:
                break
        else:
            raise Rejected("existing routing rule removed or reordered")
    extra_rules = list(after_rules)
    for rule in before_rules:
        extra_rules.remove(rule)

    if not (added or new_tags or extra_rules):
        return  # Read-only/no-op candidate.

    require(len(added) == 2, "new WSS router requires exactly two clients")
    labels = {r["email"]: r for r in added}
    owners = [email[:-len("@owrt-remote")] for email in labels
              if email.endswith("@owrt-remote")
              and not email.endswith("-ssh@owrt-remote")]
    require(len(owners) == 1 and owners[0] and
            all(ch.isascii() and (ch.isalnum() or ch in "_.-")
                for ch in owners[0]),
            "invalid new router identity")
    router_id = owners[0]
    normal = router_id + "@owrt-remote"
    ssh = router_id + "-ssh@owrt-remote"
    require(set(labels) == {normal, ssh},
            "new WSS client pair is incomplete")
    require(labels[normal].get("reverse", {}).get("tag") ==
            "reverse-" + router_id and
            labels[ssh].get("reverse", {}).get("tag") ==
            "reverse-" + router_id + "-ssh",
            "new WSS reverse tags do not match")
    expected_tags = {"entry-" + router_id, "ssh-" + router_id}
    require(new_tags == expected_tags, "new reverse portal set changed")
    ports = [int(item.get("port", 0)) for item in new.values()]
    require(len(ports) == len(set(ports)),
            "new reverse portal port conflicts with existing listener")
    for tag in expected_tags:
        item = new[tag]
        require(item.get("protocol") == "tunnel" and
                item.get("listen") == "127.0.0.1" and
                isinstance(item.get("port"), int) and 1024 <= item["port"] <= 65535,
                "new reverse portal is not a loopback TCP tunnel")
        settings = item.get("settings", {})
        require(isinstance(settings, dict) and
                settings.get("allowedNetwork") == "tcp" and
                isinstance(settings.get("rewriteAddress"), str) and
                isinstance(settings.get("rewritePort"), int),
                "new reverse portal target is invalid")
    expected_rules = [
        {"type": "field", "inboundTag": ["entry-" + router_id],
         "outboundTag": "reverse-" + router_id},
        {"type": "field", "inboundTag": ["ssh-" + router_id],
         "outboundTag": "reverse-" + router_id + "-ssh"},
    ]
    require(len(extra_rules) == 2 and
            all(rule in extra_rules for rule in expected_rules),
            "new router rules differ from expected LuCI/SSH routes")
    require(len(json.dumps(candidate).encode()) <= MAX_REQUEST - 4096,
            "candidate Xray config exceeds limit")


def atomic_replace(path, data):
    fd, temp = tempfile.mkstemp(prefix=".ro-xray-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        dirfd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)

# The adapter is the sole root-owned Docker lifecycle capability.
# The HUB is never given docker.sock or container-name selection.
XRAY_CONTAINER = "router-orchestrator-xray"
XRAY_IMAGE_PREFIX = "router-orchestrator-xray:"
PORTS = (18443, 18444)


def docker(*args, timeout=45):
    return subprocess.run(["/usr/bin/docker", *args], capture_output=True,
                          text=True, timeout=timeout, check=False)


def _container_state():
    result = docker("inspect", "--format",
                    "{{.State.Running}}|{{.Config.Image}}|{{.HostConfig.NetworkMode}}",
                    XRAY_CONTAINER, timeout=8)
    if result.returncode:
        return None
    parts = result.stdout.strip().split("|")
    if len(parts) != 3 or not parts[1].startswith(XRAY_IMAGE_PREFIX) or parts[2] != "host":
        return None
    return parts[0] == "true"


def _xray_port_ready():
    for port in PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                pass
        except OSError:
            return False
    return True


def active_xray():
    # Never consider the Docker instance healthy while a legacy host
    # systemd instance is also active, even if both reverse ports respond.
    if systemctl("is-active", "--quiet", timeout=5).returncode == 0:
        return False
    return _container_state() is True and _xray_port_ready()


def wait_xray(attempts=25):
    for _ in range(attempts):
        if active_xray():
            return True
        time.sleep(1)
    return False


def restart_docker_xray(*, recovering=False):
    """Allow recovery even if the candidate is unhealthy.

    Unlike the original hotfix, rollback never requires an already healthy
    Docker instance. The fixed container identity is rechecked after restart.
    """
    require(systemctl("is-active", "--quiet", timeout=5).returncode != 0,
            "legacy systemd Xray is running; refusing Docker restart")
    if not recovering:
        require(active_xray(), "Docker Xray is unhealthy before restart")
    state = _container_state()
    require(state is not None, "unexpected Xray container identity/network")
    action = ("restart", "--time", "5", XRAY_CONTAINER) if state else ("start", XRAY_CONTAINER)
    result = docker(*action, timeout=50)
    return result


def validate_candidate_with_container(config_path):
    """Validate using the EXACT running core image, without a writable socket in HUB."""
    state = docker_inspect_image()
    require(state is not None, "Docker Xray identity unavailable")
    require(Path(config_path).parent == CONFIG.parent, "candidate must be inside /etc/xray")
    args = ["run", "--rm", "--pull", "never", "--network", "none",
            "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true",
            "--pids-limit", "64", "--memory", "256m",
            "-v", "/etc/xray:/etc/xray:ro",
            "-v", "/usr/local/share/xray:/usr/local/share/xray:ro"]
    tls = Path("/opt/router-orchestrator/tls")
    if tls.is_dir():
        args += ["-v", str(tls) + ":/etc/router-orchestrator/tls:ro"]
    args += [state, "run", "-test", "-config", str(config_path)]
    result = docker(*args, timeout=40)
    require(result.returncode == 0,
            "running Docker Xray core rejected the candidate configuration")


def docker_inspect_image():
    result = docker("inspect", "--format", "{{.Config.Image}}", XRAY_CONTAINER, timeout=8)
    if result.returncode:
        return None
    image = result.stdout.strip()
    if image.startswith(XRAY_IMAGE_PREFIX):
        return image
    return None


def clear_stale_socket(path):
    """Safely recover a crash-left socket; never remove an active listener.

    Called only after checking a root-owned, non-writable runtime directory.
    """
    path = Path(path)
    try:
        meta = path.lstat()
    except FileNotFoundError:
        return False
    require(stat.S_ISSOCK(meta.st_mode) and meta.st_uid == os.geteuid(),
            "existing control.sock is not an owned UNIX socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            # A lingering bind inode after SIGKILL does not have a listener.
            again = path.lstat()
            require(again.st_ino == meta.st_ino and
                    stat.S_ISSOCK(again.st_mode) and again.st_uid == meta.st_uid,
                    "control.sock changed while checking stale socket")
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise Rejected("control.sock could not be verified idle") from exc
        else:
            raise Rejected("Host adapter already listening on control.sock")


class Adapter:
    def __init__(self, path=SOCKET, uid=23088, allow_restart=False, allow_apply=False):
        self.path = Path(path)
        self.uid = uid
        self.allow_restart = allow_restart
        self.allow_apply = allow_apply
        self.last_restart = 0
        self.cpu_previous = None

    def can_mutate(self, enabled):
        require(enabled, "Xray mutation not approved in host adapter")
        require(not HOLD.exists(), "Xray maintenance hold is active")
        require(time.monotonic() - self.last_restart >= 120,
                "Xray restart cooldown is active")

    def current(self):
        data = CONFIG.read_bytes()
        require(len(data) <= MAX_REQUEST - 4096, "Xray config exceeds response limit")
        return {"ok": True, "config": json.loads(data), "sha256": sha(data)}

    def xray_preview(self, request):
        """Validate one WSS addition without changing host Xray or services.

        Xray's own -test runs against a 0600 temporary file under the
        systemd-confined /run/router-orchestrator directory. The temp file is
        always removed. No write-enable flag or maintenance-hold removal is
        necessary for a preview.
        """
        original = CONFIG.read_bytes()
        expected = request.get("expected_sha256")
        require(isinstance(expected, str) and expected == sha(original),
                "Xray config changed since candidate was generated")
        candidate = request.get("config")
        validate_transition(json.loads(original), candidate)
        require(json.loads(original) != candidate,
                "preview requires exactly one new WSS router")
        data = (json.dumps(candidate, ensure_ascii=False,
                           separators=(",", ":")) + "\n").encode()
        require(len(data) < MAX_REQUEST - 4096, "Xray preview exceeds request limit")
        fd, temp = tempfile.mkstemp(prefix=".ro-preview-", suffix=".json",
                                    dir=CONFIG.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
            validate_candidate_with_container(Path(temp))
        finally:
            Path(temp).unlink(missing_ok=True)
        require(sha(CONFIG.read_bytes()) == expected,
                "host Xray config changed while preview was running")
        return {"ok": True, "validated": True, "service": SERVICE,
                "xray_config_unchanged": True,
                "restart_performed": False, "apply_performed": False,
                "candidate_sha256": sha(data)}

    def secure_catalog(self, request):
        from secure_profile_store import connection, save_allocated_draft, list_revisions
        from secure_profile_catalog import available_profiles, desired_profiles, set_desired_profiles
        op = request["op"]
        with connection() as db:
            if op == "secure.catalog.list":
                return {"ok": True, "revisions": list_revisions(db),
                        "available": available_profiles(db)}
            if op == "secure.catalog.draft":
                config = json.loads(CONFIG.read_bytes())
                occupied = [item.get("port") for item in config.get("inbounds", [])
                            if isinstance(item, dict) and type(item.get("port")) is int]
                from secure_ingress_inventory import live_tcp_ports
                occupied = set(occupied) | live_tcp_ports()
                draft = save_allocated_draft(db, profile=request.get("profile"),
                                   label=request.get("label"), remote_host=request.get("remote_host"),
                                   public_port=request.get("public_port"), occupied_ports=occupied)
                return {"ok": True, "draft": draft}
            if op == "secure.catalog.approve":
                from secure_profile_store import load_artifact
                from secure_ingress_inventory import approve_draft_names
                require(request.get("confirmed") is True, "explicit SNI approval required")
                artifact, digest, published = load_artifact(db, request.get("profile_id"), request.get("revision"))
                require(not published, "revision already published")
                return {"ok": True, **approve_draft_names(artifact)}
            if op in ("secure.catalog.validate", "secure.catalog.plan"):
                from secure_profile_store import load_artifact
                from secure_certificate_inventory import inspect_pair
                from secure_ingress import plan_ingress
                artifact, digest, published = load_artifact(
                    db, request.get("profile_id"), request.get("revision"))
                profile = artifact["profile"]
                original = CONFIG.read_bytes()
                config = json.loads(original)
                occupied = [item.get("port") for item in config.get("inbounds", [])
                            if isinstance(item, dict) and type(item.get("port")) is int]
                plan = plan_ingress(profile, public_port=artifact["public_port"],
                                    occupied_ports=occupied)
                if plan["requires_certificate"]:
                    mount = Path("/etc/router-orchestrator/tls")
                    host = Path("/opt/router-orchestrator/tls")
                    cert = host / Path(profile["certificate_file"]).relative_to(mount)
                    key = host / Path(profile["key_file"]).relative_to(mount)
                    inspect_pair(profile["server_name"], cert, key, roots=[host])
                if op == "secure.catalog.plan":
                    from secure_ingress_inventory import reviewed_inventory, live_tcp_ports
                    from secure_publication import build_publication, public_summary
                    if published:
                        raise Rejected("revision already published")
                    inventory = reviewed_inventory()
                    planning_inventory = dict(inventory, occupied_ports=sorted(set(inventory["occupied_ports"]) | live_tcp_ports()))
                    publication = build_publication(config, artifact, planning_inventory,
                        profile_id=request["profile_id"], revision=request["revision"])
                    data = (json.dumps(publication["candidate"], ensure_ascii=False,
                                       separators=(",", ":")) + "\n").encode()
                    require(len(data) < MAX_REQUEST, "publication candidate exceeds size limit")
                    fd, temp = tempfile.mkstemp(prefix=".ro-publication-preview-",
                                                suffix=".json", dir=CONFIG.parent)
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            os.fchmod(stream.fileno(), 0o600)
                            stream.write(data)
                        validate_candidate_with_container(Path(temp))
                    finally:
                        Path(temp).unlink(missing_ok=True)
                    require(CONFIG.read_bytes() == original,
                            "Xray changed during publication planning")
                    require(publication["backend_port"] not in live_tcp_ports(), "backend port became occupied during planning")
                    require(reviewed_inventory() == inventory,
                            "nginx inventory changed during publication planning")
                    return {"ok": True, **public_summary(publication),
                            "artifact_sha256": digest, "xray_validated": True,
                            "nginx_disk_inventory_checked": True,
                            "live_listener_verified": False}
                result = self.transport_preview({"profiles": [profile],
                                                 "expected_sha256": sha(original)})
                return {"ok": True, "profile_id": profile["name"],
                        "revision": request["revision"], "artifact_sha256": digest,
                        "xray_validated": result["validated"],
                        "certificate_validated": plan["requires_certificate"],
                        "ingress_inventory_checked": False, "published": published,
                        "applied": False}
            router_id = request.get("router_id")
            if op == "secure.catalog.preferences":
                return {"ok": True, "desired": desired_profiles(db, router_id),
                        "available": available_profiles(db), "applied": False}
            if op == "secure.catalog.select":
                set_desired_profiles(db, router_id, request.get("profiles"))
                return {"ok": True, "desired": desired_profiles(db, router_id), "applied": False}
            raise Rejected("unsupported catalog operation")

    def transport_preview(self, request):
        """Pure preview: no config write or live Xray restart.

        Only one candidate listener is accepted. A fixed, empty VLESS inbound
        is used for syntax validation; actual router enrollment remains a
        separate guarded transaction.
        """
        from transport_profiles import validate_profiles, render_router_outbound
        profiles = request.get("profiles")
        require(isinstance(profiles, list) and len(profiles) == 1,
                "preview accepts one candidate profile")
        original = CONFIG.read_bytes()
        require(request.get("expected_sha256") == sha(original),
                "Xray configuration changed during preview")
        config = json.loads(original)
        generated = validate_profiles(profiles)
        profile = generated[0]
        port = profile["port"]
        reserved = {80, 443, 8088, 18089, 18445}
        occupied = {int(item["port"]) for item in config.get("inbounds", [])
                    if isinstance(item, dict) and isinstance(item.get("port"), int)}
        require(port not in reserved and port not in occupied,
                "transport listener port conflicts with existing service")
        tag = "ro-preview-" + profile["name"]
        require(all(item.get("tag") != tag for item in config.get("inbounds", [])),
                "candidate listener tag already exists")
        candidate = json.loads(original)
        candidate["inbounds"].append({
            "tag": tag, "listen": "127.0.0.1", "port": port,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": profile["streamSettings"],
        })
        data = (json.dumps(candidate, separators=(",", ":")) + "\n").encode()
        require(len(data) < MAX_REQUEST - 4096, "Xray candidate exceeds limit")
        fd, temp = tempfile.mkstemp(prefix=".ro-transport-preview-",
                                    suffix=".json", dir=CONFIG.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
            validate_candidate_with_container(Path(temp))
        finally:
            Path(temp).unlink(missing_ok=True)
        require(sha(CONFIG.read_bytes()) == request["expected_sha256"],
                "Xray configuration changed during preview")
        safe = json.loads(json.dumps(profile))
        reality = safe["streamSettings"].get("realitySettings")
        if reality:
            reality["privateKey"] = "[REDACTED]"
        response = {"ok": True, "validated": True, "preview": [safe],
                    "xray_config_unchanged": True,
                    "restart_performed": False, "apply_performed": False,
                    "candidate_sha256": sha(data)}
        if request.get("router_uuid"):
            response["router_outbound"] = render_router_outbound(
                profiles[0], router_uuid=request["router_uuid"],
                remote_host=request.get("remote_host"))
        return response

    def transport_canary_preview(self, request):
        """Prepare full router-specific shadow server/client candidate without writes.

        The adapter reads the canonical SQLite identities itself, preventing
        browser-origin profile fields from selecting arbitrary VLESS UUIDs.
        """
        from transport_canary import build_shadow_canary, assert_baseline_retained
        from transport_profiles import render_router_outbound, _host
        profiles = request.get("profiles")
        require(isinstance(profiles, list) and len(profiles) == 1,
                "exactly one transport candidate required")
        router_id = request.get("router_id")
        require(isinstance(router_id, str) and
                re.fullmatch(r"[A-Za-z0-9_-]{1,64}", router_id),
                "invalid router ID")
        expected = request.get("expected_sha256")
        original = CONFIG.read_bytes()
        require(isinstance(expected, str) and expected == sha(original),
                "Xray baseline changed during preview")
        database = Path("/opt/router-orchestrator/data/hub.db")
        require(database.is_file() and not database.is_symlink(),
                "canonical HUB SQLite database unavailable")
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "select id,vless_uuid,ssh_vless_uuid,admin_host,admin_port,"
                "ssh_host,ssh_port,vps_host from routers where id=? and deleted_at=0",
                (router_id,)
            ).fetchone()
        require(row is not None, "router not found")
        original_json = json.loads(original)
        canary, pilot = build_shadow_canary(
            original_json, profiles[0], dict(row),
            admin_test_port=request.get("admin_test_port"),
            ssh_test_port=request.get("ssh_test_port"),
            listen="127.0.0.1"
        )
        assert_baseline_retained(original_json, canary)
        data = (json.dumps(canary, separators=(",", ":")) + "\n").encode()
        require(len(data) < MAX_REQUEST - 4096, "candidate exceeds maximum size")
        fd, temp = tempfile.mkstemp(prefix=".ro-shadow-preview-",
                                    suffix=".json", dir=CONFIG.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
            validate_candidate_with_container(Path(temp))
        finally:
            Path(temp).unlink(missing_ok=True)
        require(sha(CONFIG.read_bytes()) == expected,
                "production Xray config changed while testing")
        remote_host = _host(request.get("remote_host") or row["vps_host"])
        admin_outbound = render_router_outbound(
            profiles[0], router_uuid=pilot["vless_uuid"], remote_host=remote_host,
            reverse_tag=pilot["reverse_tag"]
        )
        admin_outbound["tag"] = "vps-pilot-admin"
        ssh_outbound = render_router_outbound(
            profiles[0], router_uuid=pilot["ssh_vless_uuid"], remote_host=remote_host,
            reverse_tag=pilot["ssh_reverse_tag"]
        )
        ssh_outbound["tag"] = "vps-pilot-ssh"
        safe_pilot = {k:v for k,v in pilot.items()
                      if k not in ("vless_uuid","ssh_vless_uuid")}
        return {"ok": True, "validated_by_xray": True,
                "pilot": safe_pilot, "client_outbounds": [admin_outbound, ssh_outbound],
                "baseline_unchanged": True, "applied": False,
                "candidate_sha256": sha(data)}

    def transport_stage_start(self, request):
        """Explicit opt-in, short-lived server canary; never changes router UCI."""
        self.can_mutate(self.allow_apply)
        require(request.get("confirmed") is True,
                "operator confirmation required")
        from transport_profiles import require_secure_profile
        profiles = request.get("profiles")
        require(isinstance(profiles, list) and len(profiles) == 1,
                "exactly one transport candidate required")
        require_secure_profile(profiles[0])
        self.transport_canary_preview(request)
        queue = Path("/opt/router-orchestrator/data/transport-stage-queue")
        require(queue.is_dir() and not queue.is_symlink(), "stage queue not provisioned")
        metadata = queue.stat()
        require(metadata.st_uid == 0 and stat.S_IMODE(metadata.st_mode) == 0o700,
                "unsafe stage queue permissions")
        state = Path("/opt/router-orchestrator/data/transport-stage/stage.json")
        if state.exists():
            require(not state.is_symlink(), "unsafe shadow journal")
            journal = json.loads(state.read_text())
            require(journal.get("phase") == "ROLLED_BACK",
                    "another shadow transport transaction is still active")
        for candidate in queue.glob("*.json"):
            require(not candidate.is_symlink(), "unsafe stage queue entry")
            item = json.loads(candidate.read_text())
            require(item.get("status") not in ("pending", "running"),
                    "another shadow staging job is already pending/running")
        profile = request["profiles"][0]
        job_id = str(uuid.uuid4())
        record = {
            "id": job_id, "action": "stage", "status": "pending",
            "profile": profile, "router_id": request["router_id"],
            "entry_port": request["admin_test_port"],
            "ssh_port": request["ssh_test_port"],
            "created_unix": int(time.time()),
        }
        path = queue / (job_id + ".json")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        unit = "router-orchestrator-transport-stage@" + job_id + ".service"
        started = subprocess.run(["systemctl", "start", "--no-block", unit],
                                 capture_output=True, timeout=15, text=True)
        if started.returncode:
            path.unlink(missing_ok=True)
            raise Rejected("shadow stage worker did not start")
        self.last_restart = time.monotonic()
        return {"ok": True, "job_id": job_id, "status": "pending",
                "expires_after_seconds": 600, "primary_uci_changed": False}

    def transport_stage_job(self, request):
        job_id = request.get("job_id")
        require(isinstance(job_id, str) and
                re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", job_id),
                "invalid stage job identifier")
        path = Path("/opt/router-orchestrator/data/transport-stage-queue") / (job_id + ".json")
        require(path.is_file() and not path.is_symlink(), "stage job unavailable")
        meta = path.stat()
        require(meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o600,
                "stage job permissions unsafe")
        entry = json.loads(path.read_text())
        require(entry.get("id") == job_id, "stage job identifier mismatch")
        return {"ok": True, "job": {
            "id": entry["id"], "status": entry.get("status"),
            "message": entry.get("message", ""),
            "created_unix": entry.get("created_unix"),
            "finished_unix": entry.get("finished_unix"),
        }}

    def transport_stage_status(self):
        """Redacted status of the one active shadow transaction."""
        state = Path("/opt/router-orchestrator/data/transport-stage/stage.json")
        if not state.exists():
            return {"ok": True, "active": False, "phase": "NONE"}
        require(state.is_file() and not state.is_symlink(), "unsafe stage journal")
        meta = state.stat()
        require(meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o600,
                "unsafe stage journal permissions")
        entry = json.loads(state.read_text())
        phase = entry.get("phase")
        require(phase in ("STAGING", "STAGED", "VERIFIED", "ROLLED_BACK",
                          "ROLLBACK_FAILED"), "unrecognized stage state")
        expires = int(entry.get("expires_unix") or 0)
        response = {"ok": True, "active": phase in ("STAGING", "STAGED", "VERIFIED",
                                                    "ROLLBACK_FAILED"),
                    "phase": phase, "router_id": entry.get("router_id"),
                    "expires_unix": expires,
                    "seconds_remaining": max(0, expires - int(time.time()))}
        if phase in ("STAGED", "VERIFIED"):
            client = entry.get("client")
            pilot = entry.get("pilot")
            if isinstance(client, dict) and isinstance(pilot, dict):
                # No private keys, UUIDs, or server Xray config are returned.
                allowed = ("transport", "host", "port", "sni", "ws_path",
                           "xhttp_path", "grpc_service", "reality_public_key",
                           "reality_short_id", "reverse_tag", "ssh_reverse_tag")
                response["client"] = {k: client[k] for k in allowed if k in client}
                response["test_ports"] = {
                    "ssh": pilot.get("ssh_test_port"),
                    "admin": pilot.get("admin_test_port"),
                }
        return response

    def transport_stage_rollback(self, request):
        """Request an immediate fixed-unit rollback; never execute web input.

        Recovery MUST remain possible while the shadow staging maintenance
        hold exists and during the normal 120-second restart cooldown.
        """
        require(self.allow_apply, "transport rollback not approved")
        require(request.get("confirmed") is True,
                "explicit confirmation required")
        stage = self.transport_stage_status()
        require(stage["phase"] in ("STAGED", "VERIFIED"),
                "no completed shadow pilot available for rollback")
        unit = "router-orchestrator-transport-rollback.service"
        started = subprocess.run(
            ["systemctl", "start", "--no-block", unit],
            capture_output=True, text=True, timeout=15, check=False
        )
        require(started.returncode == 0,
                "rollback service could not be started")
        self.last_restart = time.monotonic()
        return {"ok": True, "status": "rollback_requested",
                "router_id": stage["router_id"], "phase": stage["phase"]}

    def core_job_start(self, request):
        action = request.get("action")
        require(action in ("prepare", "upgrade", "restart", "rollback"),
                "unsupported core action")
        if action == "prepare":
            require(self.allow_apply, "Xray preparation is not authorized")
        else:
            self.can_mutate(self.allow_apply if action == "upgrade" else self.allow_restart)
        version = request.get("version") or ""
        if action in ("prepare", "upgrade"):
            require(isinstance(version, str) and
                    re.fullmatch(r"v[0-9]{1,4}\.[0-9]{1,3}\.[0-9]{1,3}", version),
                    "invalid stable release version")
        else:
            require(not version, "version is not used for this action")
        root = Path("/opt/router-orchestrator/data/core-jobs")
        require(root.is_dir() and not root.is_symlink(), "core-job directory not provisioned")
        st = root.stat()
        require(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o700,
                "core-job directory permissions unsafe")
        # Do not queue more work while a core operation is in progress.
        for path in root.glob("*.json"):
            if path.is_symlink():
                raise Rejected("unsafe core-job record")
            try:
                item = json.loads(path.read_text())
            except Exception:
                raise Rejected("unreadable core-job record")
            if item.get("status") in ("pending", "running"):
                raise Rejected("a core operation is already pending/running")
        job_id = str(uuid.uuid4())
        path = root / (job_id + ".json")
        record = {"id": job_id, "status": "pending",
                  "action": action, "version": version,
                  "created_unix": int(time.time())}
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(record, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        unit = "router-orchestrator-core-job@" + job_id + ".service"
        started = subprocess.run(["systemctl", "start", "--no-block", unit],
                                 capture_output=True, timeout=15, text=True)
        if started.returncode:
            path.unlink(missing_ok=True)
            raise Rejected("core-job systemd unit did not start")
        if action != "prepare":
            self.last_restart = time.monotonic()
        return {"ok": True, "job_id": job_id, "status": "pending",
                "action": action}

    def core_job_status(self, request):
        job_id = request.get("job_id")
        require(isinstance(job_id, str) and
                re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", job_id),
                "invalid job ID")
        path = Path("/opt/router-orchestrator/data/core-jobs") / (job_id + ".json")
        require(path.is_file() and not path.is_symlink(), "job record absent")
        st = path.stat()
        require(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o600,
                "unsafe job record")
        record = json.loads(path.read_text())
        require(record.get("id") == job_id, "job record mismatch")
        return {"ok": True, "job": record,
                "running_image": docker_inspect_image()}

    def xray_restart(self):
        self.can_mutate(self.allow_restart)
        require(restart_docker_xray(recovering=True).returncode == 0 and wait_xray(),
                "Docker Xray restart/health failed; investigate container")
        self.last_restart = time.monotonic()
        return {"ok": True, "service": XRAY_CONTAINER, "runtime": "docker"}

    def xray_apply(self, request):
        self.can_mutate(self.allow_apply)
        original = CONFIG.read_bytes()
        require(request.get("expected_sha256") == sha(original),
                "Xray config changed concurrently")
        candidate = request.get("config")
        validate_transition(json.loads(original), candidate)
        data = (json.dumps(candidate, ensure_ascii=False, indent=2) + "\n").encode()
        if json.loads(original) == candidate:
            return {"ok": True, "unchanged": True, "service": XRAY_CONTAINER, "runtime": "docker"}
        fd, test_path = tempfile.mkstemp(prefix=".ro-candidate-", suffix=".json",
                                         dir=CONFIG.parent)
        try:
            with os.fdopen(fd, "wb") as out:
                os.fchmod(out.fileno(), 0o600)
                out.write(data)
            validate_candidate_with_container(Path(test_path))
        finally:
            Path(test_path).unlink(missing_ok=True)
        backup_dir = CONFIG.parent / "owrt-remote-backups"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        backup = backup_dir / ("router-orchestrator-" + stamp + ".json")
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(original)
            out.flush()
            os.fsync(out.fileno())
        try:
            atomic_replace(CONFIG, data)
            require(restart_docker_xray().returncode == 0 and wait_xray(),
                    "Xray candidate failed to start")
        except BaseException:
            atomic_replace(CONFIG, original)
            recovered = restart_docker_xray(recovering=True).returncode == 0 and wait_xray()
            require(recovered, "Xray rollback failed; immediate operator recovery required")
            raise Rejected("Xray candidate rejected; original config restored")
        self.last_restart = time.monotonic()
        return {"ok": True, "unchanged": False, "service": XRAY_CONTAINER, "runtime": "docker",
                "backup": str(backup)}

    def metrics(self):
        info = {}
        with open("/proc/meminfo", encoding="ascii") as stream:
            for line in stream:
                if line.startswith(("MemTotal:", "MemAvailable:")):
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        disk = shutil.disk_usage("/")
        with open("/proc/stat", encoding="ascii") as stream:
            ticks = [int(x) for x in stream.readline().split()[1:9]]
        now_ticks = (sum(ticks), ticks[3] + ticks[4])
        previous, self.cpu_previous = self.cpu_previous, now_ticks
        percent = None
        if previous:
            delta = now_ticks[0] - previous[0]
            idle = now_ticks[1] - previous[1]
            if delta > 0 and idle >= 0:
                percent = round(100 * (delta - idle) / delta, 1)
        return {"ok": True, "cpu": {"percent": percent, "cores": os.cpu_count() or 1},
                "memory": {"percent": round((total - avail) / total * 100, 1) if total else None,
                           "used_bytes": total - avail, "total_bytes": total,
                           "available_bytes": avail},
                "disk": {"percent": round(disk.used / disk.total * 100, 1),
                         "used_bytes": disk.used, "total_bytes": disk.total,
                         "free_bytes": disk.free, "path": "/"},
                "sampled_at": time.time(), "scope": "host-adapter"}

    def handle(self, request):
        require(isinstance(request, dict), "invalid request")
        op = request.get("op")
        if op == "health":
            return {"ok": True, "adapter": "host-control-v1"}
        if op == "host.metrics":
            return self.metrics()
        if op == "xray.status":
            return {"ok": True, "active": active_xray(), "service": XRAY_CONTAINER, "runtime": "docker"}
        if op == "xray.current":
            return self.current()
        if op == "xray.preview":
            return self.xray_preview(request)
        if op in ("secure.catalog.list", "secure.catalog.draft",
                  "secure.catalog.preferences", "secure.catalog.select", "secure.catalog.validate", "secure.catalog.plan", "secure.catalog.approve"):
            try:
                return self.secure_catalog(request)
            except sqlite3.Error:
                raise Rejected("profile store unavailable") from None
        if op == "xray.transport.preview":
            return self.transport_preview(request)
        if op == "xray.transport.canary.preview":
            return self.transport_canary_preview(request)
        if op == "xray.transport.stage.start":
            return self.transport_stage_start(request)
        if op == "xray.transport.stage.job":
            return self.transport_stage_job(request)
        if op == "xray.transport.stage.status":
            return self.transport_stage_status()
        if op == "xray.transport.stage.rollback":
            return self.transport_stage_rollback(request)
        if op == "xray.core.info":
            return {"ok": True, "active": active_xray(),
                    "image": docker_inspect_image(),
                    "runtime": "docker"}
        if op == "xray.core.start":
            return self.core_job_start(request)
        if op == "xray.core.job":
            return self.core_job_status(request)
        if op == "xray.restart":
            return self.xray_restart()
        if op == "xray.apply":
            return self.xray_apply(request)
        raise Rejected("unsupported operation")

    def serve_peer(self, conn):
        """One peer-credential authorized request, tested on real UNIX sockets.

        Unauthorized UIDs receive no payload or sensitive error message.
        The peer check is performed before reading even one JSON byte.
        """
        _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED,
            struct.calcsize("3i")))
        if uid != self.uid:
            return False
        conn.settimeout(8)
        try:
            with conn.makefile("rb") as reader:
                raw = reader.readline(MAX_REQUEST + 1)
            require(raw.endswith(b"\n") and len(raw) <= MAX_REQUEST,
                    "request too large or incomplete")
            response = self.handle(json.loads(raw))
        except (Rejected, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            response = {"ok": False, "error": str(exc)[:180]}
        encoded = json.dumps(response, ensure_ascii=False).encode() + b"\n"
        if len(encoded) > MAX_RESPONSE:
            encoded = b'{"ok":false,"error":"response too large"}\n'
        try:
            conn.sendall(encoded)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            return False
        return True

    def run(self):
        require(self.path == SOCKET, "Host adapter is restricted to its fixed UNIX socket")
        parent = self.path.parent
        require(parent.exists() and not parent.is_symlink(),
                "systemd runtime directory missing/unsafe")
        meta = parent.lstat()
        require(stat.S_ISDIR(meta.st_mode) and meta.st_uid == 0
                and not (stat.S_IMODE(meta.st_mode) & 0o022),
                "runtime directory must be root-owned and not writable by group/others")
        # The group can traverse the directory and access only this socket;
        # it cannot unlink or replace a root-owned socket entry.
        os.chown(parent, 0, self.uid)
        os.chmod(parent, 0o750)
        clear_stale_socket(self.path)
        bound_inode = None
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self.path))
            bound_inode = self.path.lstat().st_ino
            os.chown(self.path, 0, self.uid)
            os.chmod(self.path, 0o660)
            server.listen(8)
            try:
                while True:
                    conn, _ = server.accept()
                    with conn:
                        self.serve_peer(conn)
            finally:
                # Never unlink a socket replaced behind our back.
                try:
                    info = self.path.lstat()
                    if stat.S_ISSOCK(info.st_mode) and info.st_ino == bound_inode:
                        self.path.unlink()
                except FileNotFoundError:
                    pass


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--socket", default=str(SOCKET))
    p.add_argument("--peer-uid", type=int, default=23088)
    p.add_argument("--allow-xray-restart", action="store_true")
    p.add_argument("--allow-xray-apply", action="store_true")
    args = p.parse_args()
    require(Path(args.socket) == SOCKET, "custom socket paths are forbidden")
    require(0 < args.peer_uid < 65535, "invalid HUB peer UID")
    require(os.geteuid() == 0, "Host adapter requires root service")
    Adapter(args.socket, args.peer_uid, args.allow_xray_restart, args.allow_xray_apply).run()
