#!/usr/bin/env python3
"""Independent Xray health watchdog. Run from systemd timer, never HUB.

After 3 successive failures of a previously committed candidate image,
invoke the digest-checked rollback. Do not change unjournaled deployments.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sys
import time

# Import adjacent, reviewed lifecycle code; fixed namespace (no input paths).
import xray_lifecycle as life

STATE = life.ROOT / "data" / "xray-watchdog.json"


def atomic(path, value):
    life.atomic(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def run():
    if os.geteuid() != 0:
        raise RuntimeError("root required")
    life.LOCK.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with life.LOCK.open("a+") as lock:
        # Skip while core manager holds exclusive lifecycle lock.
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("WATCHDOG: lifecycle in progress, skipped")
            return

        if life.TRANSPORT_HOLD.exists():
            print("WATCHDOG: configuration transaction pending, skipped")
            return

        if not life.JOURNAL.is_file() or not life.ENV.is_file():
            print("WATCHDOG: no committed core upgrade to monitor")
            return
        journal = json.loads(life.JOURNAL.read_text())
        if journal.get("phase") != "committed":
            print("WATCHDOG: upgrade not committed, no action")
            return
        current = life.env_image(life.ENV.read_bytes())
        if current != journal.get("new_image"):
            print("WATCHDOG: deployed image differs from journal, skipped")
            return
        if not isinstance(journal.get("old_image_id"), str):
            raise RuntimeError("invalid old-image digest")
        if life.image_id(journal["old_image"]) != journal["old_image_id"]:
            raise RuntimeError("last known-good Xray image was altered")

        state = {"failed_checks": 0, "image": current}
        if STATE.is_file():
            previous = json.loads(STATE.read_text())
            if previous.get("image") == current:
                state = previous
        if life.active():
            state = {"failed_checks": 0, "image": current,
                     "last_ok": int(time.time())}
            atomic(STATE, state)
            print("WATCHDOG: Docker Xray healthy")
            return

        state["failed_checks"] = int(state.get("failed_checks", 0)) + 1
        state["last_failure"] = int(time.time())
        atomic(STATE, state)
        if state["failed_checks"] < 3:
            print("WATCHDOG: unhealthy, retry", state["failed_checks"], "/3")
            return

        print("WATCHDOG: 3 consecutive failures, restoring previous pinned image", flush=True)
        try:
            life.rollback()
        except BaseException:
            print("CRITICAL: Xray watchdog rollback failed", file=sys.stderr)
            raise
        atomic(STATE, {"failed_checks": 0, "image": journal["old_image"],
                       "rollback_unix": int(time.time())})
        print("WATCHDOG: ROLLBACK PASS")


if __name__ == "__main__":
    try:
        run()
    except BaseException as exc:
        print("WATCHDOG ERROR:", type(exc).__name__, file=sys.stderr)
        sys.exit(1)
