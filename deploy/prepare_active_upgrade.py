#!/usr/bin/env python3
"""Stage an upgrade of an already-active unified Belt installation.

No service restart, live source checkout, env change or unit installation.
Run from a clean, detached candidate worktree. Private artifacts are retained.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

ROOT=Path("/opt/router-orchestrator")
SRC=ROOT/"src"
REPO=Path(__file__).resolve().parents[1]
ENV=ROOT/".env"
COMPOSE=ROOT/"docker-compose.yml"
STORE=Path("/var/lib/router-orchestrator-secure-profiles")
UNITS=Path("/etc/systemd/system")
SERVICES=("hub","xray","nginx")


def require(ok,message):
    if not ok:
        raise RuntimeError(message)


def run(*args,timeout=90):
    result=subprocess.run(args,capture_output=True,timeout=timeout)
    require(result.returncode==0,"operation failed: "+" ".join(args[:2]))
    return result.stdout


def digest(data):
    return hashlib.sha256(data).hexdigest()


def head(path):
    return run("git","-C",str(path),"rev-parse","HEAD").decode().strip()


def parse_env(data):
    values={}
    for line in data.decode().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key,sep,value=line.partition("=")
        require(sep and re.fullmatch(r"[A-Z][A-Z0-9_]*",key) and key not in values,
                "unsupported or duplicate environment entry")
        values[key]=value
    return values


def replace_hub(data,tag):
    settings=parse_env(data)
    require("RO_HUB_IMAGE" in settings,"missing HUB image")
    lines=data.decode().splitlines(keepends=True)
    return "".join("RO_HUB_IMAGE="+tag+"\n" if line.startswith("RO_HUB_IMAGE=") else line
                   for line in lines).encode()


def snapshot_db(source,destination):
    with sqlite3.connect(source.resolve().as_uri()+"?mode=ro",uri=True,timeout=10) as db, \
         sqlite3.connect(destination) as target:
        db.backup(target)
        require(target.execute("PRAGMA quick_check").fetchone()[0]=="ok","SQLite backup failed")
    os.chmod(destination,0o600)


def containers():
    result={}
    for service in SERVICES:
        item=json.loads(run("docker","inspect","router-orchestrator-"+service))[0]
        require(item["State"]["Running"],"container not running: "+service)
        result[service]={"id":item["Id"],"image":item["Image"],"tag":item["Config"]["Image"],
                         "mounts":[{"source":m["Source"],"target":m["Destination"],"rw":m["RW"]}
                                   for m in item["Mounts"]]}
    return result


def private_write(path,data):
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def main():
    os.umask(0o077)
    require(os.geteuid()==0,"root required")
    require(REPO!=SRC.resolve() and REPO.is_relative_to(ROOT/"server-candidates"),
            "use a separate server-candidates checkout")
    require(COMPOSE.is_symlink() and COMPOSE.resolve()==SRC/"deploy/docker-compose.yml",
            "unexpected active Compose layout")
    for path in (REPO,SRC):
        require(not run("git","-C",str(path),"status","--porcelain","--untracked-files=all").strip(),
                "tracked source has edits")
    require(not (ROOT/"data/.xray-maintenance-hold").exists(),"maintenance already active")
    before=containers()
    old_commit=head(SRC)
    new_commit=head(REPO)
    require(old_commit!=new_commit,"candidate is already installed")
    run("git","-C",str(REPO),"merge-base","--is-ancestor",old_commit,new_commit)
    old_env=ENV.read_bytes()
    settings=parse_env(old_env)
    require(settings.get("RO_HUB_IMAGE")==before["hub"]["tag"] and
            settings.get("RO_XRAY_IMAGE")==before["xray"]["tag"],"active image and env differ")
    require(settings.get("RO_TLS_DIR")==str(ROOT/"tls"),"unexpected dedicated TLS directory")
    require((ROOT/"tls").is_dir() and not (ROOT/"tls").is_symlink(),"TLS directory absent")
    require(Path(settings["RO_NGINX_CONF"])==SRC/"deploy/nginx/embedded.conf",
            "unexpected project nginx mode")
    tag="router-orchestrator-hub:unified-"+new_commit[:12]
    require(tag != before["hub"]["tag"],"candidate tag is currently running")
    stage=Path(tempfile.mkdtemp(prefix="active-upgrade-"+new_commit[:12]+"-",dir=ROOT))
    print("PRIVATE CANDIDATE:",stage,flush=True)
    private_write(stage/"previous.env",old_env)
    private_write(stage/"candidate.env",replace_hub(old_env,tag))
    private_write(stage/"previous-compose.yml",COMPOSE.read_bytes())
    private_write(stage/"candidate-compose.yml",(REPO/"deploy/docker-compose.yml").read_bytes())
    private_write(stage/"previous-xray.json",Path("/etc/xray/owrt-remote.json").read_bytes())
    snapshot_db(Path(settings["RO_STATE_DIR"])/"hub.db",stage/"hub.db")
    if (STORE/"profiles.db").exists():
        snapshot_db(STORE/"profiles.db",stage/"profiles.db")
    if (STORE/"ingress.json").exists():
        private_write(stage/"ingress.json",(STORE/"ingress.json").read_bytes())
    unit_dir=stage/"units"
    unit_dir.mkdir(mode=0o700)
    for path in sorted((REPO/"deploy/systemd").glob("router-orchestrator-*.service"))+sorted((REPO/"deploy/systemd").glob("router-orchestrator-*.timer")):
        previous=UNITS/path.name
        if previous.exists():
            require(previous.is_file() and not previous.is_symlink(),"unexpected unit path")
            private_write(unit_dir/path.name,previous.read_bytes())
    print("===== BUILD CANDIDATE HUB =====",flush=True)
    run("docker","build","-f",str(REPO/"docker/hub/Dockerfile"),
        "--build-arg","HUB_SOURCE_SHA256="+digest((REPO/"vps/owrt-remote-hub.py").read_bytes()),
        "-t",tag,str(REPO),timeout=600)
    image=run("docker","image","inspect","--format","{{.Id}}",tag).decode().strip()
    run("docker","compose","--project-directory",str(ROOT),"--env-file",str(stage/"candidate.env"),
        "-f",str(stage/"candidate-compose.yml"),"config","--quiet")
    require(containers()==before and head(SRC)==old_commit and ENV.read_bytes()==old_env
            and Path("/etc/xray/owrt-remote.json").read_bytes()==(stage/"previous-xray.json").read_bytes(),
            "production changed during staging; candidate not accepted")
    manifest={"phase":"STAGED_ONLY","old_commit":old_commit,"new_commit":new_commit,
              "hub_image":tag,"hub_image_id":image,"containers":before,
              "old_env_sha256":digest(old_env),"xray_sha256":digest((stage/"previous-xray.json").read_bytes()),
              "requires_xray_recreate":not any(m["target"]=="/etc/router-orchestrator/tls" and
                    m["source"]==str(ROOT/"tls") and not m["rw"] for m in before["xray"]["mounts"])}
    private_write(stage/"manifest.json",json.dumps(manifest,indent=2).encode())
    print("HUB IMAGE:",tag)
    print("XRAY RECREATE FOR TLS MOUNT:",manifest["requires_xray_recreate"])
    print("ACTIVE UPGRADE CANDIDATE: STAGED")
    print("LIVE SERVICES AND SOURCE: UNCHANGED")


if __name__=="__main__":
    main()
