"""Read-only per-channel fleet acceptance for upgrades of an existing HUB.

Never equate a fresh heartbeat with working reverse SSH/LuCI. Capture each
working channel, preserve all of them, and require explicit acknowledgement
of any already degraded recently online router before maintenance.
"""
from concurrent.futures import ThreadPoolExecutor
import http.client
import socket
import time
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"docker"))
from secure_publication_worker import db_path, fleet_rows, hub_ready, ADMIN_CODES
import xray_lifecycle as life


def ssh_check(port):
    if not 1<=port<=65535:
        return False,"no-port"
    try:
        with socket.create_connection(("127.0.0.1",port),timeout=4) as sock:
            deadline=time.monotonic()+5
            data=b""
            while len(data)<4096 and time.monotonic()<deadline:
                sock.settimeout(max(.1,deadline-time.monotonic()))
                chunk=sock.recv(512)
                if not chunk:
                    break
                data+=chunk
                if any(line.startswith(b"SSH-") for line in data.splitlines()):
                    return True,"SSH banner"
        return False,"no SSH banner"
    except OSError as exc:
        return False,type(exc).__name__


def admin_check(port):
    if not 1<=port<=65535:
        return False,"no-port"
    status="no response"
    for path in ("/","/cgi-bin/luci/"):
        conn=http.client.HTTPConnection("127.0.0.1",port,timeout=4)
        try:
            conn.request("GET",path,headers={"Connection":"close"})
            code=conn.getresponse().status
            status="HTTP "+str(code)
            if code in ADMIN_CODES:
                return True,status
        except (OSError,http.client.HTTPException) as exc:
            status=type(exc).__name__
        finally:
            conn.close()
    return False,status


def probe(row):
    admin,admin_reason=admin_check(row["admin"])
    ssh,ssh_reason=ssh_check(row["ssh"])
    return {"admin_ok":admin,"ssh_ok":ssh,
            "admin_reason":admin_reason,"ssh_reason":ssh_reason}


def survey():
    path=db_path()
    rows=fleet_rows(path)
    cutoff=int(time.time())-180
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(probe,rows.values()))
    return str(path),{rid:dict(row,**result,recent=row["seen"]>=cutoff)
                      for (rid,row),result in zip(rows.items(),results)}


def baseline_from(rows, allowed_degraded=()):
    degraded=sorted(rid for rid,row in rows.items() if row["recent"] and
                    not (row["admin_ok"] and row["ssh_ok"]))
    if set(degraded)!=set(allowed_degraded):
        raise RuntimeError("existing degraded routers need explicit review: "+(", ".join(degraded) or "none"))
    baseline={rid:{key:row[key] for key in ("admin","ssh","admin_ok","ssh_ok","recent")}
              for rid,row in rows.items() if row["recent"] or row["admin_ok"] or row["ssh_ok"]}
    if not any(row["admin_ok"] and row["ssh_ok"] for row in baseline.values()):
        raise RuntimeError("no fully working reverse router available for acceptance")
    return baseline


def capture(allowed_degraded=()):
    if not hub_ready() or not life.active():
        raise RuntimeError("HUB or Xray baseline is unhealthy")
    path,rows=survey()
    for rid,row in sorted(rows.items()):
        print(f"{rid}: heartbeat={'RECENT' if row['recent'] else 'STALE'} "
              f"LuCI={'PASS' if row['admin_ok'] else 'FAIL'} ({row['admin_reason']}) "
              f"SSH={'PASS' if row['ssh_ok'] else 'FAIL'} ({row['ssh_reason']})",flush=True)
    return path,baseline_from(rows,allowed_degraded)


def preserved(before,after,cutoff):
    return (after is not None and before["admin"]==after["admin"] and before["ssh"]==after["ssh"]
            and (not before["recent"] or after["seen"]>=cutoff)
            and (not before["admin_ok"] or after["admin_ok"])
            and (not before["ssh_ok"] or after["ssh_ok"]))


def wait(context,since,timeout=180):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        path,rows=survey()
        if path!=context["database"]:
            raise RuntimeError("HUB database changed")
        if hub_ready() and life.active() and all(preserved(row,rows.get(rid),since)
                                                for rid,row in context["baseline"].items()):
            print("FLEET: all previously working channels and recent heartbeats preserved",flush=True)
            return True
        time.sleep(2)
    return False
