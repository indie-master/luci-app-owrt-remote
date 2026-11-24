#!/usr/bin/env python3
"""Guarded existing-stack update with retained source and manual crash recovery.

Run from the exact immutable candidate checkout. No profile publication is
enabled by this upgrade. On failure restore old source, env and units, never
overwrite the live HUB database with a potentially stale backup.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from prepare_active_upgrade import (ROOT,SRC,REPO,ENV,COMPOSE,STORE,UNITS,
    require,run,head,digest,parse_env,replace_hub,snapshot_db,containers,private_write,hold_snapshot)
sys.path.insert(0,str(REPO/"docker"))
import xray_lifecycle as life
from secure_publication_transaction import atomic,read_regular,sync_directory
from secure_publication_worker import exclusive,capture_fleet,wait_fleet,identity
from secure_ingress_inventory import nginx_digest
import active_upgrade_fleet as fleet

ADAPTER="router-orchestrator-host-adapter.service"
TIMERS=("router-orchestrator-xray-watchdog.timer","router-orchestrator-transport-expiry.timer",
        "router-orchestrator-publication-recovery.timer")
UNIT_NAMES=(ADAPTER,"router-orchestrator-core-job@.service",
    "router-orchestrator-xray-watchdog.service",TIMERS[0],
    "router-orchestrator-transport-expiry.service",TIMERS[1],
    "router-orchestrator-transport-stage@.service",
    "router-orchestrator-transport-rollback.service",
    "router-orchestrator-publication-recovery.service",TIMERS[2],
    "router-orchestrator-publication-job.service")
HOLD=b"active stack upgrade v1\n"
LEGACY_HOLD=Path("/var/lib/owrt-remote/.xray-maintenance-hold")
LEGACY_SHA="1eeebf5600bf8b8abe5aa45fa66aed90376882fb4c400e27cea75a4b053112c5"
INSTALLER_ONLY={"deploy/apply_active_upgrade.py","deploy/active_upgrade_fleet.py",
                "tests/test_active_upgrade_apply.py","tests/test_active_upgrade_fleet.py",
                "docs/SECURE_TRANSPORT_AND_CORE_POLICY.md"}
XRAY=Path("/etc/xray/owrt-remote.json")
NGINX=Path("/etc/nginx/router-orchestrator-managed.map")
EXTERNAL_NGINX=Path("/etc/nginx/nginx.conf")
ENABLE=STORE/"web-publication.enabled"
TERMINAL={"ACCEPTED","ROLLED_BACK"}


def checked_stage(value):
    path=Path(value)
    require(path.is_absolute() and path.parent==ROOT and path.name.startswith("active-upgrade-"),
            "unexpected stage path")
    info=path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and not info.st_mode&0o077,
            "unsafe stage directory")
    return path


def read_json(path):
    data,mode,uid,_=read_regular(path)
    require(uid==0 and not mode&0o077,"unsafe private manifest")
    return json.loads(data)


def service_state(name):
    return run("systemctl","show","-p","ActiveState","--value",name,timeout=10).decode().strip()


def compose(*args):
    return run("docker","compose","--project-directory",str(ROOT),
        "--env-file",str(ENV),"-f",str(COMPOSE),*args,timeout=180)


def clean_source():
    require(not run("git","-C",str(SRC),"status","--porcelain","--untracked-files=all").strip(),
            "active source not clean")


def check_installer_revision(commit):
    """Allow reuse of a built image only for installer/test/docs-only fixes."""
    require(re.fullmatch(r"[0-9a-f]{40}",commit) is not None,"invalid staged commit")
    if head(REPO)==commit:
        return
    run("git","-C",str(REPO),"merge-base","--is-ancestor",commit,head(REPO))
    changes=set(run("git","-C",str(REPO),"diff","--name-only","--no-renames",commit,head(REPO)).decode().splitlines())
    require(changes <= INSTALLER_ONLY,"runtime source differs from staged image")


def legacy_hold():
    current=hold_snapshot(life.TRANSPORT_HOLD)
    original=hold_snapshot(LEGACY_HOLD)
    require(current is not None and original is not None,"legacy hold pair missing")
    require(current[2:]==(23088,23088,0o600,1,LEGACY_SHA)
            and original[2:]==(0,0,0o600,1,LEGACY_SHA),
            "legacy hold differs from audited Belt marker")
    for path in (life.JOURNAL,ROOT/"data/transport-stage/stage.json",STORE/"publication/pending.json"):
        require(not path.exists() and not path.is_symlink(),"unexpected maintenance journal: "+path.name)
    return list(current)


def check_upgrade_hold(record):
    if record.get("legacy_hold") is not None:
        require(legacy_hold()==record["legacy_hold"],"legacy hold changed")
    else:
        require(read_regular(life.TRANSPORT_HOLD)[0]==HOLD,"upgrade hold ownership changed")


def check_quiescent():
    result=run("systemctl","list-units","--all","--state=active,activating,deactivating",
        "--plain","--no-legend","router-orchestrator-core-job@*.service",
        "router-orchestrator-transport-stage@*.service",
        "router-orchestrator-transport-rollback.service",
        "router-orchestrator-publication-job.service",timeout=15)
    require(not result.strip(),"a maintenance worker is already running")
    require(not ENABLE.exists() and not ENABLE.is_symlink(),"disable Web publication before upgrade")


def write_state(stage,record,phase):
    record["phase"]=phase
    atomic(stage/"upgrade-state.json",json.dumps(record,sort_keys=True).encode())


def check_files(record):
    for path,expected in ((XRAY,record["xray_sha"]),(NGINX,record["map_sha"]),
                          (EXTERNAL_NGINX,record["nginx_sha"])):
        require(digest(path.read_bytes())==expected,"external configuration changed: "+path.name)
    require(nginx_digest("external-stream")==record["nginx_full_sha"],"external nginx include tree changed")


def unit_exists(name):
    # This deployment installs these fixed units directly in /etc/systemd/system.
    return (UNITS/name).is_file()


def stop_workers():
    for timer in TIMERS:
        if unit_exists(timer):
            run("systemctl","stop",timer,timeout=30)
    # Stop the fixed timer services as well, so old code cannot run during
    # the source checkout. The common lifecycle lock is held by the caller.
    for timer in TIMERS:
        service=timer.removesuffix(".timer")+".service"
        if unit_exists(service):
            run("systemctl","stop",service,timeout=30)
    check_quiescent()


def stop_hub():
    running=run("docker","ps","--filter","name=^/router-orchestrator-hub$","--format","{{.Names}}",timeout=10).decode().splitlines()
    if "router-orchestrator-hub" in running:
        run("docker","stop","--time","10","router-orchestrator-hub",timeout=45)
    run("systemctl","stop",ADAPTER,timeout=30)


def install_units(stage,restore=False):
    for name in UNIT_NAMES:
        path=UNITS/name
        require(not path.is_symlink(),"unexpected systemd symlink")
        if restore:
            backup=stage/"activation-units"/name
            if backup.exists():
                atomic(path,backup.read_bytes(),0o644,(0,0))
            else:
                path.unlink(missing_ok=True)
        else:
            source=SRC/"deploy/systemd"/name
            require(source.is_file(),"candidate unit absent: "+name)
            atomic(path,source.read_bytes(),0o644,(0,0))
    run("systemctl","daemon-reload",timeout=30)


def probe_adapter():
    code="""import json,socket
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
 s.settimeout(8)
 s.connect('/run/router-orchestrator/control.sock')
 s.sendall(b'{"op":"secure.catalog.job"}\\n')
 raw=s.makefile('rb').readline()
 result=json.loads(raw)
 assert result.get('ok') is True and result.get('enabled') is False
"""
    run("docker","exec","router-orchestrator-hub","python","-c",code,timeout=20)


def health():
    return run("docker","inspect","--format","{{.State.Health.Status}}",
               "router-orchestrator-hub",timeout=10).decode().strip()=="healthy"


def wait_ready(record):
    end=time.monotonic()+180
    while time.monotonic()<end:
        if health() and service_state(ADAPTER)=="active":
            break
        time.sleep(3)
    else:
        raise RuntimeError("HUB/adapter did not become healthy")
    require(life.wait_healthy(),"Xray listeners unavailable")
    baseline=record["fleet"]["baseline"]
    check=fleet.wait if baseline and all("admin_ok" in row for row in baseline.values()) else wait_fleet
    require(check(record["fleet"],record["started"],timeout=180),
            "not all baseline router channels recovered")
    check_files(record)


def restart_timers(record,restore=False):
    for timer in TIMERS:
        if restore and not record["timers"][timer]["present"]:
            # New unit was removed; disable its persistent wants link first
            # (the rollback path calls disable before removing units).
            continue
        if restore:
            if record["timers"][timer]["active"]:
                run("systemctl","start",timer,timeout=20)
        else:
            run("systemctl","enable","--now",timer,timeout=30)
            require(service_state(timer)=="active","recovery timer inactive")


def rollback(stage,record):
    require(record["phase"] not in TERMINAL,"upgrade is already terminal")
    require(head(SRC) in (record["old_commit"],record["new_commit"]),"active Git source changed")
    require(ENV.read_bytes() in ((stage/"previous.env").read_bytes(),(stage/"candidate.env").read_bytes()),
            "active env changed externally")
    check_files(record)
    check_upgrade_hold(record)
    write_state(stage,record,"RESTORING")
    stop_workers()
    stop_hub()
    # Retain new-state DB independently, but never overwrite live user data
    # automatically. Old schema compatibility must be accepted by old HUB.
    db=Path(parse_env((stage/"previous.env").read_bytes())["RO_STATE_DIR"])/"hub.db"
    snapshot_db(db,stage/("hub-after-failure-"+str(time.time_ns())+".db"))
    if not record["timers"][TIMERS[2]]["present"] and unit_exists(TIMERS[2]):
        run("systemctl","disable","--now",TIMERS[2],timeout=30)
    clean_source()
    run("git","-C",str(SRC),"checkout","--detach",record["old_commit"],timeout=60)
    atomic(ENV,(stage/"previous.env").read_bytes())
    install_units(stage,restore=True)
    run("systemctl","start",ADAPTER,timeout=30)
    # Old unified Compose already declares the TLS mount. Preserve the exact
    # old core image/config, not necessarily the old container ID/mount set.
    require(life.image_id(parse_env(ENV.read_bytes())["RO_XRAY_IMAGE"])==record["old_xray_image"],
            "old Xray image digest changed")
    require(life.image_id(parse_env(ENV.read_bytes())["RO_HUB_IMAGE"])==record["old_hub_image"],
            "old HUB image digest changed")
    if not life.active():
        compose("up","-d","--no-deps","--no-build","--force-recreate","xray")
    require(run("docker","inspect","--format","{{.Image}}",life.CONTAINER).decode().strip()==record["old_xray_image"],
            "running Xray image differs from rollback image")
    compose("up","-d","--no-deps","--no-build","--force-recreate","hub")
    wait_ready(record)
    restart_timers(record,restore=True)
    write_state(stage,record,"ROLLED_BACK")
    release_hold(record)
    print("ACTIVE UPGRADE ROLLBACK: VERIFIED",flush=True)


def release_hold(record=None):
    check_upgrade_hold(record or {})
    if (record or {}).get("legacy_hold") is not None:
        print("LEGACY HOLD: PRESERVED",flush=True)
        return
    life.TRANSPORT_HOLD.unlink()
    sync_directory(life.TRANSPORT_HOLD.parent)


def apply(stage, preserve_legacy_hold=False, allowed_degraded=()):
    manifest=read_json(stage/"manifest.json")
    require(manifest["phase"]=="STAGED_ONLY" and not (stage/"upgrade-state.json").exists(),
            "candidate was already used")
    check_installer_revision(manifest["new_commit"])
    require(head(SRC)==manifest["old_commit"],"source revision mismatch")
    require(COMPOSE.is_symlink() and COMPOSE.resolve()==SRC/"deploy/docker-compose.yml","unexpected compose entry point")
    clean_source()
    require(not run("git","-C",str(REPO),"status","--porcelain","--untracked-files=all").strip(),
            "candidate source not clean")
    preserved=None
    if preserve_legacy_hold:
        require(manifest.get("preserved_hold") is True,"candidate not prepared with preserved hold")
        preserved=legacy_hold()
    else:
        require(not life.TRANSPORT_HOLD.exists() and not life.TRANSPORT_HOLD.is_symlink(),"maintenance active")
    check_quiescent()
    require(containers()==manifest["containers"],"running containers changed since staging")
    require(digest(ENV.read_bytes())==manifest["old_env_sha256"] and
            digest(XRAY.read_bytes())==manifest["xray_sha256"],"baseline changed")
    require((SRC/"deploy/nginx/embedded.conf").read_bytes()==(REPO/"deploy/nginx/embedded.conf").read_bytes(),
            "project nginx changes require separate migration")
    require((stage/"candidate-compose.yml").read_bytes()==(REPO/"deploy/docker-compose.yml").read_bytes(),
            "candidate compose changed")
    require((stage/"previous.env").read_bytes()==ENV.read_bytes() and
            (stage/"candidate.env").read_bytes()==replace_hub(ENV.read_bytes(),manifest["hub_image"]),
            "candidate env mismatch")
    for unit in UNIT_NAMES:
        dropins=UNITS/(unit+".d")
        require(not dropins.exists() or not any(dropins.iterdir()),"unexpected systemd drop-in: "+unit)
    require(life.image_id(manifest["hub_image"])==manifest["hub_image_id"],"candidate image changed")
    settings=parse_env(ENV.read_bytes())
    for service,var in (("hub","RO_HUB_IMAGE"),("xray","RO_XRAY_IMAGE")):
        require(life.image_id(settings[var])==manifest["containers"][service]["image"],
                "active image tag was changed: "+service)
    life.candidate_test(settings["RO_XRAY_IMAGE"])
    database,baseline=fleet.capture(allowed_degraded)
    record={"old_commit":manifest["old_commit"],"new_commit":manifest["new_commit"],
        "old_xray_image":manifest["containers"]["xray"]["image"],
        "old_hub_image":manifest["containers"]["hub"]["image"],
        "xray_sha":digest(XRAY.read_bytes()),"map_sha":digest(NGINX.read_bytes()),
        "nginx_sha":digest(EXTERNAL_NGINX.read_bytes()),"nginx_full_sha":nginx_digest("external-stream"),
        "started":int(time.time()),
        "fleet":{"database":database,"baseline":baseline},"timers":{}, "legacy_hold":preserved}
    units=stage/"activation-units"
    units.mkdir(mode=0o700)
    for name in UNIT_NAMES:
        path=UNITS/name
        require(not path.is_symlink(),"unexpected unit symlink")
        if path.exists():
            private_write(units/name,path.read_bytes())
    for timer in TIMERS:
        present=(UNITS/timer).is_file()
        record["timers"][timer]={"present":present,"active":present and service_state(timer)=="active"}
    require(all(record["timers"][t]["active"] for t in TIMERS[:2]),"legacy recovery timers inactive")
    write_state(stage,record,"PREPARED")
    if preserved is None:
        atomic(life.TRANSPORT_HOLD,HOLD)
    check_upgrade_hold(record)
    try:
        stop_workers()
        stop_hub()
        check_upgrade_hold(record)
        snapshot_db(Path(database),stage/"hub-at-cutover.db")
        write_state(stage,record,"SWITCHING")
        clean_source()
        run("git","-C",str(SRC),"checkout","--detach",record["new_commit"],timeout=60)
        atomic(ENV,(stage/"candidate.env").read_bytes())
        install_units(stage)
        run("systemctl","start",ADAPTER,timeout=30)
        if manifest["requires_xray_recreate"]:
            compose("up","-d","--no-deps","--no-build","--force-recreate","xray")
        require(identity()["image"]==record["old_xray_image"],"Xray image changed during upgrade")
        compose("up","-d","--no-deps","--no-build","--force-recreate","hub")
        write_state(stage,record,"VERIFYING")
        wait_ready(record)
        probe_adapter()
        require(containers()["hub"]["image"]==manifest["hub_image_id"],"wrong HUB image")
        require(containers()["nginx"]==manifest["containers"]["nginx"],"project nginx changed")
        check_upgrade_hold(record)
        restart_timers(record)
        check_upgrade_hold(record)
        write_state(stage,record,"ACCEPTED")
        release_hold(record)
        print("ACTIVE STACK UPGRADE: VERIFIED; WEB PUBLICATION DISABLED",flush=True)
    except BaseException:
        if record["phase"] not in TERMINAL:
            rollback(stage,record)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("apply","recover","preflight"))
    parser.add_argument("--stage")
    parser.add_argument("--allow-degraded-router",action="append",default=[])
    parser.add_argument("--confirm-maintenance",action="store_true")
    parser.add_argument("--preserve-legacy-hold",action="store_true")
    args=parser.parse_args()
    os.umask(0o077)
    require(os.geteuid()==0,"root required")
    if args.action=="preflight":
        with exclusive(life.LOCK):
            fleet.capture(args.allow_degraded_router)
            print("FLEET PREFLIGHT: PASS")
        return
    require(args.confirm_maintenance and args.stage,"stage and explicit maintenance confirmation required")
    stage=checked_stage(args.stage)
    with exclusive(life.LOCK):
        if args.action=="apply":
            apply(stage,args.preserve_legacy_hold,args.allow_degraded_router)
        else:
            record=read_json(stage/"upgrade-state.json")
            check_installer_revision(record["new_commit"])
            check_upgrade_hold(record)
            if record["phase"] in TERMINAL:
                check_files(record)
                release_hold(record)
            else:
                rollback(stage,record)


if __name__=="__main__":
    main()
