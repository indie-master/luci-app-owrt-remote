# Belt: operator-controlled one-router WSS commit pilot

**DEVELOPMENT / NOT APPROVED FOR LIVE APPLICATION**

On November 23 the installed Belt Xray core accepted an isolated synthetic seventh WSS router through the UID-limited read-only host adapter (`BELT XRAY LIVE DRY-RUN: PASS`). This is still not a real router enrollment.

This pilot adds a **separate root-only operator CLI**. It does **not** enable `xray.apply` or `xray.restart` on the running Unix socket; that adapter stays read-only. The old systemd HUB and Xray remain the production processes.

## Live router registration prerequisites

A single **real new router** must first have an owner-approved record in the active HUB database with WSS/443, non-legacy mode, paired VLESS UUIDs and two unused reverse ports. The user must explicitly approve a maintenance window that tolerates a short Xray restart, because existing reverse tunnels can briefly disconnect. This utility does not create that record or install the OpenWrt client; it cannot prove SSH/LuCI/heartbeat after the restart by itself. AX3000T legacy TCP must stay unchanged.

## What is implemented

- `status --expect-commit <sha>`: read-only, validates the pinned clean release, systemd HUB/Xray/read-only adapter and existing maintenance hold. Prints transport counts, live Xray SHA and transaction state. **Works without any new router**.
- `preview --router-id NEW --expect-commit <sha>`: uses SQLite's backup API to copy live HUB SQLite into a root-only disposable directory, imports the reviewed HUB candidate into that directory, creates an **additive** Xray config for only the named missing WSS record, verifies the existing entire Xray configuration through `validate_transition`, and runs the installed `xray run -test` on a private 0600 temporary file. Prints only hashes and the specified router ID. Does not restart/apply.
- `apply --router-id NEW --expect-commit <sha> --expect-candidate-sha256 <preview hash> --approve-maintenance`: **NOT AUTHORIZED YET**. Double-binds the exact reviewed candidate hash and pinned Git commit, checks service state, maintenance hold and stable fleet identities, takes an fsynced byte-exact private config backup, writes a root-owned transaction journal, atomically replaces only the Xray config and restarts only dedicated `owrt-remote-xray.service`. It checks 18443/18444 sockets and service health before marking **APPLIED_PENDING_ROUTER**, explicitly NOT confirmed router success. No original SQLite records or host Nginx/HUB are modified.
- `rollback --confirm`: manual/exception recovery if Xray restart fails or operator aborts. Requires current Xray SHA to match the original or this one reviewed candidate; restores exact previous bytes, restarts only Xray, retains all private backup/journal evidence and keeps the maintenance hold. If unrelated config drift is detected, it blocks instead of overwriting.
- A deployment lock prevents two concurrent pilot operations. A journal existing from an earlier transaction blocks new applications until operator review. Tests inject restart failure, crash after applying config, external edits and wrong candidate SHA. All test data is synthetic.

## Current safe task

From a pinned, clean checkout on Belt, **only the following status command is safe to run immediately**, even with just the existing six routers:

```bash
cd /opt/router-orchestrator/release || exit 1
EXPECTED="$(git rev-parse HEAD)"
sudo python3 tools/orchestrator_wss_commit_pilot.py status --expect-commit "$EXPECTED"
```

Expected: `WSS PILOT STATUS: PASS`, `FLEET: WSS=5 TCP=1`, `TRANSACTION: NONE`, and unchanged systemd services. **Do not run apply or rollback at this stage.**

## Remaining acceptance gates

1. Finish testing all failure injections (including host reboot or power loss) and check that recovery is possible without any dependency on Docker, Git checkout or a working main HUB. Current manual rollback still depends on executing this reviewed script from the release checkout; package a rescue copy separately before authorization.
2. Add an admin-only, individual provisioning workflow with short-lived, one-use credentials; **no global HUB token in an installation command**.
3. Independently verify real router WSS heartbeat, reverse LuCI HTTP and SSH banner after install, then verify the original five WSS clients after any Xray restart; do not assume open TCP listener means reverse connectivity.
4. Integrate the strictly bounded apply capability with Docker HUB via a reviewed dedicated capability boundary; the current root-only CLI is deliberately **not** a direct network/API endpoint.
5. Only later approve single-writer systemd→Docker HUB cutover and acceptance of terminal, sessions, Push and existing fleet state.

**Do not remove the maintenance hold, manually edit the live Xray JSON, or enable write flags in the installed host adapter.** Private backups contain Xray credentials; never upload them to GitHub or send them into chat.
