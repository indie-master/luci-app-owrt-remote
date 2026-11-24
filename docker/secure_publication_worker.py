#!/usr/bin/env python3
"""Root-only external-stream publication and independently supervised recovery.

The first live publication is an operator-run maintenance operation. It
restarts only the fixed Docker Xray (trial and final), reloads external nginx,
and checks all previously reachable routers. No router config is changed.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

import xray_lifecycle as life
from secure_ingress_inventory import reviewed_inventory, nginx_digest, live_tcp_ports
from secure_nginx_preflight import candidate_digest, MANAGED
from secure_profile_store import STORE, connection, load_artifact
from secure_publication import build_publication, encode
from secure_publication_commit import binding_for, finalize_publication
from secure_publication_transaction import PublicationTransaction, atomic, read_regular, sha, sync_directory
from secure_certificate_inventory import inspect_pair, MOUNTED_TLS, CONTAINER_TLS
from secure_live_probe import build_trial
from transport_profiles import ProfileError, REALITY_PROFILES

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"tools"))
from rehearse_secure_reverse import Proof, proof_matches, reserve, start_core, check_reality_target

STATE = STORE / "publication"
INVENTORY = STORE / "ingress.json"
CONFIG = Path("/etc/xray/owrt-remote.json")
HOLD_BYTES = b"secure publication v1\n"
TIMER = "router-orchestrator-publication-recovery.timer"
TERMINAL = {"ACCEPTED","ROLLED_BACK"}
ADMIN_CODES = {200,301,302,303,307,308,401,403}
DIGEST = re.compile(r"[0-9a-f]{64}")


class MaintenanceBusy(ProfileError):
    pass


def require(ok,message):
    if not ok:
        raise ProfileError(message)


def run(*args,timeout=30):
    result=subprocess.run(args,capture_output=True,timeout=timeout)
    require(result.returncode==0,"fixed host operation failed: "+args[0])
    return result.stdout


def docker(*args,timeout=45):
    return run("/usr/bin/docker",*args,timeout=timeout)


@contextlib.contextmanager
def exclusive(path):
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        info=os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1
                and not info.st_mode & 0o022,"unsafe publication lock")
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise MaintenanceBusy("another maintenance transaction is running") from None
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def locks():
    info=STORE.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o077,
            "private publication store missing or unsafe")
    with exclusive(life.LOCK),exclusive(STORE/"ingress.lock"):
        yield


def identity(*, running=True):
    item=json.loads(docker("inspect",life.CONTAINER))[0]
    require(item["HostConfig"]["NetworkMode"]=="host","unexpected Xray network")
    require(not running or item["State"]["Running"],"Xray is not running")
    mounts={m["Destination"]:m for m in item["Mounts"]}
    for source,target in ((CONFIG.parent,"/etc/xray"),(MOUNTED_TLS,str(CONTAINER_TLS))):
        m=mounts.get(target,{})
        require(m.get("Type")=="bind" and m.get("Source")==str(source)
                and m.get("RW") is False,"required Xray directory mount missing: "+target)
    require(item["Config"]["Cmd"]==["run","-config",str(CONFIG)],"unexpected Xray command")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}",item["Image"]) is not None,
            "invalid deployed image digest")
    return {"id":item["Id"],"image":item["Image"]}


def check_identity(context,running=True):
    require(identity(running=running)==context["identity"],"Xray container or image changed")


def db_path():
    path,_=life.online_router_baseline(life.ENV.read_bytes())
    return path.resolve(strict=True)


def fleet_rows(path):
    with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=5) as db:
        return {row[0]:{"seen":int(row[1] or 0),"admin":int(row[2] or 0),"ssh":int(row[3] or 0)}
                for row in db.execute("SELECT id,last_seen,entry_port,ssh_entry_port FROM routers WHERE deleted_at=0")}


def router_ready(row):
    if not 1<=row["admin"]<=65535 or not 1<=row["ssh"]<=65535:
        return False
    try:
        with socket.create_connection(("127.0.0.1",row["ssh"]),timeout=2) as sock:
            sock.settimeout(2)
            if not sock.recv(32).startswith(b"SSH-"):
                return False
        conn=http.client.HTTPConnection("127.0.0.1",row["admin"],timeout=3)
        try:
            conn.request("GET","/")
            return conn.getresponse().status in ADMIN_CODES
        finally:
            conn.close()
    except (OSError,http.client.HTTPException):
        return False


def hub_ready():
    conn=http.client.HTTPConnection("127.0.0.1",18089,timeout=3)
    try:
        conn.request("GET","/health")
        response=conn.getresponse()
        return response.status==200 and json.loads(response.read(4096)).get("ok") is True
    except (OSError,http.client.HTTPException,ValueError):
        return False
    finally:
        conn.close()


def capture_fleet():
    path=db_path()
    rows=fleet_rows(path)
    cutoff=int(time.time())-180
    baseline={k:{"admin":v["admin"],"ssh":v["ssh"]}
              for k,v in rows.items() if v["seen"]>=cutoff}
    require(baseline and hub_ready() and life.active(),"healthy baseline unavailable")
    require(all(router_ready(row) for row in baseline.values()),
            "at least one recently online router has no working reverse access")
    return str(path),baseline


def wait_fleet(context,since=None,timeout=150):
    path=db_path()
    require(str(path)==context["database"],"HUB database changed")
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        rows=fleet_rows(path)
        cutoff=since if since is not None else int(time.time())-180
        if (hub_ready() and life.active() and all(
            rid in rows and rows[rid]["seen"]>=cutoff
            and rows[rid]["admin"]==row["admin"] and rows[rid]["ssh"]==row["ssh"]
            and router_ready(row) for rid,row in context["baseline"].items())):
            return True
        time.sleep(2)
    return False


def resolved(name,port):
    addresses={ipaddress.ip_address(row[4][0].split("%")[0])
               for row in socket.getaddrinfo(name,port,type=socket.SOCK_STREAM)}
    require(addresses,"DNS resolution returned no addresses")
    return addresses


def check_profile(artifact):
    profile=artifact["profile"]
    if profile["transport"] in REALITY_PROFILES:
        # First external-stream implementation is deliberately limited to a
        # verified TLS 1.3/H2 target at 443, with no recursion into this host.
        require(profile["target_port"]==443,"initial REALITY publication requires target port 443")
        target=resolved(profile["target_host"],443)
        dial=resolved(artifact["remote_host"],artifact["public_port"])
        interfaces=json.loads(run("/usr/sbin/ip","-j","address","show",timeout=5))
        local={ipaddress.ip_address(a["local"]) for dev in interfaces for a in dev.get("addr_info",[])}
        require(not target.intersection(dial|local) and all(a.is_global for a in target),
                "REALITY target may resolve to this ingress or a non-public address")
        for sni in profile["server_names"]:
            check_reality_target(profile["target_host"],sni)
    else:
        cert=MOUNTED_TLS/Path(profile["certificate_file"]).relative_to(CONTAINER_TLS)
        key=MOUNTED_TLS/Path(profile["key_file"]).relative_to(CONTAINER_TLS)
        inspect_pair(profile["server_name"],cert,key,roots=[MOUNTED_TLS])


def validate_config(data,context):
    check_identity(context)
    fd,name=tempfile.mkstemp(prefix=".ro-publication-",suffix=".json",dir=CONFIG.parent)
    try:
        with os.fdopen(fd,"wb") as stream:
            os.fchmod(stream.fileno(),0o600)
            stream.write(data)
        docker("run","--rm","--pull","never","--network","none","--read-only",
               "--cap-drop","ALL","--security-opt","no-new-privileges:true",
               "--memory","256m","--pids-limit","64",
               "-v",str(CONFIG.parent)+":/etc/xray:ro",
               "-v","/usr/local/share/xray:/usr/local/share/xray:ro",
               "-v",str(MOUNTED_TLS)+":"+str(CONTAINER_TLS)+":ro",
               context["identity"]["image"],"run","-test","-config",name)
    finally:
        Path(name).unlink(missing_ok=True)


def activate(context):
    check_identity(context,running=False)
    restoring=sha(MANAGED.read_bytes())==context["original_map_sha"]
    expected=context["original_nginx_digest"] if restoring else context["candidate_nginx_digest"]
    if not restoring:
        require(nginx_digest("external-stream")==expected,"external nginx changed before activation")
    require(life.run("systemctl","is-active","--quiet",life.SERVICE).returncode!=0,
            "legacy Xray service is active")
    since=int(time.time())
    docker("restart","--time","5",life.CONTAINER,timeout=55)
    require(life.wait_healthy(),"Xray legacy listeners did not recover")
    check_identity(context)
    # Even if unrelated nginx files were edited, restore the old Xray first
    # and retain the hold rather than reloading unreviewed nginx files.
    require(nginx_digest("external-stream")==expected,"external nginx changed during activation")
    run("/usr/bin/systemctl","reload","nginx",timeout=30)
    require(wait_fleet(context,since),"existing reverse access did not fully recover")


def verify_final(context):
    check_identity(context)
    ports=live_tcp_ports()
    return (context["backend_port"] in ports and not set(context["probe_ports"]).intersection(ports)
            and wait_fleet(context))


def cleanup_probe(context):
    label=context["probe_label"]
    require(re.fullmatch(r"[0-9a-f]{16}",label) is not None,"invalid probe ownership label")
    name="ro-probe-"+label+"-client"
    found=docker("ps","-aq","--filter","label=ro.probe="+label).decode().split()
    for container in found:
        actual=docker("inspect","--format","{{.Name}}|{{index .Config.Labels \"ro.probe\"}}",container).decode().strip()
        require(actual=="/"+name+"|"+label,"unexpected container in publication probe scope")
        docker("rm","-f",container,timeout=20)
    require(not docker("ps","-aq","--filter","label=ro.probe="+label).strip(),
            "probe cleanup not verified")
    (STATE/"probe-client.json").unlink(missing_ok=True)


def finalize(context,record):
    with connection() as db:
        return finalize_publication(record,db=db,inventory_path=INVENTORY,
            xray_path=CONFIG,nginx_path=MANAGED,nginx_digest=nginx_digest,
            verify_runtime=lambda:verify_final(context))


def tx():
    return PublicationTransaction(STATE,CONFIG,MANAGED)


def read_context():
    raw,mode,uid,_=read_regular(STATE/"context.json")
    require(uid==0 and not mode&0o077,"unsafe publication context")
    return json.loads(raw)


def release_hold():
    require(read_regular(life.TRANSPORT_HOLD)[0]==HOLD_BYTES,"publication hold changed")
    life.TRANSPORT_HOLD.unlink()
    sync_directory(life.TRANSPORT_HOLD.parent)


def recover_locked():
    if not life.TRANSPORT_HOLD.exists():
        if (STATE/"pending.json").exists():
            require(tx()._record()["phase"] in TERMINAL,"unfinished publication has no hold; operator recovery required")
        return "NONE"
    if read_regular(life.TRANSPORT_HOLD)[0]!=HOLD_BYTES:
        return "OTHER_MAINTENANCE"
    context=read_context()
    # An interruption before the journal was written cannot have touched
    # either live file. Otherwise the journal owns all permitted hashes.
    if not tx().journal.exists():
        require(sha(CONFIG.read_bytes())==context["original_xray_sha"]
                and sha(MANAGED.read_bytes())==context["original_map_sha"],
                "unrecorded publication files changed")
        cleanup_probe(context)
        release_hold()
        return "PREPARATION_ABORTED"
    result=tx().recover(activate=lambda:activate(context),
        verify_baseline=lambda:wait_fleet(context),finalize=lambda record:finalize(context,record))
    cleanup_probe(context)
    release_hold()
    return result["phase"]


def archive_previous():
    if not STATE.exists():
        return
    require(STATE.is_dir() and not STATE.is_symlink(),"unsafe publication state")
    if tx().journal.exists():
        require(tx()._record()["phase"] in TERMINAL,"recover unfinished publication first")
    # Keep all previous backups and journals, never prune automatically.
    destination=STORE/("publication-archive-"+secrets.token_hex(8))
    STATE.rename(destination)
    sync_directory(STORE)


def publish(profile_id,revision,expected):
    require(Path(__file__).resolve()==(life.ROOT/"src/docker/secure_publication_worker.py").resolve(),
            "publication requires the installed production revision")
    run("/usr/bin/systemctl","is-active","--quiet",TIMER,timeout=5)
    require(not life.TRANSPORT_HOLD.exists(),"another maintenance transaction is active")
    if life.JOURNAL.exists():
        require(json.loads(life.JOURNAL.read_text()).get("phase") in ("committed","rolled-back"),
                "unfinished Xray core lifecycle journal")
    archive_previous()
    inventory=reviewed_inventory()
    require(inventory["mode"]=="external-stream","only reviewed external stream is enabled")
    with connection() as db:
        artifact,digest,published=load_artifact(db,profile_id,revision)
    require(not published and secrets.compare_digest(digest,expected),"draft changed or already published")
    check_profile(artifact)
    current=identity()
    database,baseline=capture_fleet()
    original=CONFIG.read_bytes()
    fragment=MANAGED.read_bytes()
    planning=dict(inventory,occupied_ports=sorted(set(inventory["occupied_ports"])|live_tcp_ports()))
    plan=build_publication(json.loads(original),artifact,planning,profile_id=profile_id,revision=revision)
    candidate=encode(plan["candidate"])
    candidate_map=plan["fragment"].encode()
    stamp=candidate_digest(candidate_map,expected_live_sha=inventory["nginx_config_sha256"],
                           expected_fragment_sha=sha(fragment),nginx_digest=nginx_digest)
    binding=binding_for(plan,inventory,profile_id=profile_id,revision=revision,expected_nginx_digest=stamp)
    version=docker("run","--rm","--pull","never","--network","none",current["image"],"version").decode().splitlines()[0]
    STATE.mkdir(mode=0o700)
    sync_directory(STORE)
    label=secrets.token_hex(8)
    with contextlib.ExitStack() as stack:
        sockets=[reserve() for _ in range(4)]
        for sock in sockets:
            stack.callback(sock.close)
        ports=[sock.getsockname()[1] for sock in sockets]
        proof1,proof2=Proof(sockets[2]),Proof(sockets[3])
        stack.callback(proof1.close)
        stack.callback(proof2.close)
        trial,client=build_trial(plan["candidate"],artifact["profile"],plan["tag"],
            public_port=artifact["public_port"],dial_host=artifact["remote_host"],
            admin_port=ports[0],ssh_port=ports[1],admin_target=ports[2],ssh_target=ports[3],
            version=version,proxy_protocol=inventory.get("proxy_protocol",False))
        context={"identity":current,"database":database,"baseline":baseline,"probe_label":label,
            "probe_ports":ports[:2],"backend_port":plan["backend_port"],
            "original_xray_sha":sha(original),"original_map_sha":sha(fragment),
            "original_nginx_digest":inventory["nginx_config_sha256"],"candidate_nginx_digest":stamp}
        atomic(STATE/"context.json",encode(context))
        require(not life.TRANSPORT_HOLD.exists() and not life.TRANSPORT_HOLD.is_symlink(),
                "maintenance hold appeared during preflight")
        # All internal writers own the shared lifecycle lock. Atomic
        # replacement prevents a crash leaving a zero-byte marker.
        atomic(life.TRANSPORT_HOLD,HOLD_BYTES)

        def validate(data,nginx):
            require(nginx==candidate_map and reviewed_inventory()==inventory,
                    "reviewed nginx inventory changed")
            validate_config(data,context)
            require(plan["backend_port"] not in live_tcp_ports(),"backend port occupied")

        def trial_accept():
            profile=artifact["profile"]
            names=profile.get("server_names",[profile["server_name"]])
            for name in names:
                config=copy.deepcopy(client)
                for link in config["outbounds"]:
                    if link["protocol"]!="vless":
                        continue
                    stream=link["streamSettings"]
                    if "realitySettings" in stream:
                        stream["realitySettings"]["serverName"]=name
                    else:
                        stream["tlsSettings"]["serverName"]=name
                    if "wsSettings" in stream:
                        stream["wsSettings"]["host"]=name
                proof1.token=secrets.token_bytes(32)
                proof2.token=secrets.token_bytes(32)
                atomic(STATE/"probe-client.json",encode(config))
                try:
                    with contextlib.ExitStack() as probe_stack:
                        start_core(current["image"],"ro-probe-"+label+"-client",label,
                                   STATE/"probe-client.json",probe_stack)
                        deadline=time.monotonic()+45
                        while time.monotonic()<deadline:
                            if proof_matches(ports[0],proof1) and proof_matches(ports[1],proof2):
                                break
                            time.sleep(1)
                        else:
                            return False
                finally:
                    cleanup_probe(context)
            return True

        sockets[0].close()
        sockets[1].close()
        try:
            result=tx().apply(candidate_xray=candidate,candidate_nginx=candidate_map,
                expected_xray_sha=sha(original),expected_nginx_sha=sha(fragment),
                validate=validate,activate=lambda:activate(context),
                trial_xray=encode(trial),trial_accept=trial_accept,
                accept=lambda:verify_final(context),verify_baseline=lambda:wait_fleet(context),
                binding=binding,finalize=lambda record:finalize(context,record))
        except Exception:
            # Do not clear a nonterminal hold. The independent timer retries
            # recovery after this worker releases its shared locks.
            if tx().journal.exists() and tx()._record()["phase"]=="ROLLED_BACK":
                cleanup_probe(context)
                release_hold()
            raise
        cleanup_probe(context)
        release_hold()
        return result["phase"]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("publish","recover","status"))
    parser.add_argument("--profile-id")
    parser.add_argument("--revision",type=int)
    parser.add_argument("--artifact-sha256")
    parser.add_argument("--confirm-maintenance",action="store_true")
    args=parser.parse_args()
    require(os.geteuid()==0,"root required")
    if args.action=="recover":
        try:
            with locks():
                print("PUBLICATION RECOVERY:",recover_locked())
        except MaintenanceBusy:
            print("PUBLICATION RECOVERY: active maintenance, skipped")
        return
    with locks():
        if args.action=="status":
            print("PUBLICATION:",tx()._record()["phase"] if (STATE/"pending.json").exists() else "NONE")
        else:
            require(args.confirm_maintenance and args.profile_id and args.revision
                    and DIGEST.fullmatch(args.artifact_sha256 or ""),
                    "explicit maintenance confirmation and sealed revision digest required")
            print("PUBLICATION:",publish(args.profile_id,args.revision,args.artifact_sha256))


if __name__=="__main__":
    try:
        main()
    except ProfileError as exc:
        print("PUBLICATION BLOCKED:",str(exc),file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print("PUBLICATION ERROR:",type(exc).__name__,file=sys.stderr)
        sys.exit(1)
