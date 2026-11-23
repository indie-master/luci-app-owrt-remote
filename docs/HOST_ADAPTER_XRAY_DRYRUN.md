# Belt Xray pre-apply read-only dry-run

**Scope:** test a synthetic seventh WSS router against Belt's *installed Xray core* without registering or connecting a real router.

Previously confirmed on Belt: the root host adapter rejects UID 0, accepts only UID 23088, returns healthy host Xray state, and leaves the production HUB/Xray services active. The synthetic WSS database/Xray structural validator also passed on six live routers.

## Design

- A newly added `xray.preview` request is handled by the same read-only, UID-authorized Unix socket. Its own fixed payload contains `expected_sha256` and a complete candidate Xray JSON.
- It checks that the current host Xray SHA matches, calls `validate_transition` (only two added VLESS clients, two loopback portals, and two rules), and invokes `/usr/local/bin/xray run -test -config` on a temporary 0600 JSON file under `/run/router-orchestrator`.
- The temporary file is always deleted; `/etc/xray/owrt-remote.json`, maintenance hold, systemd host HUB, systemd Xray and routers are never changed.
- The operator's test runs against the existing private snapshot, adds the *synthetic* router in an isolated disposable SQLite copy, and forwards the Xray candidate only in memory to the socket as UID 23088.
- Output includes only success/failure/status and no UUIDs, private keys, token files or full JSON.
- This remains **preview-only**: no `--allow-xray-apply`, `--allow-xray-restart`, or production cutover.

## First: supervised upgrade of the already installed read-only adapter

The existing root adapter runs from `/opt/router-orchestrator/host-adapter/host_adapter.py`, not from Git. Fetching a new Git branch does **not** upgrade that root service. The specific new operation must be pinned separately using the opt-in script.

After verifying a reviewed 40-character SHA and clean checkout in `/opt/router-orchestrator/release`, invoke:

```bash
sudo bash tools/orchestrator_host_adapter_readonly_upgrade.sh --upgrade-readonly "$EXPECTED"
```

The script requires the installed adapter to byte-match the previously approved commit `8a9e3a87c0915cf6d616370dd0942b1fa121f308`, saves a 0600 private copy, atomically replaces only the pinned binary and restarts **only** `router-orchestrator-host-adapter.service`. It then re-runs UID/socket/Xray checks and re-verifies the Xray SHA. On failure, it tries to restore the previous adapter binary and restart the adapter. No HUB or Xray restart.

**Do not re-run the original `--install-readonly` installer**; it correctly refuses an existing installation.

## Second: actual read-only Xray -test on the synthetic router

```bash
sudo python3 tools/orchestrator_host_adapter_xray_dryrun.py \
  --snapshot /opt/router-orchestrator/backups/orchestrator-docker-rehearsal-xn5xakyp \
  --expect-commit "$EXPECTED"
```

Expected: `BELT XRAY LIVE DRY-RUN: PASS`, unchanged six-router inventory, a synthetic WSS router accepted by the installed Xray core, and no production apply/restart. If it fails, inspect the printed safe blocker and stop; do not remove the maintenance hold.

## Gates remaining

- Prove the above dry-run on real Belt, including Xray -test, with a rooted previous-adapter backup available.
- Separately implement/review a narrowly scoped **write** capability that is not enabled by this unit. Require real `xray run -test`, strong post-restart endpoint checks, backup, rollback and explicit operator approval in a maintenance window.
- Prove first actual router onboarding without breaking five WSS and one TCP (the offline AX3000T is not repaired as part of this work).
- Complete VPS terminal and Push parity, then approve a single-writer Docker HUB cutover with rollback.