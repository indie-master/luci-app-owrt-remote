#!/usr/bin/env python3
"""Independent Xray health watchdog. Run from systemd timer, never HUB.

Reconcile abandoned unified core transactions. Legacy deployments retain
their separate health check and digest-checked rollback.
"""
from __future__ import annotations

import json
import os
import sys
import time

# Import adjacent, reviewed lifecycle code; fixed namespace (no input paths).
import manager as m
from core_job_runner import recover_unified
from unified_core_manager import configured_stack_env, legacy_exclusion
from unified_xray_adapter import Adapter
import xray_lifecycle as life

STATE = life.ROOT / "data" / "xray-watchdog.json"


def atomic(path, value):
    life.atomic(path, (json.dumps(value, sort_keys=True) + "\n").encode())


def run():
    m.require(os.geteuid() == 0, "Run watchdog as root")
    env = configured_stack_env()
    if env is not None:
        recover_unified(Adapter(env))
        return

    with m.deployment_lock(), legacy_exclusion():
        run_legacy()


def run_legacy():
    if life.TRANSPORT_HOLD.exists() or life.TRANSPORT_HOLD.is_symlink():
        print("WATCHDOG: configuration transaction pending, skipped")
        return

    if not life.JOURNAL.is_file() or not life.ENV.is_file():
        print("WATCHDOG: no committed core upgrade to monitor")
        return
    from stack_runtime import protected

    legacy_env = protected(life.ENV).read_bytes()
    keys = {line.strip().partition(b"=")[0].strip() for line in legacy_env.splitlines()}
    m.require(not keys.intersection({b"RO_STACK_MODE", b"RO_STACK_NAME"}) and
              b"unified_adapter" not in legacy_env,
              "Managed Stack requires an explicit watchdog selector")
    journal = json.loads(protected(life.JOURNAL).read_text())
    if journal.get("phase") in ("switching", "rolling-back"):
        life.rollback()
        return
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
    except m.Blocked as exc:
        print("WATCHDOG BLOCKED:", str(exc), file=sys.stderr)
        sys.exit(1)
    except BaseException as exc:
        print("WATCHDOG ERROR:", type(exc).__name__, file=sys.stderr)
        sys.exit(1)
