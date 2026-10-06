"""Read-only Linux monitoring of owrt-remote; no credentials/DB writes needed.

Usage on VPS: python3 monitor_vps_resources.py 10
FD samples can briefly include active requests or SQLite transactions.
The report records trends; real router/proxy/browser actions should run alongside.
"""
import datetime
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import urllib.request


def main(minutes):
    started = time.monotonic()
    since = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")
    report_path = Path(f"/tmp/owrt-resource-monitor-{stamp}.jsonl")
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    samples = []
    previous = None
    print(f"Read-only monitoring for {minutes} minute(s). Report: {report_path}", flush=True)
    with report_path.open("x", encoding="utf-8") as report:
        while True:
            pid = int(subprocess.check_output(["systemctl", "show", "owrt-remote", "-p", "MainPID", "--value"], text=True, timeout=10).strip())
            if pid <= 0:
                raise RuntimeError("owrt-remote has no running MainPID")
            proc = Path(f"/proc/{pid}")
            links = []
            for item in (proc / "fd").iterdir():
                try:
                    links.append(os.readlink(item))
                except FileNotFoundError:
                    pass  # a live request can close a descriptor while sampled
            fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])
            sample_time = time.monotonic()
            cpu = 0.0
            if previous and previous[0] == pid:
                cpu = 100 * (ticks - previous[1]) / ticks_per_second / (sample_time - previous[2])
            previous = pid, ticks, sample_time
            status = (proc / "status").read_text()
            memory = re.search(r"^VmRSS:\s+(\d+)", status, re.MULTILINE)
            threads = re.search(r"^Threads:\s+(\d+)", status, re.MULTILINE)
            try:
                with urllib.request.urlopen("http://127.0.0.1:8088/health", timeout=5) as response:
                    health = response.status == 200 and bool(json.load(response).get("ok"))
            except Exception as exc:
                health = f"error: {exc}"
            sample = {"time": datetime.datetime.now().isoformat(timespec="seconds"),
                      "elapsed": round(sample_time - started, 1), "pid": pid, "fd": len(links),
                      "hub_db_fd": sum("/hub.db" in link for link in links),
                      "cpu_percent": round(cpu, 2), "rss_kib": int(memory[1]) if memory else None,
                      "threads": int(threads[1]) if threads else None, "health": health}
            samples.append(sample)
            line = json.dumps(sample, ensure_ascii=False)
            print(line, flush=True)
            report.write(line + "\n")
            report.flush()
            if time.monotonic() - started >= minutes * 60:
                break
            time.sleep(min(10, max(0, minutes * 60 - (time.monotonic() - started))))

        journal = subprocess.check_output(["journalctl", "-u", "owrt-remote", "--since", since, "--no-pager"], text=True, errors="replace", timeout=30)
        pattern = re.compile(r"unable to open database|database is locked|sqlite|traceback|exception|monitor.*error|connection reset|remote end closed", re.IGNORECASE)
        errors = [line for line in journal.splitlines() if pattern.search(line)]
        window = max(1, min(10, len(samples) // 3))
        trend = {
            key: {"first_median": statistics.median(s[key] for s in samples[:window]),
                  "last_median": statistics.median(s[key] for s in samples[-window:])}
            for key in ("fd", "hub_db_fd", "rss_kib", "threads")
            if all(s[key] is not None for s in samples)
        }
        # Concurrent requests can briefly raise FD counts. Compare windows and
        # flag sustained growth; the report still needs workload-aware review.
        fd_growth = trend["fd"]["last_median"] > trend["fd"]["first_median"] + 8
        db_growth = trend["hub_db_fd"]["last_median"] > trend["hub_db_fd"]["first_median"] + 2
        summary = {"type": "summary", "seconds": samples[-1]["elapsed"], "first": samples[0], "last": samples[-1],
                   "fd_min": min(s["fd"] for s in samples), "fd_max": max(s["fd"] for s in samples),
                   "hub_db_fd_max": max(s["hub_db_fd"] for s in samples),
                   "pid_changed": len({s["pid"] for s in samples}) != 1,
                   "health_failures": sum(s["health"] is not True for s in samples), "matching_log_lines": errors,
                   "trend": trend, "sustained_fd_growth": fd_growth, "sustained_db_growth": db_growth}
        report.write(json.dumps(summary, ensure_ascii=False) + "\n")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        print(f"Report saved: {report_path}", flush=True)
        print("Check nginx and exercise the panel, a real router proxy, heartbeat and notifications alongside this report.", flush=True)
        if summary["pid_changed"] or summary["health_failures"] or errors or fd_growth or db_growth:
            raise RuntimeError("Monitoring found failures; inspect the saved report")


if __name__ == "__main__":
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    if not 1 <= minutes <= 1440:
        raise ValueError("minutes must be between 1 and 1440")
    main(minutes)
