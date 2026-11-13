# Belt -> Docker: complete state continuity and staged six-router cutover

**STATUS: implementation plan + read-only rehearsal tooling. NOT YET
AUTHORIZED FOR A LIVE CUTOVER.** Do not run a generic VPS installer, replace
the working HUB, or stop the production Xray service in this phase.

## Non-negotiable invariants

1. All six live routers retain their exact IDs, unique LuCI/SSH VLESS
   identities, reverse tags, hostnames, ports, permissions and configuration.
   Current accepted baseline: five WSS/443, one legacy TCP/18443
   (AX3000T-Shteinberg). The tool is generic and refuses unknown changes,
   rather than assuming that a fleet will always have six devices.
2. Existing /etc/xray/owrt-remote.json and the live Xray binary, process,
   loopback reverse ports, 18443 and 18444 listeners must NOT change
   during the HUB-only migration. Existing Nginx owns HTTPS/TLS :443.
3. Preserve 100% of the original persistent /var/lib/owrt-remote state,
   including files not yet known to the new application. SQLite hub.db
   is not the whole state.
4. Do not reset passwords, session signing keys, per-router tokens,
   authentication users/roles, saved SSH credentials, TOTP/Passkey,
   notifications, router logs, detailed status, historical statistics,
   push subscription state, VAPID keys, traffic counters, ACME files,
   or the .xray-maintenance-hold.
5. Never run host and container HUB simultaneously against the same
   writable database/state directory. Only one writer at any time.
6. Roll back the container by returning the **same current live state**
   to the old host HUB. Do not overwrite it with an older snapshot,
   because heartbeat/history/auth writes may have happened since
   cutover. An immutable snapshot is for recovery from corruption or
   offline rehearsal, not routine rollback.

## Explicit persistence inventory from source

The current HUB uses /var/lib/owrt-remote (OWRT_REMOTE_STATE_DIR) and
hub.db (OWRT_REMOTE_DB). Its state also includes:

- hub-auth.json, hub-session.token, hub-sessions.json, agent.token
- hub-notifications.json, hub-push-subscriptions.json, hub-push-delivery.json
- hub-vapid-private.pem and hub-vapid-public.txt, hub-boot.id
- xray-wan-reconnect-restart.json, hub-traffic-counters.json
- acme-webroot, maintenance hold and any future or user-added state
- SQLite DB tables such as routers and historical logs/audit

The GUI's existing Backup/Restore action is NOT a complete live Docker
migration mechanism. Its restore function rewrites selected endpoints
and may try to regenerate/reload Xray; DO NOT run it on the live
cutover path. The additional migration gate snapshots the complete
state tree and uses SQLite's online Backup API.

Outside the state directory, separately preserve and verify:
- /opt/owrt-remote/owrt-remote-hub.py and other installed app assets
- /etc/xray/owrt-remote.json, installed Xray binary and exact version
- systemd unit files and systemd drop-ins for HUB and Xray
- existing Nginx vhosts, symlinks and server certificate ownership
- the underlying secrets directory(s), protected permissions and any
  system-level webhook/proxy configuration used by the HUB
- daemon logs that are NOT in HUB SQLite/state, if historical
  journal retention is required (systemd journal is host-owned)

These references must be documented by the final migration operator
before the first cutover. Preserving the SQLite database alone is NOT
proof that all historical data or log files were retained.

## Stage 0 — audit of the *working* Belt (no service modifications)

From an explicitly reviewed, pinned commit in the development branch,
copy tools/orchestrator_belt_docker_migration_gate.py to Belt. Run:

~~~bash
python3 /root/orchestrator_belt_docker_migration_gate.py audit
~~~

It verifies:
- SQLite integrity and schema table counts (includes history)
- exact router identities, WSS/TCP transport/port parity
- registered live Xray UUIDs and both LuCI+SSH reverse listeners/routes
- no unknown/orphaned Xray account or unknown reverse listener

It does not print UUIDs, DB contents, tokens or private keys. On a
discrepancy, STOP and reconcile in staging; do not use Xray regenerate
to "repair" production.

## Stage 1 — sealed rehearsal snapshot (no service modifications)

~~~bash
umask 077
python3 /root/orchestrator_belt_docker_migration_gate.py snapshot \
  --output-root /root
~~~

The tool creates a private mode-0700 directory under /root with
an encrypted-by-filesystem-permissions **not cryptographically encrypted**
mode-0600 tar.gz and a manifest. Keep this snapshot accessible only
to root; for offsite storage use separately verified encryption.
It uses SQLite online backup instead of copying a live .db with WAL.
It contains all regular files in HUB state, Xray JSON and HUB source;
unexpected symlinks, sockets, FIFO, nested SQLite files fail closed.
A backup taken while HUB is running is a **rehearsal snapshot**.
State JSON files can change during the snapshot; its manifest hash
detects changes to a copied file, but it does not offer a transaction
across all separately written JSON files.

Verify the resulting staging directory:

~~~bash
python3 /root/orchestrator_belt_docker_migration_gate.py verify \
  /root/orchestrator-docker-rehearsal-REPLACE_WITH_ACTUAL_DIR
~~~

Do not store archive material or real state in GitHub or Telegram.

## Stage 2 — isolated Docker HUB rehearsal

**Реализация изолированного Docker shadow имеется в ветке разработки и проверена GitHub Actions на синтетической базе шести роутеров (5 WSS + 1 TCP).** Настоящий snapshot Belt пока НЕ запускали в контейнере; на продакшен ничего не переносили. См. [DOCKER_HUB_SHADOW_REHEARSAL.md](DOCKER_HUB_SHADOW_REHEARSAL.md). Контейнерная проверка не подтверждает LuCI/SSH или всё функциональное соответствие v119.

- Build a pinned, dependency-complete Docker image from the **exact
  already deployed and approved HUB Python revision**, not the whole
  experimental branch. The source SHA256 must match installed Belt.
- Start a shadow HUB on an isolated SQLite snapshot and copied state,
  e.g. loopback 18088. The copy is disposable; do NOT configure it to
  accept the real router heartbeat or run its Xray update API.
- Validate login, menus, history, delegated users, push/auth
  configuration and read-only API/UI responses with synthetic requests.
- Do not mount the host Docker socket, live state, /var/run/systemd or
  real TLS private keys into the isolated test container.
- A Docker HUB that needs local reverse ports can later use Linux host
  networking for the controlled cutover. In host mode, ports are
  already host listeners: do not combine network_mode: host with
  published port mappings.
- Start the container in HUB-only mode with
  OWRT_REMOTE_XRAY_CONTROL_BACKEND=external_readonly. This disables
  Xray restart/reload from the HUB, including accidental UI actions;
  operator-managed Xray remains under the existing host systemd unit.
- The updater, VPS-terminal and any other actions using systemd or
  host shell operations need their own Docker-specific capability
  adapter or a clear disabled state before the migration is considered
  feature-complete. Do not imply original parity without testing them.

## Stage 3 — guarded HUB-only cutover (FUTURE, NOT YET IMPLEMENTED)

**First, pass stage 0-2 and write a tested fail-safe coordinator.**

Preflight requirements:
- Out-of-band VPS SSH/console independent of HUB is working.
- A complete shutdown-consistent backup is created immediately before
  handover, after writes are quiesced; DB and JSON files are aligned.
- New container uses the same checked source and remains schema
  backward-compatible with the host rollback version.
- Old and new processes target the **same live bind mount**, and
  precisely one of them may run at a time.
- Nginx host config candidate is tested with nginx -t, keeping
  the WSS location and all other vhosts identical.
- Service and reverse baseline acceptance probes cover ALL routers
  including LuCI HTTP, SSH banner, heartbeat and WSS/TCP connections.

Expected cutover:
1. Keep host Xray process and host Nginx unchanged.
2. Stop ONLY the old HUB host service; do not stop Xray.
3. Start the reviewed Docker HUB using the original live state tree
   bind-mounted RW and the current host network (for loopback
   LuCI/SSH reverse ports). Bind to a separate local HTTP upstream.
4. Confirm /health, auth, complete router inventory, historical
   DB counts, existing sessions and remote LuCI/SSH.
5. Switch ONLY the HUB upstream in host Nginx after nginx -t.
   Do not touch WSS routing or SSL, and do not change router configs.
6. Run the whole acceptance matrix. If any mandatory item fails,
   roll back by stopping container HUB, bringing back old HUB over
   the same live state, and restoring the original Nginx upstream.
   Never restore the old snapshot over new writes by default.

There may be a **short web/heartbeat control-plane interruption**
during HUB handover. The existing Xray tunnels are a separate
systemd service and continue to carry LuCI/SSH. Do not claim a
perfectly seamless HTTP/WebSocket session or unchanged SSH terminal
process across a HUB process restart.

## Stage 4 — separate Xray-to-Docker migration

Only after a successful soak period of HUB-only mode:
- Run a pinned Xray core version identical to the old server binary;
  preserve exact JSON/UUIDs/ports and all reverse tags.
- A container cannot bind TCP/18443, loopback/18444 and all the
  per-router reverse ports while host Xray owns those same sockets.
  Use an explicitly timed, supervised handover with rollback; do not
  run both on the same listener.
- Existing reverse TCP and WSS transport sessions may reconnect.
  Absolute zero-dropped-flow guarantees are not possible with a
  straightforward stop/start. A separate parallel reverse-path
  architecture would be needed for make-before-break.
- Keep the old systemd service unit and Xray binary available for
  immediate reversal until all six routers are accepted.
- Nginx and TLS can continue host-managed forever; moving them into
  Docker is OPTIONAL and a separate third migration.

## Completion gates for production launch

- Every old HUB operation has an explicit systemd vs Docker runtime
  compatibility test. Original v119 workflows remain usable or the
  release is not accepted as final.
- Real migration rehearsal from a recent private Belt snapshot,
  running exactly the pinned Docker image in a fully isolated
  environment, exercises positive and negative cases.
- DB counts, schema, sensitive state-file byte hashes and router
  identities are checked before/after. All history remains queryable.
- Failure-injection tests: missing volume, concurrent writer attempt,
  readonly state, wrong image/source SHA, bad Nginx, broken Xray
  connectivity, rollback after live DB changes.
- No update to Podkop/Forkop/Tachyon or OpenWrt client is required
  by the Docker HUB cutover.
- Maintenance hold remains unless a separate reviewed, explicit
  management transition is approved.

**Belt is NOT ready for a full Docker cutover solely because the
migration audit tool passes.** A working container runtime, a tested
single-writer cutover coordinator and restore drill are mandatory.
