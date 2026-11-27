"""Opt-in Xray control service for one root-configured unified Compose stack."""

import http.client
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import sqlite3
import struct
import subprocess
import tempfile
import time

import manager as m
import unified_transport_catalog as transport_catalog
from stack_runtime import OWNER, REVISION, Stack, open_settings, protected
from unified_xray_state import BACKEND, MAX_CONFIG, ROUTER_ID, TOKEN, XrayState, profile_ports, read_private, router_spec
from unified_transport_runtime import Runtime

MAX_REQUEST = 8192
XRAY_TMPFS = {
    "/tmp": "rw,nosuid,nodev,size=32m",
    "/usr/local/etc/xray": "ro,nosuid,nodev,noexec,size=1m",
    "/var/log/xray": "rw,nosuid,nodev,noexec,size=32m,uid=23088,gid=23088,mode=0700",
}
OPERATIONS = {
    "status": {frozenset()},
    "core.info": {frozenset()},
    "core.start": {frozenset({"action", "version", "confirmed"})},
    "core.job": {frozenset({"job_id"})},
    "version": {frozenset()},
    "health": {frozenset()},
    "validate_config": {frozenset({"router_id", "router"})},
    "apply_config": {frozenset({"router_id", "router", "transaction"}), frozenset({"commit"})},
    "restart": {frozenset()},
    "rollback": {frozenset({"transaction"})},
    "transport.recover": {frozenset()},
    **{op: {frozenset(fields)} for op, fields in transport_catalog.OPERATIONS.items()},
}


def docker(*args, timeout=12):
    return m.call("docker", "--host", "unix:///var/run/docker.sock", *args,
                  env=m.local_docker_env(), timeout=timeout).stdout


def socket_address(stack):
    return "\0router-orchestrator-xray-" + str(stack.plan["hub"])


def peer_is_hub(pid, uid, gid, hub):
    if (uid, gid) != (m.HUB_UID, m.HUB_GID) or pid <= 0:
        return False
    init = hub["State"]["Pid"]
    if hub["State"]["Status"] != "running" or init <= 0:
        return False
    try:
        client_proc, hub_proc = Path(f"/proc/{pid}"), Path(f"/proc/{init}")
        return (os.path.samefile(client_proc / "ns/pid", hub_proc / "ns/pid") and
                not os.path.samefile(hub_proc / "ns/pid", "/proc/self/ns/pid") and
                (client_proc / "cgroup").read_bytes() == (hub_proc / "cgroup").read_bytes())
    except OSError:
        return False


def read_request(channel):
    # An idle timeout alone lets a slow peer hold the listener indefinitely.
    deadline = time.monotonic() + 4
    raw = bytearray()
    while len(raw) < MAX_REQUEST:
        remaining = deadline - time.monotonic()
        m.require(remaining > 0, "IPC deadline exceeded")
        channel.settimeout(remaining)
        block = channel.recv(MAX_REQUEST - len(raw))
        if not block:
            break
        raw.extend(block)
        if b"\n" in block:
            m.require(raw.count(b"\n") == 1 and raw.endswith(b"\n"), "Invalid IPC framing")
            return json.loads(raw)
    raise m.Blocked("Invalid IPC framing")


def parameters(request):
    m.require(isinstance(request, dict), "Invalid request")
    args = dict(request)
    op = args.pop("op", None)
    m.require(isinstance(op, str) and frozenset(args) in OPERATIONS.get(op, ()),
              "Operation or parameters denied")
    for name in ("transaction", "commit"):
        if name in args:
            m.require(isinstance(args[name], str) and TOKEN.fullmatch(args[name]),
                      "Invalid transaction identifier")
    if "router_id" in args:
        name = args["router_id"]
        m.require(isinstance(name, str) and ROUTER_ID.fullmatch(name), "Invalid router ID")
        if "router" in args and args["router"] is not None:
            m.require(router_spec(args["router"])["id"] == name, "Router IDs differ")
    if op in transport_catalog.OPERATIONS:
        transport_catalog.validate_request(op, args)
    return op, args


class Adapter:
    def __init__(self, env):
        self.env = env
        self.core_worker = None

    def open_project(self, *, allow_core_pending=False):
        stack = Stack(self.env, open_settings(self.env), command_timeout=6)
        m.require(stack.config["RO_XRAY_BACKEND"] == BACKEND, "Xray adapter is disabled")
        from unified_core_manager import load_record
        journal = load_record(stack)
        release = journal["current"]
        pending = journal["pending"]
        m.require(pending is None or (allow_core_pending and pending.get("kind") == "core"),
                  "Stack release unavailable or pending")
        allowed = (release, pending["target"]) if pending else (release,)
        m.require(any(item["config"] == stack.config for item in allowed), "Active stack env changed")
        records = stack.inspect(*allowed)
        hub = records["hub"]
        m.require(hub["HostConfig"].get("PidMode", "") in {"", "private"},
                  "HUB requires a private PID namespace")
        return stack, release, records

    @staticmethod
    def owned_xray(stack, release, record):
        host, config = record["HostConfig"], record["Config"]
        labels = config["Labels"]
        mounts = [mount for mount in record["Mounts"]
                  if not (mount["Type"] == "tmpfs" and mount["Destination"] in XRAY_TMPFS)]
        expected = {"/etc/xray": stack.config["RO_XRAY_CONFIG_DIR"],
                    "/usr/local/share/xray": stack.config["RO_XRAY_ASSET_DIR"],
                    "/etc/router-orchestrator/tls": stack.config["RO_TLS_DIR"]}
        m.require(record["Image"] == release["images"]["xray"] and
                  labels.get(OWNER) == stack.owner and labels.get(REVISION) == release["revision"] and
                  labels.get("com.docker.compose.project") == stack.name and
                  labels.get("com.docker.compose.service") == "xray" and
                  record["Name"] == "/" + stack.name + "-xray" and
                  re.fullmatch(r"[a-f0-9]{64}", record["Id"]),
                  "Xray ownership verification failed: identity")
        m.require(host["NetworkMode"] == "host" and host["ReadonlyRootfs"] is True and
                  not host["Privileged"] and not host.get("CapAdd") and not host.get("Devices") and
                  host.get("PidMode", "") in {"", "private"} and config["User"] == f"{m.HUB_UID}:{m.HUB_GID}" and
                  config["Cmd"] == ["run", "-config", "/etc/xray/owrt-remote.json"],
                  "Xray ownership verification failed: runtime")
        tmpfs = host.get("Tmpfs") or {}
        m.require(set(tmpfs) == set(XRAY_TMPFS) and
                  all(set(options.split(",")) == set(XRAY_TMPFS[path].split(","))
                      for path, options in tmpfs.items()),
                  "Xray ownership verification failed: tmpfs")
        m.require(len(mounts) == len(expected) and
                  all(mount["Type"] == "bind" and mount["RW"] is False and
                      expected.get(mount["Destination"]) == mount["Source"] for mount in mounts) and
                  {mount["Destination"] for mount in mounts} == expected.keys(),
                  "Xray ownership verification failed: bind-mounts")
        return record["Id"]

    def probe(self, stack, release, routers, *, extra_ports=None):
        try:
            item, = json.loads(docker("container", "inspect", stack.name + "-xray", timeout=4))
            self.owned_xray(stack, release, item)
            ports = [port for spec in routers.values() for port in spec["ports"]]
            if extra_ports is None:
                record = XrayState(stack).load()
                extra_ports = profile_ports(record.get("profiles", []))
            ports.extend(extra_ports)
            return item["State"]["Status"] == "running" and stack.xray_ready(item, ports)
        except (OSError, http.client.HTTPException):
            return False

    def restart(self, stack, release, container, routers, *, extra_ports=None):
        stack.guard()
        m.require(self.owned_xray(stack, release, stack.inspect(release)["xray"]) == container,
                  "Xray container changed")
        docker("restart", "--time", "3", container)
        deadline = time.monotonic() + 12
        while not (self.probe(stack, release, routers) if extra_ports is None else
                   self.probe(stack, release, routers, extra_ports=extra_ports)):
            m.require(time.monotonic() < deadline, "Xray readiness failed")
            time.sleep(0.2)

    def test_config(self, stack, release, data, *, image=None):
        with tempfile.TemporaryDirectory(prefix=".xray-test-", dir=stack.path.parent) as directory:
            candidate = Path(directory) / "candidate.json"
            candidate.write_bytes(data)
            os.chown(candidate, 0, m.HUB_GID)
            candidate.chmod(0o640)
            mounts = []
            for source, target in ((stack.config["RO_XRAY_CONFIG_DIR"], "/etc/xray"),
                                   (candidate, "/etc/xray/owrt-remote.json"),
                                   (stack.config["RO_XRAY_ASSET_DIR"], "/usr/local/share/xray"),
                                   (stack.config["RO_TLS_DIR"], "/etc/router-orchestrator/tls")):
                mounts.extend(("--mount", f"type=bind,src={source},dst={target},readonly"))
            docker("run", "--rm", "--pull=never", "--network=none", "--read-only",
                   "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                   "--pids-limit=64", "--memory=128m", "--user", f"{m.HUB_UID}:{m.HUB_GID}",
                   "--env", "XRAY_LOCATION_ASSET=/usr/local/share/xray",
                   *(arg for path, options in XRAY_TMPFS.items()
                     for arg in ("--tmpfs", path + ":" + options)),
                   *mounts, image or release["images"]["xray"], "run", "-test", "-config", "/etc/xray/owrt-remote.json")

    def handle(self, stack, release, records, request):
        op, args = parameters(request)
        container = self.owned_xray(stack, release, records["xray"])
        state = XrayState(stack)
        record = state.load()
        pending = record["pending"]
        profile_pending = record.get("profile_pending")
        stack.verify_release(release, recover_xray=bool(pending or profile_pending))
        hold = stack.xray_hold()
        runtime = Runtime(self, stack, release, state, container, docker)
        if op == "status":
            operations = {"status", "version", "health"}
            if not hold:
                if profile_pending:
                    operations.add("transport.recover")
                elif pending:
                    operations.update({"apply_config", "rollback"})
                else:
                    operations.update({"validate_config", "apply_config", "restart"})
            return {"backend": BACKEND, "maintenance_hold": hold,
                    "pending": pending["token"] if pending else None,
                    "profile_recovery_pending": bool(profile_pending),
                    "ready": not (pending or profile_pending) and self.probe(stack, release, record["routers"]),
                    "operations": sorted(operations),
                    "public_port": stack.plan["public"], "wss_path": stack.plan["wss_path"]}
        if op == "health":
            return {"ready": not (pending or profile_pending) and self.probe(stack, release, record["routers"])}
        if op == "version":
            output = docker("exec", container, "xray", "version")
            version = re.match(r"Xray ([0-9]+\.[0-9]+\.[0-9]+)\b", output)
            m.require(version, "Xray version unavailable")
            return {"version": version[1], "image": release["images"]["xray"]}

        if op in {"transport.list", "transport.detail", "transport.router_profiles", "transport.router_portals"}:
            return transport_catalog.handle(self, stack, release, state, op, args, runtime=runtime)
        stack.guard()
        m.require(not m.pending_transaction(), "Pending HUB-only deployment")
        if profile_pending:
            runtime.recover(record)
            record = state.load()
        if op == "transport.recover":
            return {"recovered": bool(profile_pending)}
        if op == "rollback" or "commit" in args:
            token = args.get("commit", args.get("transaction"))
            if "commit" in args and record.get("committed") == token:
                return {}
            m.require(pending and pending["token"] == token, "Xray transaction is not pending")
            if op == "rollback":
                self.restore(stack, release, container, state, record)
            else:
                routers = pending["routers"]
                m.require(read_private(state.config, MAX_CONFIG) == state.render(record, routers),
                          "Prepared Xray configuration is missing")
                # The commit receipt in SQLite is authoritative after a HUB crash.
                self.ensure_ready(stack, release, container, routers)
                state.save(dict(record, routers=routers, previous=record["routers"],
                                committed=token, pending=None))
            return {}
        m.require(not pending, "Pending Xray transaction")
        if op in transport_catalog.OPERATIONS:
            return transport_catalog.handle(self, stack, release, state, op, args, container=container,
                                            runtime=runtime)
        if op == "restart":
            self.restart(stack, release, container, record["routers"])
            return {"ready": True}

        routers = dict(record["routers"])
        name, router = args["router_id"], args["router"]
        if router is None:
            m.require(name in routers, "Router is outside adapter ownership")
            m.require(not any(name in item.get("assignments", {}) for item in record.get("profiles", [])),
                      "Router has assigned transport profiles")
            del routers[name]
        else:
            routers[name] = router
        candidate = state.render(record, routers)
        old_ports = {port for spec in record["routers"].values() for port in spec["ports"]}
        if router:
            for port in set(router["ports"]) - old_ports:
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", port))
        self.test_config(stack, release, candidate)
        if op == "validate_config" or routers == record["routers"]:
            return {"changed": False}
        # The baseline and prior router inputs are durable before replacing the config.
        state.save(dict(record, pending={"token": args["transaction"], "routers": routers}))
        try:
            stack.guard()
            state.replace(candidate)
            self.restart(stack, release, container, routers)
        except Exception:
            self.restore(stack, release, container, state, record)
            raise
        return {"changed": True}

    def ensure_ready(self, stack, release, container, routers):
        if not self.probe(stack, release, routers):
            self.restart(stack, release, container, routers)

    def restore(self, stack, release, container, state, record):
        stack.guard()
        state.replace(state.render(record, record["routers"]))
        self.restart(stack, release, container, record["routers"])
        state.save(dict(record, pending=None))

    def serve(self, channel):
        channel.settimeout(4)
        authenticated = False
        try:
            pid, uid, gid = struct.unpack("3i", channel.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            if (uid, gid) != (m.HUB_UID, m.HUB_GID):
                return
            stack, _, records = self.open_project(allow_core_pending=True)
            if not peer_is_hub(pid, uid, gid, records["hub"]):
                return
            authenticated = True
            request = read_request(channel)
            op, args = parameters(request)
            if op == "core.job":
                from core_job_runner import settle_unified_job, unified_job_status
                result = unified_job_status(stack, args["job_id"])
                if result["job"]["status"] in ("pending", "running"):
                    try:
                        with m.deployment_lock():
                            settle_unified_job(stack, args["job_id"])
                    except m.Blocked:
                        pass
                    result = unified_job_status(stack, args["job_id"])
            else:
                with m.deployment_lock():
                    stack, release, records = self.open_project(allow_core_pending=op in {"core.start", "core.info"})
                    if not peer_is_hub(pid, uid, gid, records["hub"]):
                        return
                    if op == "core.start":
                        from core_job_runner import queue_unified
                        result = queue_unified(self, stack, args)
                    elif op == "core.info":
                        from unified_core_manager import load_record
                        pending = load_record(stack)["pending"]
                        item = records.get("xray")
                        active = item is not None and item["State"]["Status"] == "running"
                        version = None
                        running_release = release
                        if item is not None:
                            if pending and item["Config"]["Labels"][REVISION] == pending["target"]["revision"]:
                                running_release = pending["target"]
                            container = self.owned_xray(stack, running_release, item)
                            if active:
                                output = docker("exec", container, "xray", "version")
                                version = re.match(r"Xray ([0-9]+\.[0-9]+\.[0-9]+)\b", output)
                        enabled = not stack.xray_hold()
                        result = {"active": active, "image": running_release["config"]["RO_XRAY_IMAGE"],
                                  "image_id": running_release["images"]["xray"] if item is not None else "",
                                  "version": version[1] if version else "", "architecture": "amd64",
                                  "runtime": "docker", "maintenance_hold": not enabled,
                                  "recovery_pending": bool(pending),
                                  "may_prepare": enabled, "may_upgrade": enabled, "may_restart": enabled}
                    else:
                        result = self.handle(stack, release, records, request)
            response = {"ok": True, **result}
        except (m.Blocked, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError,
                subprocess.SubprocessError, sqlite3.Error):
            if not authenticated:
                return
            response = {"ok": False, "error": "Xray adapter request failed"}
        channel.settimeout(4)
        try:
            channel.sendall(json.dumps(response).encode() + b"\n")
        except OSError:
            pass

    def run(self):
        m.require_root()
        m.require(os.getegid() == 0, "Adapter requires root group")
        stack = Stack(self.env, open_settings(self.env))
        m.require(stack.config["RO_XRAY_BACKEND"] == BACKEND, "Xray adapter is disabled")
        protected(m.LOCK.parent)
        adapter = self

        from core_job_runner import recover_unified
        try:
            recover_unified(self)
        except (m.Blocked, OSError, ValueError, KeyError, TypeError,
                subprocess.SubprocessError, sqlite3.Error):
            print("Core recovery unavailable at startup; watchdog will retry", flush=True)

        if XrayState(stack).exists() and XrayState(stack).load().get("profile_pending"):
            try:
                with m.deployment_lock():
                    stack, release, records = self.open_project()
                    self.handle(stack, release, records, {"op": "transport.recover"})
            except (m.Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError, sqlite3.Error):
                print("Profile recovery pending", flush=True)

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                adapter.serve(self.request)

        class Server(socketserver.UnixStreamServer):
            def service_actions(self):
                if adapter.core_worker is not None and adapter.core_worker.poll() is not None:
                    adapter.core_worker = None

        with Server(socket_address(stack), Handler) as server:
            server.serve_forever()

