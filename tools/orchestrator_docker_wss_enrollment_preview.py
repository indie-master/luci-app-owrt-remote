#!/usr/bin/env python3
"""Read-only Belt WSS enrollment preview using the *upgraded* HUB generator.

The tool restores a VERIFIED private rehearsal snapshot, imports the pinned HUB
source against that restored state, checks that its generated Xray config
exactly matches the deployed reference, adds one SYNTHETIC WSS router to the
disposable SQLite copy, and validates the precise Xray transition through
docker/host_adapter.py. No host Xray changes, live DB writes, network or ports.
The private temporary restored state is removed afterwards.

A dry-run pass is necessary but NOT permission to turn on Xray mutation.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import uuid

import orchestrator_belt_docker_migration_gate as gate
import orchestrator_docker_rehearsal_restore as restored
import orchestrator_docker_candidate_rehearsal as candidate

ROOT = Path("/opt/router-orchestrator")
REPO = Path(__file__).resolve().parents[1]
BACKUPS = ROOT / "backups"


class Blocked(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Blocked(message)


def select_pair(used_ports: set[int]) -> tuple[int, int]:
    """Allocate only TEST ports on private data, not on Belt."""
    for port in range(24000, 28000):
        if port not in used_ports and port + 1000 not in used_ports:
            return port, port + 1000
    raise Blocked("no free synthetic reverse port pair")


def import_checked_module(name: str, path: Path):
    require(path.is_file() and not path.is_symlink(), "unsafe module path")
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None,
            "cannot import reviewed module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def preview(snapshot: Path, expected_commit: str, backup_root: Path) -> dict:
    require(os.geteuid() == 0, "run as root on Belt")
    candidate.verify_private_parent(backup_root)
    candidate.verify_checkout(expected_commit)
    require(snapshot.is_dir() and not snapshot.is_symlink() and
            snapshot.resolve().parent == backup_root.resolve(),
            "snapshot must be directly under private backup root")
    manifest = gate.inspect_archive(snapshot)
    require(manifest.get("kind") == "REHEARSAL_ONLY_NOT_CUTOVER",
            "snapshot type is not an approved rehearsal archive")

    stage = None
    original_env = {name: os.environ.get(name) for name in (
        "OWRT_REMOTE_STATE_DIR", "OWRT_REMOTE_RUNTIME",
        "OWRT_REMOTE_XRAY_CONTROL_BACKEND")}
    phase = "snapshot.restore"
    try:
        stage, _ = restored.restore(snapshot, backup_root)
        state = stage / "state"
        phase = "snapshot.inventory"
        xray_ref = stage / "reference/xray.json"
        xray = json.loads(xray_ref.read_text())
        baseline = gate.db_inventory(state / "hub.db")
        require(baseline["active_count"] > 0, "no existing routers")
        # Refuse a stale snapshot after a real fleet or host Xray change.
        # Reading the live files here never modifies them.
        live_xray = Path("/etc/xray/owrt-remote.json")
        live_state = Path("/var/lib/owrt-remote")
        require(live_xray.is_file() and not live_xray.is_symlink(),
                "live host Xray reference is missing/unsafe")
        phase = "live.xray.sha256"
        require(gate.file_sha(xray_ref) == gate.file_sha(live_xray),
                "host Xray has changed since snapshot")
        phase = "live.router.inventory"
        actual = gate.db_inventory(live_state / "hub.db")
        require(actual["routers"] == baseline["routers"],
                "live router identity/transport inventory changed since snapshot")

        # The checked code receives *only* the private restored state path.
        os.environ.update({
            "OWRT_REMOTE_STATE_DIR": str(state),
            "OWRT_REMOTE_RUNTIME": "docker",
            "OWRT_REMOTE_XRAY_CONTROL_BACKEND": "external_readonly",
        })
        phase = "candidate.import"
        hub = import_checked_module(
            "orchestrator_preview_hub_" + uuid.uuid4().hex,
            REPO / "vps/owrt-remote-hub.py")
        adapter = import_checked_module(
            "orchestrator_preview_host_" + uuid.uuid4().hex,
            REPO / "docker/host_adapter.py")
        old_tcp = [i for i in xray.get("inbounds", [])
                   if i.get("tag") == "owrt-remote-vless"]
        require(len(old_tcp) == 1, "legacy TCP inbound missing/ambiguous")
        listen = old_tcp[0].get("listen", "0.0.0.0")
        port = int(old_tcp[0]["port"])

        phase = "candidate.baseline.generator"
        with hub.connect(state / "hub.db") as db:
            hub.init_db(db)  # on disposable copy only
            rows = hub.list_router_rows(db)
            original_gen = hub.make_server_xray_config(
                rows, listen_host=listen, listen_port=port)
        # Historical Belt Xray metadata/ordering may differ from the latest
        # full generator. Validate its existing router/port/UUID parity but
        # preserve the live JSON byte-for-byte outside the NEW router delta.
        baseline_drift = original_gen != xray
        phase = "candidate.baseline.parity"
        gate.xray_parity(baseline, xray)

        phase = "synthetic.port.selection"
        used = {int(i.get("port", 0)) for i in xray.get("inbounds", [])}
        used.update(int(r[k]) for r in rows for k in
                    ("entry_port", "ssh_entry_port") if r[k])
        admin_port, ssh_port = select_pair(used)
        synthetic_id = "ro_probe_" + uuid.uuid4().hex[:10]
        new_values = {
            "id": synthetic_id,
            "name": "Disposable WSS test only",
            "vps_host": "hub.example.invalid",
            "vps_transport": "ws",
            "vless_port": 443,
            "vless_uuid": str(uuid.uuid4()),
            "ssh_vless_uuid": str(uuid.uuid4()),
            "entry_port": admin_port,
            "ssh_entry_port": ssh_port,
            "admin_host": "127.0.0.1",
            "admin_port": 80,
            "ssh_host": "127.0.0.1",
            "ssh_port": 22,
            "vps_allow_legacy": 0,
        }
        phase = "synthetic.sqlite.insert"
        with hub.connect(state / "hub.db") as db:
            hub.upsert_router(db, new_values)
            new_rows = hub.list_router_rows(db)
            generated = hub.build_additive_wss_candidate(xray, new_rows)
        phase = "synthetic.xray.transition"
        adapter.validate_transition(xray, generated)

        phase = "synthetic.postcheck"
        after = gate.db_inventory(state / "hub.db")
        require(after["active_count"] == baseline["active_count"] + 1,
                "synthetic enrollment row count invalid")
        old_ids = {row["id"]: row for row in baseline["routers"]}
        new_ids = {row["id"]: row for row in after["routers"]}
        require(all(new_ids.get(key) == value for key, value in old_ids.items()),
                "original router identity changed in disposable copy")
        require(after["tables"]["router_logs"] ==
                baseline["tables"]["router_logs"],
                "historical logs changed in disposable copy")
        return {"routers": baseline["active_count"], "wss": baseline["wss"],
                "tcp": baseline["tcp"], "new_router": "WSS",
                "preserved_logs": baseline["tables"]["router_logs"],
                "baseline_generator_drift_preserved": baseline_drift}
    except Blocked as exc:
        # Only our own fixed, operator-safe messages reach the terminal.
        # No raw Xray JSON, UUID, token, key or SQLite data is printed.
        raise Blocked(f"{phase}: {exc}") from exc
    except (gate.GateError, restored.Blocked, candidate.Rejected,
            ValueError, TypeError, KeyError, RuntimeError) as exc:
        # External modules can embed private IDs in their exception strings.
        # Report the stable phase and exception class only.
        raise Blocked(f"{phase}: {type(exc).__name__}") from exc
    finally:
        for name, old in original_env.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
        if stage is not None:
            require(stage.parent.resolve() == backup_root.resolve() and
                    stage.name.startswith("orchestrator-shadow-state-") and
                    stage.is_dir() and not stage.is_symlink(),
                    "unsafe restored state cleanup target")
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--expect-commit", required=True)
    parser.add_argument("--backup-root", type=Path, default=BACKUPS)
    args = parser.parse_args()
    try:
        result = preview(args.snapshot, args.expect_commit, args.backup_root)
        print("WSS ENROLLMENT PREVIEW: PASS")
        print("FLEET: {routers} existing | WSS={wss} TCP={tcp}".format(**result))
        print("SYNTHETIC ROUTER: WSS/443 candidate accepted by host adapter")
        print("EXISTING XRAY GENERATOR DRIFT:",
              "PRESERVED" if result["baseline_generator_drift_preserved"]
              else "NONE")
        print("HISTORY: router_logs={preserved_logs}".format(**result))
        print("Xray: actual deployed config unchanged; no restart or apply")
        print("SQLITE: only disposable restored copy was modified")
        print("CLEANUP: private temporary restored state removed")
        print("NO CUTOVER: write capabilities remain disabled")
        return 0
    except Blocked as exc:
        print("WSS ENROLLMENT PREVIEW: BLOCKED — " + str(exc)[:230],
              file=sys.stderr)
        return 2
    except (gate.GateError, restored.Blocked, candidate.Rejected,
            OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        print("WSS ENROLLMENT PREVIEW: BLOCKED — " + type(exc).__name__,
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
