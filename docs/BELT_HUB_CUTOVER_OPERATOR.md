# Belt HUB-only cutover: operator gate

**Status: development, NOT YET AUTHORIZED for production switching.**

A source-exact Docker rehearsal and an upgraded-image compatibility rehearsal
have passed using an isolated real Belt snapshot: six routers, five WSS and one
legacy TCP; historical logs and SQLite schema retained. Those tests do not
establish live LuCI/SSH, terminal, Push or router enrollment parity.

## Read-only acceptance command

From the reviewed pinned commit in /opt/router-orchestrator/release:

    sudo python3 tools/orchestrator_belt_cutover.py preflight \
      --expect-commit "$(git rev-parse HEAD)"

This checks production systemd/Xray health, Xray-to-DB credential and port
parity, root-only backups, no competing Docker HUB, a clean pinned Git tree,
and whether rollback backup and host state are on the same filesystem.

**Passing preflight does not authorize running prepare or activate.**

## Proposed single-writer transaction

- Preflight: read-only and no file or service mutations.
- Prepare: build the reviewed image and store a root-only transaction journal.
  No production services stop.
- Activate, after separate approval: private online snapshot, then disable
  and stop only owrt-remote.service. Take a final consistent SQLite Backup API
  copy and preserve every regular state file and empty directory in
  /opt/router-orchestrator/data, owned by UID 23088. Original
  /var/lib/owrt-remote remains intact. Launch Docker HUB on 127.0.0.1:8088.
  Do not restart or regenerate the existing host Xray/Nginx/TLS.
- Rollback: STOP Docker first, copy the LATEST Docker state and new history,
  atomically promote it to the original host state path, archive the original,
  and restart systemd. Never replace newer state with an older snapshot.
  Archive active Docker .env so accidental concurrent startup fails.

The private journal is at /opt/router-orchestrator/.cutover.json.
Private snapshots are under /opt/router-orchestrator/backups.

## Incomplete and unsafe-to-activate gates

1. Prove host Xray adapter one-router WSS enrollment without modifying
   other transport clients or any existing LuCI/SSH reverse portal.
2. Confirm real browser, SSH, LuCI, heartbeat, session, history, Push,
   Passkey, metrics and admin API after supervised single-writer switch.
3. Restore the VPS terminal feature through an audited capability boundary.
4. Inject failure tests: corrupt candidate, Docker start failure, failed
   health check, bad permissions, interrupted rollback, OS reboot/power loss.
5. Provide a protected emergency console and crash-resumable recovery.
   The present coordinator has best-effort caught-exception rollback but
   **does not recover automatically from SIGKILL or power loss**.
6. Preserve AX3000T-Shteinberg legacy TCP configuration; do not use this
   migration to repair a separately troubled router.
7. Confirm site-specific public HTTPS hostname before prepare; do not
   silently substitute the staging URL or a guessed hostname.

The existing Xray service remains host-managed during HUB-only migration.
Its maintenance hold is preserved. The current readonly backend does not
yet support automated addition of new routers.

Never upload private snapshots, Xray UUIDs, session keys, tokens or
authentication files into GitHub, chat or CI.

## Manual crash recovery (not authorized on production yet)

If the journal reports STOP_REQUESTED, HOST_STOPPED, STATE_COPIED,
DOCKER_START_REQUESTED, RECOVERING, RECOVERY_FAILED, ROLLBACK_STARTED,
DOCKER_FENCED, STATE_RECOVERED, ENV_SEALED or HOST_RESTART_REQUESTED:

1. Use a trusted rescue console, confirm Xray and Nginx remain healthy, and
   inspect current writers with the read-only command:

       sudo python3 tools/orchestrator_belt_cutover.py status

2. Do **not** manually restart both HUB instances. If both are running,
   stop and investigate: recovery is designed to fail closed.
3. After reviewing the private transaction journal, the guarded manual
   recovery entry point is:

       sudo python3 tools/orchestrator_belt_cutover.py recover --confirm

   It first disables the Docker restart policy and stops the container,
   verifies the latest data, then promotes it to the systemd directory and
   restores the old service. It can be rerun if interrupted; it never
   uses the old pre-cutover snapshot to overwrite newer Docker history.
4. If either the Docker daemon or state parity check cannot be verified,
   recovery blocks rather than guessing. Do not delete the journal or
   state directories, and do not bypass this guard.

**Do not run the recovery command on Belt unless a genuine cutover failure
is being handled in an approved maintenance window.**
