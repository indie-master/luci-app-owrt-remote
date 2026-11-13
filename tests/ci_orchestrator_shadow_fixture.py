#!/usr/bin/env python3
"""Create a synthetic REAL HUB six-router Docker rehearsal (CI only).

No Belt state, tokens, private keys, or production network resources. Builds
a compatible v119 SQLite DB and Xray JSON with five WSS and one TCP router,
three retained TCP fallbacks, preserved historical router_logs and test auth.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import orchestrator_belt_docker_migration_gate as gate
import orchestrator_docker_rehearsal_restore as restore
import orchestrator_docker_image_context as image_context

ROUTERS = (
    "main", "WR3000S_2", "PaxoBs", "Netis_N6_router5",
    "Movietonn", "AX3000T-Shteinberg",
)
LEGACY = {"main", "WR3000S_2", "PaxoBs"}


def real_hub_fixture(out: Path) -> tuple[Path, Path]:
    source = ROOT / "vps" / "owrt-remote-hub.py"
    state = out / "synthetic-live-state"
    state.mkdir(mode=0o700)
    original_env = os.environ.get("OWRT_REMOTE_STATE_DIR")
    os.environ["OWRT_REMOTE_STATE_DIR"] = str(state)
    try:
        spec = importlib.util.spec_from_file_location("orchestrator_shadow_ci_hub", source)
        assert spec and spec.loader
        hub = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = hub
        spec.loader.exec_module(hub)
        db = state / "hub.db"
        with hub.connect(db) as conn:
            hub.init_db(conn)
            for n, rid in enumerate(ROUTERS):
                mode = "tcp" if rid == "AX3000T-Shteinberg" else "ws"
                hub.upsert_router(conn, {
                    "id": rid, "name": rid, "role": "node",
                    "entry_port": 18080 + n * 10,
                    "ssh_entry_port": 19080 + n * 10,
                    "vps_host": "orchestrator.example.org",
                    "public_url": "https://orchestrator.example.org",
                    "vless_port": 18443 if mode == "tcp" else 443,
                    "vps_transport": mode,
                    "vless_uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, "ci-luci-" + rid)),
                    "ssh_vless_uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, "ci-ssh-" + rid)),
                })
                if rid in LEGACY:
                    conn.execute(
                        "update routers set vps_allow_legacy=1 where id=?", (rid,))
                if n < 5:
                    conn.execute(
                        "insert into router_logs(router_id,content) values(?,?)",
                        (rid, f"CI_HISTORY_{n}".encode()))
            rows = hub.list_router_rows(conn)
        with hub.connect(db) as conn:
            rows = hub.list_router_rows(conn)
            xray_cfg = hub.make_server_xray_config(rows, listen_port=18443)
        hub.save_auth("test-admin", "ci-private-test-password-2026")
        hub.agent_token()
        hub.session_token()
        (state / ".xray-maintenance-hold").write_text(
            "CI synthetic, block service restarts\n")
        (state / "router-note-images").mkdir(mode=0o700)
        (state / "router-note-images" / "fixture.txt").write_text(
            "synthetic history image marker")
        xray = out / "synthetic-xray.json"
        xray.write_text(json.dumps(xray_cfg))
        inventory, _ = gate.analyze(state, xray)
        assert inventory["active_count"] == 6
        assert inventory["wss"] == 5 and inventory["tcp"] == 1
        assert inventory["tables"].get("router_logs") == 5
        return state, xray
    finally:
        if original_env is None:
            os.environ.pop("OWRT_REMOTE_STATE_DIR", None)
        else:
            os.environ["OWRT_REMOTE_STATE_DIR"] = original_env


def make(out: Path) -> dict:
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    state, xray = real_hub_fixture(out)
    snapshot, _ = gate.snapshot(state, xray, ROOT / "vps" / "owrt-remote-hub.py", out)
    stage, report = restore.restore(snapshot, out)
    context, sha = image_context.build_context(
        stage, ROOT / "vps" / "static",
        ROOT / "docker" / "hub" / "Dockerfile.rehearsal", out)
    assert report["source_sha256"] == sha
    result = {
        "context": str(context),
        "stage": str(stage),
        "source_sha256": sha,
        "image": "orchestrator-shadow:ci",
    }
    (out / "ci-paths.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-root", type=Path, required=True)
    args = p.parse_args()
    obj = make(args.output_root)
    print("SYNTHETIC SIX-ROUTER SNAPSHOT: PASS")
    print("WSS=5 / TCP=1 / SSH+LuCI=12 / saved history=5")
    print("Build context contains exact development HUB source, not synthetic secrets")
    print("CI PATHS:", args.output_root / "ci-paths.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
