#!/usr/bin/env python3
"""Run isolated dual-reverse trials with the exact current Belt Xray image.

Temporary loopback listeners, distinct test UUIDs and a private nginx
process. Never reloads production nginx or restarts the production Xray.
TLS uses an existing trusted certificate; REALITY requires an explicit
operator-selected target. No publication or router assignment is performed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "docker"))
from secure_certificate_inventory import mounted_certificate, MOUNTED_TLS
from secure_certificate_import import import_certificate
from secure_ingress import domain
from secure_profile_store import reality_keys
from secure_reverse_probe import build_probe
from transport_profiles import ProfileError, SECURE_PROFILES, REALITY_PROFILES

DOCKER = "/usr/bin/docker"
NGINX = "/usr/sbin/nginx"
CONTAINER = "router-orchestrator-xray"


def run(*args, timeout=40):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise ProfileError("command unavailable or timed out: " + args[0]) from None
    if result.returncode:
        # Do not print raw Xray config errors: they may contain key material.
        raise ProfileError("command failed: " + " ".join(args[:2]))
    return result.stdout.strip()


def image_identity():
    result = run(DOCKER,"inspect","--format","{{.Id}}|{{.Image}}|{{.State.Running}}",CONTAINER)
    parts=result.split("|")
    if len(parts)!=3 or parts[2]!="true" or not parts[1].startswith("sha256:"):
        raise ProfileError("production Xray image identity unavailable")
    return parts[0],parts[1]


def reserve():
    sock=socket.socket()
    sock.bind(("127.0.0.1",0))
    return sock


class Proof:
    def __init__(self,sock):
        self.sock=sock
        self.port=sock.getsockname()[1]
        self.token=secrets.token_bytes(32)
        self.stop=threading.Event()
        self.sock.listen(8)
        self.sock.settimeout(.3)
        self.thread=threading.Thread(target=self.serve,daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection,_=self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(1)
                    connection.sendall(self.token)
                except OSError:
                    pass

    def close(self):
        self.stop.set()
        self.sock.close()
        self.thread.join(timeout=2)


def proof_matches(port,proof):
    try:
        with socket.create_connection(("127.0.0.1",port),timeout=1) as conn:
            conn.settimeout(1)
            data=b""
            while len(data)<len(proof.token):
                block=conn.recv(len(proof.token)-len(data))
                if not block:
                    return False
                data+=block
            return secrets.compare_digest(data,proof.token)
    except OSError:
        return False


def cleanup_container(name,label):
    try:
        info=run(DOCKER,"inspect","--format","{{index .Config.Labels \"ro.probe\"}}",name,timeout=5)
        if info==label:
            run(DOCKER,"rm","-f",name,timeout=15)
    except ProfileError:
        # Auto-remove covers normal process exit. Report uncertain cleanup
        # rather than deleting a container whose ownership is not verified.
        check=subprocess.run([DOCKER,"inspect",name],capture_output=True,timeout=5)
        if check.returncode==0:
            print("CLEANUP WARNING:",name,file=sys.stderr)


def start_core(image,name,label,config,stack):
    stack.callback(cleanup_container,name,label)
    run(DOCKER,"run","--detach","--rm","--pull","never","--name",name,
        "--label","ro.probe="+label,"--network","host","--read-only",
        "--cap-drop","ALL","--security-opt","no-new-privileges:true",
        "--memory","192m","--pids-limit","64",
        "--mount","type=bind,src="+str(config)+",dst=/probe/config.json,readonly",
        "--mount","type=bind,src="+str(MOUNTED_TLS)+",dst=/etc/router-orchestrator/tls,readonly",
        image,"run","-config","/probe/config.json")


def stop_nginx(process):
    if process.poll() is None:
        os.killpg(process.pid,signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL)
            process.wait(timeout=5)


def rehearsal(profile,image,version,proxy):
    label=secrets.token_hex(8)
    with contextlib.ExitStack() as stack:
        directory=Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="ro-reverse-")))
        reservations=[reserve() for _ in range(6)]
        for sock in reservations:
            stack.callback(sock.close)
        backend,ingress,admin,ssh,_,_=[s.getsockname()[1] for s in reservations]
        admin_proof=Proof(reservations[4])
        ssh_proof=Proof(reservations[5])
        stack.callback(admin_proof.close)
        stack.callback(ssh_proof.close)
        profile=dict(profile,port=backend)
        server,client=build_probe(profile,ingress_port=ingress,admin_port=admin,ssh_port=ssh,
                                  admin_target=admin_proof.port,ssh_target=ssh_proof.port,
                                  core_version=version,proxy_protocol=proxy)
        for name,value in (("server",server),("client",client)):
            config=directory/(name+".json")
            config.write_text(json.dumps(value))
            config.chmod(0o600)
            run(DOCKER,"run","--rm","--pull","never","--network","none","--read-only",
                "--cap-drop","ALL","--security-opt","no-new-privileges:true",
                "--mount","type=bind,src="+str(config)+",dst=/probe/config.json,readonly",
                "--mount","type=bind,src="+str(MOUNTED_TLS)+",dst=/etc/router-orchestrator/tls,readonly",
                image,"run","-test","-config","/probe/config.json")
        names=profile.get("server_names",[profile["server_name"]])
        entries=" ".join(domain(n)+" 127.0.0.1:"+str(backend)+";" for n in names)
        modules="include /etc/nginx/modules-enabled/*.conf;" if Path("/etc/nginx/modules-enabled").is_dir() else ""
        config=directory/"nginx.conf"
        config.write_text(modules+"\nworker_processes 1;\nerror_log stderr;\n"
            +"pid "+str(directory/"nginx.pid")+";\nevents { worker_connections 64; }\n"
            +"stream { map $ssl_preread_server_name $backend { default \"\"; "+entries+" }\n"
            +"server { listen 127.0.0.1:"+str(ingress)+"; ssl_preread on; "
            +"proxy_protocol "+("on" if proxy else "off")+"; proxy_pass $backend; } }\n")
        # Retain the installed nginx prefix for relative load_module paths.
        # All runtime files and listen sockets are nevertheless unique.
        run(NGINX,"-e","stderr","-t","-c",str(config))
        for sock in reservations[:4]:
            sock.close()
        start_core(image,"ro-probe-"+label+"-server",label,directory/"server.json",stack)
        process=subprocess.Popen([NGINX,"-e","stderr","-c",str(config),"-g","daemon off;"],
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        stack.callback(stop_nginx,process)
        start_core(image,"ro-probe-"+label+"-client",label,directory/"client.json",stack)
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise ProfileError("isolated nginx exited")
            if proof_matches(admin,admin_proof) and proof_matches(ssh,ssh_proof):
                return
            time.sleep(1)
        raise ProfileError("both authenticated reverse nonce proofs not received")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sni",required=True,help="existing TLS certificate hostname or REALITY serverName")
    parser.add_argument("--transport",action="append",choices=SECURE_PROFILES)
    parser.add_argument("--target",help="explicit REALITY target hostname")
    parser.add_argument("--no-proxy-protocol",action="store_true")
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise ProfileError("root required")
    name=domain(args.sni)
    available=next((int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                    if line.startswith("MemAvailable:")),0)
    if available < 512*1024:
        raise ProfileError("less than 512 MiB available RAM; isolated dual-core trial refused")
    modes=args.transport or ["tcp-tls","wss","xhttp","grpc"]
    if any(t in REALITY_PROFILES for t in modes) and not args.target:
        raise ProfileError("REALITY rehearsal requires an explicit target")
    before,image=image_identity()
    version=run(DOCKER,"run","--rm","--pull","never","--network","none",image,"version").splitlines()[0]
    print("EXACT CORE:",version,flush=True)
    pair=None
    if any(t not in REALITY_PROFILES for t in modes):
        try:
            pair=mounted_certificate(name)
        except ProfileError:
            imported=import_certificate(name)
            pair={k:imported[k] for k in ("certificate_file","key_file")}
    failed=[]
    for mode in modes:
        profile={"name":"trial","transport":mode,"server_name":name,"path":"/ro-probe/","service_name":"ro_probe"}
        if mode in REALITY_PROFILES:
            profile.update(server_names=[name],target_host=domain(args.target),target_port=443)
            reality_keys(profile)
        else:
            profile.update(pair)
        try:
            rehearsal(profile,image,version,not args.no_proxy_protocol)
            print(mode+": DUAL REVERSE PASS",flush=True)
        except (ProfileError,OSError,subprocess.SubprocessError) as exc:
            failed.append(mode)
            print(mode+": FAIL ("+str(exc)+")",flush=True)
    after,after_image=image_identity()
    if (before,image)!=(after,after_image):
        raise ProfileError("production Xray identity changed during trial")
    print("PRODUCTION XRAY IDENTITY: UNCHANGED")
    print("PRODUCTION CONFIGURATION AND ROUTERS: NOT MODIFIED")
    if failed:
        raise ProfileError("failed profiles: "+", ".join(failed))


if __name__=="__main__":
    def interrupted(signum,frame):
        raise SystemExit("REHEARSAL INTERRUPTED; cleaning up temporary processes")
    signal.signal(signal.SIGTERM,interrupted)
    signal.signal(signal.SIGHUP,interrupted)
    try:
        main()
    except (ProfileError,OSError,subprocess.SubprocessError) as exc:
        raise SystemExit("REHEARSAL BLOCKED: "+str(exc))
