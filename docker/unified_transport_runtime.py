"""Commit a profile change across the catalog and the owned stack runtime."""
import contextlib
import copy
import hashlib
import json
import os
import re
import stat
import time
import uuid

import manager as m
from secure_ingress import plan_ingress
from secure_ingress_inventory import live_tcp_ports
from secure_profile_catalog import desired_profiles, profile_selection, publish_verified, set_desired_profiles
from secure_profile_store import connection, load_artifact
from secure_publication_transaction import atomic
from secure_reverse_probe import build_probe
from stack_runtime import protected
from rehearse_secure_reverse import Proof, proof_matches, reserve
from transport_profiles import render_router_outbound
from unified_xray_state import MAX_CONFIG, TOKEN, assignment_id, assignment_tag, encode, profile_ports, profile_tag


OPERATIONS = {"transport.publish", "transport.activate", "transport.disable", "transport.rollback"}


def key(item):
    return item["profile"]["name"], item["revision"]


def ref_key(ref):
    return f"{ref[0]}:{ref[1]}"


def used(db, ref):
    return any((item["profile_id"], item["revision"]) == ref
               for (data,) in db.execute("SELECT desired_profiles FROM secure_router_preferences")
               for item in json.loads(data))


def receipt(db):
    db.execute("CREATE TABLE IF NOT EXISTS unified_transport_commit (token TEXT PRIMARY KEY)")
    row = db.execute("SELECT token FROM unified_transport_commit").fetchone()
    return row[0] if row else None


class Runtime:
    def __init__(self, adapter, stack, release, state, container, docker):
        self.adapter = adapter
        self.stack = stack
        self.release = release
        self.state = state
        self.container = container
        self.docker = docker

    def nginx(self):
        item = self.stack.inspect(self.release)["nginx"]
        host = item["HostConfig"]
        expected = {"/etc/nginx/nginx.conf": str(self.stack.plan["nginx_conf"]),
                    "/etc/router-orchestrator/tls": self.stack.config["RO_TLS_DIR"]}
        mounts = [mount for mount in item["Mounts"] if mount["Type"] != "tmpfs"]
        m.require(re.fullmatch(r"[a-f0-9]{64}", item["Id"]) and
                  host["NetworkMode"] == "host" and host["ReadonlyRootfs"] and
                  not host["Privileged"] and not host.get("Devices") and
                  {cap.removeprefix("CAP_") for cap in host.get("CapAdd") or ()} ==
                  {"NET_BIND_SERVICE", "CHOWN", "SETGID", "SETUID"} and
                  len(mounts) == len(expected) and
                  {mount["Destination"] for mount in mounts} == expected.keys() and
                  all(mount["Type"] == "bind" and not mount["RW"] and
                      expected[mount["Destination"]] == mount["Source"] for mount in mounts),
                  "nginx runtime ownership verification failed")
        return item

    def apply(self, profiles, data, routers, *, trial_ports=()):
        stack = self.stack
        stack.guard()
        stack.verify_release(self.release, recover_xray=True)
        self.state.replace(data)
        ports = profile_ports(profiles) + tuple(trial_ports)
        self.adapter.restart(stack, self.release, self.container, routers, extra_ports=ports)
        if stack.plan["mode"] == "standalone":
            nginx = self.nginx()
            path = protected(stack.plan["nginx_conf"])
            previous = path.read_bytes()
            config = stack.render_profile_nginx(profiles).encode()
            if config != previous:
                info = path.stat()
                atomic(path, config, stat.S_IMODE(info.st_mode), (info.st_uid, info.st_gid))
            # A file bind can still refer to the previous inode after rename.
            loaded = ""
            if nginx["State"]["Status"] == "running":
                loaded = self.docker("exec", nginx["Id"], "cat", "/etc/nginx/nginx.conf", timeout=4)
            if loaded.rstrip("\n") != config.decode().rstrip("\n") or config != previous:
                stack.guard()
                stack.compose("run", "--rm", "--no-deps", "--pull", "never", "-T",
                              "nginx", "nginx", "-t", release=self.release)
                m.require(self.nginx()["Id"] == nginx["Id"], "nginx container changed")
                self.docker("restart", "--time", "3", nginx["Id"])
                m.require(self.docker("exec", nginx["Id"], "cat", "/etc/nginx/nginx.conf",
                                      timeout=4).rstrip("\n") == config.decode().rstrip("\n"),
                          "nginx did not mount the expected configuration")
            deadline = time.monotonic() + 6
            while True:
                try:
                    if stack.nginx_ready(nginx) and stack.routed_ready():
                        break
                except OSError:
                    pass
                m.require(time.monotonic() < deadline, "Owned ingress is unavailable")
                time.sleep(0.2)

    def stop_probe(self, token):
        self.stack.guard()
        m.require(TOKEN.fullmatch(token), "Invalid profile probe identity")
        name = self.stack.name + "-probe-" + token
        ids = self.docker("ps", "-aq", "--filter", "name=^/" + name + "$", timeout=4).split()
        if ids:
            item, = json.loads(self.docker("container", "inspect", *ids, timeout=4))
            m.require(item["Name"] == "/" + name and
                      item["Config"]["Labels"].get("org.router-orchestrator.profile-probe") == token and
                      item["Image"] == self.release["images"]["xray"] and
                      re.fullmatch(r"[a-f0-9]{64}", item["Id"]), "Foreign profile probe container")
            self.docker("rm", "-f", "-v", item["Id"], timeout=6)
        directory = self.stack.path.parent / (".transport-probe-" + token)
        if directory.exists() or directory.is_symlink():
            m.require(protected(directory).is_dir() and not directory.stat().st_mode & 0o077,
                      "Unsafe profile probe directory")
            candidate = directory / "client.json"
            if candidate.exists() or candidate.is_symlink():
                m.require(protected(candidate).is_file() and candidate.stat().st_nlink == 1,
                          "Unsafe profile probe configuration")
                candidate.unlink()
            directory.rmdir()
            m.sync_directory(directory.parent)

    def prove(self, record, profiles, data, item, pending):
        profile = item["profile"]
        token = pending["token"]
        version = self.docker("exec", self.container, "xray", "version")
        with contextlib.ExitStack() as resources:
            sockets = [reserve() for _ in range(4)]
            for sock in sockets:
                resources.callback(sock.close)
            admin, ssh, admin_target, ssh_target = [sock.getsockname()[1] for sock in sockets]
            proof_admin, proof_ssh = [Proof(sock) for sock in sockets[2:]]
            resources.callback(proof_admin.close)
            resources.callback(proof_ssh.close)
            server, client = build_probe(
                profile, ingress_port=item["public_port"], admin_port=admin, ssh_port=ssh,
                admin_target=admin_target, ssh_target=ssh_target, core_version=version,
                proxy_protocol=False, probe_name="trial-" + token[:12])
            trial = json.loads(data)
            live = next(entry for entry in trial["inbounds"] if entry["tag"] == profile_tag(item))
            live["settings"]["clients"].extend(server["inbounds"][0]["settings"]["clients"])
            tags = {entry["tag"] for entry in trial["inbounds"]}
            m.require(not tags.intersection(entry["tag"] for entry in server["inbounds"][1:]),
                      "Probe listener tag conflicts")
            trial["inbounds"].extend(server["inbounds"][1:])
            trial["routing"]["rules"][0:0] = server["routing"]["rules"]
            for outbound in client["outbounds"]:
                if outbound["protocol"] == "vless":
                    outbound["settings"]["address"] = item["remote_host"]
            trial = encode(trial)
            m.require(len(trial) <= MAX_CONFIG, "Probe configuration exceeds limit")
            self.adapter.test_config(self.stack, self.release, trial)
            client_bytes = encode(client)
            self.adapter.test_config(self.stack, self.release, client_bytes)
            pending["trial_sha256"] = hashlib.sha256(trial).hexdigest()
            self.state.save(dict(record, profile_pending=pending))
            for sock in sockets[:2]:
                sock.close()
            self.apply(profiles, trial, record["routers"], trial_ports=(admin, ssh))
            self.stack.guard()
            directory = self.stack.path.parent / (".transport-probe-" + token)
            directory.mkdir(mode=0o700)
            candidate = directory / "client.json"
            try:
                fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    os.fchown(stream.fileno(), 0, m.HUB_GID)
                    os.fchmod(stream.fileno(), 0o640)
                    stream.write(client_bytes)
                self.docker("run", "-d", "--pull=never", "--network=host", "--read-only",
                            "--name", self.stack.name + "-probe-" + token,
                            "--label", "org.router-orchestrator.profile-probe=" + token,
                            "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                            "--pids-limit=64", "--memory=128m", "--user", f"{m.HUB_UID}:{m.HUB_GID}",
                            "--mount", f"type=bind,src={candidate},dst=/probe.json,readonly",
                            self.release["images"]["xray"], "run", "-config", "/probe.json")
                deadline = time.monotonic() + 40
                while not (proof_matches(admin, proof_admin) and proof_matches(ssh, proof_ssh)):
                    m.require(time.monotonic() < deadline, "Ingress reverse probe failed")
                    time.sleep(0.2)
            finally:
                self.stop_probe(token)

    def recover(self, record):
        pending = record["profile_pending"]
        self.stack.guard()
        self.stop_probe(pending["token"])
        with connection(protected(self.stack.transport_store)) as db:
            committed = receipt(db) == pending["token"]
        profiles = pending["profiles"] if committed else record.get("profiles", [])
        data = self.state.render(record, record["routers"], profiles=profiles)
        self.apply(profiles, data, record["routers"])
        if committed:
            record = dict(record, profiles=profiles, profile_rollbacks=pending["rollbacks"],
                          profile_committed=pending["token"])
        self.state.save(dict(record, profile_pending=None))

    def change(self, db, op, ref, artifact, digest, published):
        record = self.state.load()
        current = record.get("profiles", [])
        active = [entry for entry in current if key(entry) == ref]
        rollbacks = dict(record.get("profile_rollbacks", {}))
        ref_id = ref_key(ref)
        if op == "transport.rollback":
            m.require(ref_id in rollbacks, "No matching profile rollback")
            saved = rollbacks.pop(ref_id)
            candidate = [entry for entry in current if key(entry) != ref]
            if saved is not None:
                candidate.append(saved)
        elif op == "transport.disable":
            m.require(published and active, "Profile is not active")
            rollbacks[ref_id] = active[0]
            candidate = [entry for entry in current if key(entry) != ref]
        else:
            m.require(not active and published == (op == "transport.activate"),
                      "Profile is already active or has not been published")
            rollbacks[ref_id] = None
            candidate = current + [dict(artifact, revision=ref[1])]
        removed = {key(entry) for entry in current} - {key(entry) for entry in candidate}
        m.require(not any(used(db, entry) for entry in removed), "Profile revision is assigned")
        additions = [entry for entry in candidate if key(entry) not in {key(old) for old in current}]
        if additions:
            busy = live_tcp_ports()
            for entry in additions:
                m.require(entry["profile"]["port"] not in busy, "Profile listener port is occupied")
        occupied = set(self.stack.plan["routes"])
        occupied.add(self.stack.plan["host"])
        for entry in candidate:
            for route in plan_ingress(entry["profile"], public_port=entry["public_port"])["routes"]:
                m.require(route["sni"] not in occupied, "Profile SNI conflicts with another route")
                occupied.add(route["sni"])
        data = self.state.render(record, record["routers"], profiles=candidate)
        def commit_catalog():
            for entry in additions:
                stored, _, _ = load_artifact(db, *key(entry))
                m.require(dict(stored, revision=entry["revision"]) == entry, "Artifact changed")
            if op == "transport.publish":
                publish_verified(db, *ref, verified_artifact_sha256=digest)

        self.transact(db, record, candidate, data, rollbacks, additions, commit_catalog)
        return {"published": published or op == "transport.publish",
                "active": any(key(entry) == ref for entry in candidate), "applied": True}

    def assign(self, db, router_id, selection):
        record = self.state.load()
        m.require(router_id in record["routers"], "Router is outside adapter ownership")
        wanted = profile_selection(selection) if selection is not None else set()
        current = record.get("profiles", [])
        candidate = copy.deepcopy(current)
        m.require(wanted <= {key(entry) for entry in candidate}, "Profile selection requires active revisions")
        current_assignment = {key(entry) for entry in current if router_id in entry.get("assignments", {})}
        if selection is None:
            m.require(self.adapter.probe(self.stack, self.release, record["routers"]),
                      "Enrollment listener readiness failed")
            for entry in candidate:
                entry.get("assignments", {}).pop(router_id, None)
        else:
            m.require(current_assignment <= wanted, "Migrate the router before removing assigned revisions")
        with contextlib.ExitStack() as sockets:
            for entry in candidate:
                if key(entry) not in wanted - current_assignment:
                    continue
                ports = [sockets.enter_context(reserve()).getsockname()[1] for _ in range(2)]
                entry.setdefault("assignments", {})[router_id] = {
                    "clients": [str(uuid.uuid4()), str(uuid.uuid4())], "ports": ports,
                }
            data = self.state.render(record, record["routers"], profiles=candidate)
        def commit_catalog():
            if selection is None:
                db.execute("DELETE FROM secure_router_preferences WHERE router_id=?", (router_id,))
            else:
                set_desired_profiles(db, router_id, selection)

        if current != candidate:
            self.transact(db, record, candidate, data, record.get("profile_rollbacks", {}), [], commit_catalog)
        else:
            db.execute("BEGIN IMMEDIATE")
            commit_catalog()
        return {"router_id": router_id, "profiles": selection or [], "applied": True}

    def router_profiles(self, db, router_id):
        record = self.state.load()
        m.require(not record.get("profile_pending") and not record["pending"] and
                  router_id in record["routers"], "Router profile state is unavailable")
        active = {key(entry): entry for entry in record.get("profiles", [])}
        ordered = []
        for ref in desired_profiles(db, router_id):
            entry = active[(ref["profile_id"], ref["revision"])]
            pair = entry["assignments"][router_id]
            profile = dict(entry["profile"], port=entry["public_port"])
            outbound = [render_router_outbound(profile, router_uuid=identity, remote_host=entry["remote_host"],
                                               reverse_tag=assignment_tag(pair, index))
                        for index, identity in enumerate(pair["clients"])]
            ordered.append({**ref, "assignment_id": assignment_id(pair), "transport": profile["transport"], "outbounds": outbound})
        return {"profiles": ordered}

    def router_portals(self, router_id, ref):
        record = self.state.load()
        m.require(not record.get("profile_pending") and not record["pending"] and
                  router_id in record["routers"], "Router profile state is unavailable")
        if ref is None:
            return {"ports": record["routers"][router_id]["ports"]}
        for entry in record.get("profiles", []):
            if key(entry) == (ref["profile_id"], ref["revision"]):
                pair = entry.get("assignments", {}).get(router_id)
                if pair and ref.get("assignment_id", assignment_id(pair)) == assignment_id(pair):
                    return {"ports": pair["ports"]}
        raise m.Blocked("Profile revision is not assigned")

    def transact(self, db, record, candidate, data, rollbacks, additions, commit_catalog):
        self.adapter.test_config(self.stack, self.release, data)
        pending = {"token": uuid.uuid4().hex, "profiles": candidate, "rollbacks": rollbacks}
        receipt(db)
        db.commit()
        self.state.save(dict(record, profile_pending=pending))
        try:
            for entry in additions:
                self.prove(record, candidate, data, entry, pending)
            self.apply(candidate, data, record["routers"])
            db.execute("BEGIN IMMEDIATE")
            commit_catalog()
            db.execute("DELETE FROM unified_transport_commit")
            db.execute("INSERT INTO unified_transport_commit VALUES (?)", (pending["token"],))
            db.commit()
            self.state.save(dict(record, profiles=candidate, profile_rollbacks=rollbacks,
                                 profile_committed=pending["token"], profile_pending=None))
        except Exception:
            db.rollback()
            self.recover(self.state.load())
            raise
