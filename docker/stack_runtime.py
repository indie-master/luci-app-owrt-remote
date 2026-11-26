"""Root operator lifecycle for a dedicated Compose deployment."""
import base64
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
import time

import manager as m

sys.path.insert(0, str(m.ROOT / "tools"))
from orchestrator_stack_config import MODES, STACK_KEYS, render_external, render_internal, stack_plan

OWNER = "org.router-orchestrator.stack-owner"
REVISION = "org.router-orchestrator.stack-revision"
IMAGES = {"hub": "RO_IMAGE", "xray": "RO_XRAY_IMAGE", "nginx": "RO_NGINX_IMAGE"}
UPDATE_KEYS = set(IMAGES.values()) | {"RO_XRAY_CONFIG_DIR", "RO_XRAY_ASSET_DIR",
                                     "RO_TLS_DIR", "RO_NGINX_CONF"}
WS_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
WS_ACCEPT = base64.b64encode(hashlib.sha1(
    (WS_KEY + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
).digest()).decode()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def protected(path):
    """Reject aliases and writes by other host users, including via ancestors."""
    m.require(path.is_absolute() and path.resolve() == path, "Unsafe path alias")
    for item in (path, *path.parents):
        info = item.lstat()
        sticky_parent = item != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        m.require(info.st_uid == 0 and (not info.st_mode & 0o022 or sticky_parent),
                  "Stack inputs require root ownership and protected permissions")
    m.require(path.is_dir() or path.is_file(), "Stack input must be a regular file or directory")
    return path


def open_settings(path):
    m.require(protected(path).is_file() and not path.stat().st_mode & 0o077,
              "Require a private stack env file (0600)")
    config = m.read_env(path, m.ALLOWED | STACK_KEYS)
    m.require(all(not re.search(r"\s|['\"\x00\x60$]", value) for value in config.values()),
              "Unquoted/simple stack settings required")
    return config


def docker(*args):
    return m.call("docker", "--host", "unix:///var/run/docker.sock", *args,
                  env=m.local_docker_env(), timeout=120).stdout


def websocket(conn, host, path):
    """Exercise the WebSocket handshake without sending application credentials."""
    try:
        conn.request("GET", path, headers={"Host": host, "Connection": "Upgrade",
                     "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                     "Sec-WebSocket-Key": WS_KEY})
        with conn.getresponse() as response:
            return (response.status == 101 and
                    response.getheader("Upgrade", "").lower() == "websocket" and
                    response.getheader("Sec-WebSocket-Accept") == WS_ACCEPT)
    finally:
        conn.close()


def check_xray(document, plan):
    m.require(isinstance(document, dict), "Require an Xray configuration object")
    inbounds = document.get("inbounds")
    m.require(isinstance(inbounds, list) and all(isinstance(item, dict) for item in inbounds),
              "Require explicit Xray inbounds")
    declared = {plan["wss"], *plan["routes"].values()}
    actual = [item.get("port") for item in inbounds]
    m.require(all(type(port) is int for port in actual) and
              len(actual) == len(declared) and set(actual) == declared and
              all(item.get("listen") == "127.0.0.1" for item in inbounds),
              "Xray inbounds must match the declared loopback ports")

    wss = next(item for item in inbounds if item["port"] == plan["wss"])
    stream = wss.get("streamSettings", {})
    m.require(isinstance(stream, dict) and isinstance(stream.get("wsSettings"), dict),
              "Require explicit Xray WebSocket settings")
    m.require(wss.get("protocol") == "vless" and stream.get("network") == "ws" and
              stream.get("security", "none") == "none" and
              stream.get("wsSettings", {}).get("path") == plan["wss_path"],
              "Xray must expose the declared VLESS WebSocket behind TLS termination")
    for inbound in inbounds:
        if inbound is wss:
            continue
        stream = inbound.get("streamSettings", {})
        m.require(isinstance(stream, dict) and stream.get("security") in ("tls", "reality") and
                  stream.get("network", "tcp") in {"tcp", "raw"},
                  "SNI passthrough requires a TLS or REALITY TCP inbound")


def verify_certificate(directory, host):
    cert, key = directory / "fullchain.pem", directory / "privkey.pem"
    m.require(cert.is_file() and key.is_file() and not key.stat().st_mode & 0o007,
              "TLS requires fullchain.pem and a private privkey.pem")
    m.call("openssl", "verify", "-purpose", "sslserver", "-verify_hostname", host,
           "-partial_chain", "-trusted", str(cert), str(cert))
    public = m.call("openssl", "x509", "-in", str(cert), "-pubkey", "-noout").stdout
    m.require(public == m.call("openssl", "pkey", "-in", str(key), "-passin", "pass:",
                               "-pubout").stdout, "TLS private key does not match certificate")


class Stack:
    def __init__(self, path, config):
        self.path = path
        self.config = config
        m.require(config.get("RO_STACK_MODE") in MODES and "RO_STACK_NAME" in config,
                  "Require an explicit stack mode and project name")
        self.plan = stack_plan(config)
        self.name = self.plan["name"]
        m.require(self.name != m.PROJECT and self.name != "router-orchestrator",
                  "The HUB-only Compose project is reserved")
        self.state = Path(config["RO_STATE_DIR"])
        m.require(self.state.parent == path.parent and self.state.name != "backups",
                  "HUB state must be a dedicated child of the stack env directory")
        m.require(not path.parent.stat().st_mode & 0o077, "Stack env directory must be private")
        m.require(not self.state.is_relative_to(m.LOCK.parent / "data"),
                  "The existing HUB state is reserved")
        self.services = ("hub", "xray") if self.plan["mode"] == "external-only" else ("hub", "xray", "nginx")
        self.journal = path.parent / (".stack-" + self.name + ".json")
        self.owner = digest(str(path))

    def compose(self, *args, release=None):
        config = dict(self.config, RO_STACK_OWNER=self.owner)
        config["RO_PORT"] = str(self.plan["hub"])
        if "nginx" in self.services:
            config["RO_NGINX_HEALTH_PORT"] = str(self.plan["health"])
        runtime = None
        if release:
            config["RO_STACK_REVISION"] = release["revision"]
            config["RO_XRAY_IMAGE"] = release["images"]["xray"]
            if "nginx" in self.services:
                config["RO_NGINX_IMAGE"] = release["images"]["nginx"]
            runtime = release["images"]["hub"]
        return m.compose(*args, config=config, runtime_image=runtime, project=self.name,
                         profile=self.plan["mode"], local=True).stdout

    def model(self, release):
        model = json.loads(self.compose("config", "--format", "json", release=release))
        m.require(set(model["services"]) == set(self.services), "Unexpected Compose services")
        hub = model["services"]["hub"]
        mounts = hub.get("volumes", [])
        m.require(hub["environment"].get("OWRT_REMOTE_XRAY_CONTROL_BACKEND") == "external_readonly" and
                  hub.get("read_only") is True and not hub.get("privileged") and
                  hub.get("user") == f"{m.HUB_UID}:{m.HUB_GID}" and
                  len(mounts) == 1 and mounts[0]["type"] == "bind" and
                  mounts[0]["source"] == str(self.state) and
                  mounts[0]["target"] == "/var/lib/owrt-remote",
                  "Compose HUB must have only its private state mount and read-only Xray control")
        return digest(model)

    def owns_hub(self, item):
        mounts = [mount for mount in item["Mounts"] if mount["Type"] != "tmpfs"]
        environment = dict(entry.split("=", 1) for entry in item["Config"]["Env"])
        return (item["HostConfig"]["ReadonlyRootfs"] is True and
                item["HostConfig"]["NetworkMode"] == "host" and
                not item["HostConfig"].get("Privileged") and
                item["Config"]["User"] == f"{m.HUB_UID}:{m.HUB_GID}" and
                environment.get("OWRT_REMOTE_XRAY_CONTROL_BACKEND") == "external_readonly" and
                environment.get("OWRT_REMOTE_BIND") == "127.0.0.1" and
                environment.get("OWRT_REMOTE_PORT") == str(self.plan["hub"]) and
                len(mounts) == 1 and mounts[0]["Type"] == "bind" and mounts[0]["RW"] is True and
                mounts[0]["Source"] == str(self.state) and
                mounts[0]["Destination"] == "/var/lib/owrt-remote")

    def guard(self):
        holds = (self.state / ".xray-maintenance-hold",
                 Path("/opt/router-orchestrator/data/.xray-maintenance-hold"))
        m.require(not any(path.exists() or path.is_symlink() for path in holds),
                  "Xray maintenance HOLD is active")
        m.require(not m.status_service(), "Existing host HUB is active")
        legacy = m.ENV
        if legacy.exists() or legacy.is_symlink():
            other = Path(m.settings()["RO_STATE_DIR"]).resolve()
            m.require(not other.is_relative_to(self.state) and not self.state.is_relative_to(other),
                      "Stack state overlaps the existing HUB-only installation")

    def state_permissions(self, initializing=False):
        if initializing and not self.state.exists():
            return
        info = self.state.lstat()
        owner = (info.st_uid, info.st_gid)
        m.require(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o700 and
                  (owner == (m.HUB_UID, m.HUB_GID) or
                   (initializing and owner == (0, 0) and not any(self.state.iterdir()))),
                  "HUB state requires a private directory owned by 23088:23088")

    def material(self):
        paths = [Path(self.config[key]) for key in ("RO_XRAY_CONFIG_DIR", "RO_XRAY_ASSET_DIR", "RO_TLS_DIR")]
        files = {}
        total = 0
        for directory in paths:
            m.require(protected(directory).is_dir(), "Expected an Xray or TLS directory")
            for item in (directory, *sorted(directory.rglob("*"))):
                protected(item)
                info = item.stat()
                m.require(len(files) < 256, "Too many stack input paths")
                if item.is_file():
                    total += info.st_size
                    m.require(total <= 512 * 1024**2, "Stack inputs exceed preflight size limit")
                files[str(item)] = [m.file_hash(item) if item.is_file() else None,
                                    stat.S_IMODE(info.st_mode), info.st_gid]
        config = paths[0] / "owrt-remote.json"
        m.require(config.is_file() and config.stat().st_size <= 4 * 1024**2 and
                  not config.stat().st_mode & 0o007,
                  "Xray configuration must be private and no larger than 4 MiB")
        check_xray(json.loads(config.read_text()), self.plan)
        nginx = self.plan["nginx_conf"]
        if nginx is not None:
            m.require(not nginx.is_relative_to(self.state), "nginx config overlaps HUB state")
            m.require(protected(nginx).is_file() and nginx.stat().st_size <= 1024**2 and
                      nginx.read_text() == render_internal(self.plan),
                      "nginx config differs from the renderer for this env")
            files[str(nginx)] = [m.file_hash(nginx), stat.S_IMODE(nginx.stat().st_mode)]
        return digest(files)

    def release(self):
        images = {}
        for service in self.services:
            reference = self.config[IMAGES[service]]
            records = json.loads(docker("image", "inspect", reference))
            info, = records
            m.require(info["Os"] == "linux" and info["Architecture"] == "amd64" and
                      re.fullmatch(r"sha256:[0-9a-f]{64}", info["Id"]),
                      "Require a local Linux amd64 image for " + service)
            if service != "hub":
                # Require the supplied digest to be associated with the local image.
                m.require(reference in (info.get("RepoDigests") or []),
                          "Image does not match the pinned repository digest for " + service)
            else:
                m.require(re.fullmatch(r"[0-9a-f]{64}", (info["Config"].get("Labels") or {}).get(
                    m.SOURCE_LABEL, "")), "HUB image is missing its source revision")
            images[service] = info["Id"]
        release = {"config": self.config, "images": images, "material": self.material()}
        release["revision"] = digest(release)
        release["model"] = self.model(release)
        return release

    def verify_release(self, release):
        m.require(self.config == release["config"] and self.release() == release,
                  "Stack images, configuration or rendered model changed")

    def preflight(self, release):
        self.verify_release(release)
        if "nginx" in self.services:
            verify_certificate(Path(self.config["RO_TLS_DIR"]), self.plan["host"])
        for service, command in (("xray", ("run", "-test", "-config", "/etc/xray/owrt-remote.json")),
                                 ("nginx", ("nginx", "-t"))):
            if service in self.services:
                try:
                    self.compose("run", "--rm", "--no-deps", "--pull", "never", "-T", service,
                                 *command, release=release)
                except subprocess.CalledProcessError as exc:
                    raise m.Blocked(service + " configuration test failed") from exc
        self.verify_release(release)

    def inspect(self, *releases):
        project_label = "com.docker.compose.project"
        service_label = "com.docker.compose.service"
        ids = docker("ps", "-aq", "--filter", "label=" + project_label + "=" + self.name).split()
        names = {self.name + "-" + service for service in self.services}
        # Detect foreign containers occupying a reserved name before Compose acts.
        for name in names:
            ids.extend(docker("ps", "-aq", "--filter", "name=^/" + name + "$").split())
        containers = json.loads(docker("container", "inspect", *sorted(set(ids)))) if ids else []
        result = {}
        for item in containers:
            labels = item["Config"]["Labels"] or {}
            service = labels.get(service_label)
            m.require(service in self.services and service not in result and
                      labels.get(project_label) == self.name and
                      item["Name"] == "/" + self.name + "-" + service,
                      "Unexpected container in the stack project")
            m.require(any(item["Image"] == r["images"].get(service) and
                          (service == "hub" or labels.get(REVISION) == r["revision"]) for r in releases) and
                      (self.owns_hub(item) if service == "hub" else labels.get(OWNER) == self.owner),
                      "Unexpected container in the stack project")
            result[service] = item
        return result

    def ports(self):
        plan = self.plan
        ports = {"hub": [("127.0.0.1", plan["hub"])],
                 "xray": [("127.0.0.1", port) for port in {plan["wss"], *plan["routes"].values()}]}
        if "nginx" in self.services:
            ports["nginx"] = [("127.0.0.1", plan["tls"]), ("127.0.0.1", plan["health"])]
        if plan["mode"] == "standalone":
            ports["nginx"].extend(("0.0.0.0", plan[key]) for key in ("http", "public"))
        return ports

    def ports_free(self, running=()):
        with contextlib.ExitStack() as resources:
            for service, ports in self.ports().items():
                if service in running:
                    continue
                for addr, port in ports:
                    sock = resources.enter_context(socket.socket())
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        sock.bind((addr, port))
                    except OSError as exc:
                        raise m.Blocked(f"{service} TCP port {port} is occupied") from exc

    def readiness(self, release):
        records = self.inspect(release)
        ready = {service: False for service in self.services}
        for service, probe in (("hub", self.hub_ready), ("xray", self.xray_ready), ("nginx", self.nginx_ready)):
            item = records.get(service)
            if item and item["State"]["Status"] == "running":
                try:
                    ready[service] = probe(item)
                except (OSError, http.client.HTTPException, ValueError):
                    ready[service] = False
        return ready

    def hub_ready(self, item):
        revision = item["Config"]["Labels"][m.SOURCE_LABEL]
        return m.health(self.plan["hub"], attempts=1, revision=revision)

    def xray_ready(self, _item):
        for port in set(self.plan["routes"].values()):
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
        return websocket(http.client.HTTPConnection("127.0.0.1", self.plan["wss"], timeout=2),
                         self.plan["host"], self.plan["wss_path"])

    def nginx_ready(self, _item):
        plan = self.plan
        with contextlib.closing(http.client.HTTPConnection("127.0.0.1", plan["health"], timeout=2)) as conn:
            conn.request("GET", "/healthz")
            with conn.getresponse() as response:
                if response.status != 200:
                    return False
        with contextlib.closing(self.ingress()) as conn:
            conn.connect()
            return True

    def ingress(self):
        # Use SNI and verify the configured TLS leaf on the actual ingress socket.
        plan = self.plan
        cert = Path(self.config["RO_TLS_DIR"]) / "fullchain.pem"
        tls = ssl.create_default_context(cafile=str(cert))
        tls.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        port = plan["public"] if plan["mode"] == "standalone" else plan["tls"]
        return Ingress(plan["host"], port, tls)

    def routed_ready(self):
        with contextlib.closing(self.ingress()) as conn:
            conn.request("GET", "/health")
            with conn.getresponse() as response:
                payload = json.loads(response.read(4096))
                if (response.status != 200 or not isinstance(payload, dict) or
                        payload.get("ok") is not True):
                    return False
        return websocket(self.ingress(), self.plan["host"], self.plan["wss_path"])

    def await_ready(self, release):
        deadline = time.monotonic() + 45
        while True:
            ready = self.readiness(release)
            if all(ready.values()):
                try:
                    if "nginx" not in self.services or self.routed_ready():
                        return
                except (OSError, http.client.HTTPException, ValueError):
                    pass
                ready["HTTPS routing"] = False
            m.require(time.monotonic() < deadline, "Readiness failed: " +
                      ", ".join(name for name, ok in ready.items() if not ok))
            time.sleep(1)

    def stop(self, *releases):
        self.guard()
        containers = self.inspect(*releases)
        for service in reversed(self.services):
            item = containers.get(service)
            if item and item["State"]["Status"] not in {"created", "exited"}:
                docker("stop", item["Id"])
        m.require(all(item["State"]["Status"] in {"created", "exited"}
                      for item in self.inspect(*releases).values()), "Stack did not stop")

    def up(self, release, *allowed):
        self.guard()
        self.state_permissions()
        self.verify_release(release)
        containers = self.inspect(release, *allowed)
        running = [service for service, item in containers.items()
                   if item["State"]["Status"] == "running"]
        self.ports_free(running)
        self.compose("up", "-d", "--no-build", "--pull", "never", *self.services, release=release)
        self.await_ready(release)

    def repair(self, release):
        # Restart only unhealthy services from the verified release. A process
        # can be alive while its application listener is unavailable.
        failed = {service for service, ready in self.readiness(release).items() if not ready}
        self.guard()
        for service, item in self.inspect(release).items():
            if service in failed and item["State"]["Status"] in {"running", "restarting"}:
                docker("stop", item["Id"])
        self.up(release)

    def save(self, journal):
        m.durable_write(self.journal, json.dumps(dict(journal, version=1), sort_keys=True) + "\n")

    def publish(self, config):
        m.durable_write(self.path, "".join(f"{key}={value}\n" for key, value in sorted(config.items())))


class Ingress(http.client.HTTPSConnection):
    """Connect to the local ingress port while using the configured DNS SNI."""
    def __init__(self, domain, port, context):
        super().__init__(domain, port, timeout=3, context=context)
        self._create_connection = lambda addr, *args: socket.create_connection(("127.0.0.1", addr[1]), *args)


def recovered(stack, record):
    tx = record["pending"]
    source, target = record["current"], tx["target"]
    stack.guard()
    if stack.config == target["config"]:
        # On initial start or after the env commit, converge the selected release.
        stack.preflight(target)
        if source:
            m.require(m.schema_fingerprint(stack.config) == tx["target_schema"],
                      "Committed SQLite schema changed")
        stack.up(target, *([source] if source else []))
        observed_schema = m.schema_fingerprint(stack.config)
        if source:
            m.require(observed_schema == tx["target_schema"],
                      "Committed SQLite schema changed after restart")
        stack.save({"current": target, "previous": source, "previous_schema": tx.get("schema"),
                    "snapshot": tx.get("snapshot"), "pending": None})
        return

    m.require(source is not None and stack.config == source["config"],
              "Stack env differs from both journal releases")
    stack.stop(source, target)
    m.require(m.schema_fingerprint(stack.config) == tx["schema"],
              "SQLite schema changed; automatic rollback blocked, live state preserved")
    stack.preflight(source)
    stack.up(source, target)
    m.require(m.schema_fingerprint(stack.config) == tx["schema"], "Recovered SQLite schema changed")
    stack.save(dict(record, pending=None))


def update(stack, record, candidate, *, rollback=False, acknowledge=False):
    source = record["current"]
    m.require(source is not None, "Start the stack before changing releases")
    m.require({key: value for key, value in candidate.config.items() if key not in UPDATE_KEYS} ==
              {key: value for key, value in stack.config.items() if key not in UPDATE_KEYS},
              "Update cannot change project, endpoints, mode or HUB state")
    stack.verify_release(source)
    target = candidate.release()
    m.require(source["material"] != target["material"] or source["images"] != target["images"],
              "Candidate does not change images or staged inputs")
    if source["images"]["hub"] != target["images"]["hub"]:
        m.require(acknowledge or rollback, "HUB update requires --acknowledge-schema-risk")
    m.require(all(stack.readiness(source).values()), "Current release is not ready")
    candidate.preflight(target)
    schema = m.schema_fingerprint(stack.config)
    if rollback:
        m.require(target == record["previous"] and schema == record["previous_schema"],
                  "Prior images or SQLite schema are incompatible")

    tx = {"target": target, "schema": schema, "target_schema": None, "snapshot": None}
    stack.save(dict(record, pending=tx))
    try:
        stack.stop(source)
        m.require(m.schema_fingerprint(stack.config) == schema, "SQLite schema changed before switch")
        tx["snapshot"] = str(m.backup_state(stack.state, stack.path.parent / "backups"))
        stack.save(dict(record, pending=tx))
        candidate.up(target, source)
        tx["target_schema"] = m.schema_fingerprint(candidate.config)
        if rollback:
            m.require(tx["target_schema"] == schema, "Rollback changed the SQLite schema")
        stack.save(dict(record, pending=tx))
        stack.publish(candidate.config)
        stack.save({"current": target, "previous": source, "previous_schema": schema,
                    "snapshot": tx["snapshot"], "pending": None})
    except Exception:
        # Read the journal and active env again in case an atomic rename already succeeded.
        persisted = json.loads(stack.journal.read_text())
        if persisted["pending"]:
            recovered(Stack(stack.path, open_settings(stack.path)), persisted)
        raise


def initialize(stack, record):
    stack.guard()
    stack.inspect()
    stack.state_permissions(initializing=True)
    m.require(not stack.state.exists() or not any(stack.state.iterdir()),
              "Initial start requires empty stack state")
    stack.ports_free()
    release = stack.release()
    stack.preflight(release)
    if not stack.state.exists():
        stack.state.mkdir(mode=0o700)
    os.chown(stack.state, m.HUB_UID, m.HUB_GID)
    m.sync_directory(stack.state)
    m.sync_directory(stack.state.parent)
    tx = {"target": release}
    stack.save(dict(record, pending=tx))
    try:
        stack.up(release)
        m.schema_fingerprint(stack.config)
    except Exception:
        stack.stop(release)
        raise
    stack.save(dict(record, current=release, pending=None))


def run(args):
    stack = Stack(args.env_file, open_settings(args.env_file))
    record = {"version": 1, "current": None, "pending": None}
    if stack.journal.exists() or stack.journal.is_symlink():
        record = json.loads(protected(stack.journal).read_text())
        m.require(record.get("version") == 1, "Unsupported stack journal")
    current, pending = record["current"], record["pending"]
    action = args.operation
    m.require(not pending or action in {"recover", "status"}, "Pending stack transaction: run stack recover")
    if not pending and current:
        m.require(current["config"] == stack.config, "Active env changed outside Docker Manager")

    if action == "status":
        release = pending["target"] if pending and pending["target"]["config"] == stack.config else current
        if pending:
            print("Pending stack transaction")
        if release:
            ready = stack.readiness(release) if not pending else {"ready": False}
            print(json.dumps(ready))
            m.require(all(ready.values()), "Stack is not ready")
        else:
            stack.inspect()
            print("Stack is uninitialized")
        return
    if action == "validate":
        stack.state_permissions(initializing=current is None)
        containers = stack.inspect(*([current] if current else []))
        stack.ports_free({service for service, item in containers.items()
                          if item["State"]["Status"] == "running"})
        stack.preflight(current or stack.release())
        for filename in render_external(stack.plan):
            print("External ingress review artifact:", filename)
    else:
        m.require(not m.pending_transaction(), "Pending HUB-only transaction: run recover")
        stack.guard()
        if action == "recover" and pending:
            recovered(stack, record)
        elif action == "start" and not current:
            initialize(stack, record)
        else:
            m.require(current is not None, "Stack is uninitialized")
            if action == "stop":
                stack.stop(current)
            elif action in {"start", "recover"}:
                stack.preflight(current)
                m.schema_fingerprint(stack.config)
                if action == "recover":
                    stack.repair(current)
                else:
                    stack.up(current)
            elif action == "update":
                candidate = Stack(stack.path, open_settings(args.candidate_env))
                update(stack, record, candidate, acknowledge=args.acknowledge_schema_risk)
            elif action == "rollback":
                m.require(record.get("previous"), "No verified prior stack release")
                update(stack, record, Stack(stack.path, record["previous"]["config"]), rollback=True)
    print("STACK", action, "PASS")


if __name__ == "__main__":
    raise SystemExit("Use docker/manager.py stack")
