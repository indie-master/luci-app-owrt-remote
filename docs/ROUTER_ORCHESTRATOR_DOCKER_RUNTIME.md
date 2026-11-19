# Router Orchestrator Docker runtime — development candidate

**STATUS: DRAFT / NOT APPROVED FOR BELT CUTOVER.**

The current Belt production remains a host systemd HUB using /opt/owrt-remote
and /var/lib/owrt-remote. It is **not** migrated by adding these files.
The existing five WSS routers plus one legacy TCP must remain untouched.
Do not run the fresh installer on Belt while owrt-remote.service is active.

## What this change provides

- An opt-in source-verified Docker image and a host-network HUB-only Compose
  profile. Host network preserves 127.0.0.1 Xray reverse LuCI/SSH portals.
  It never exposes a public HUB port: OWRT_REMOTE_BIND=127.0.0.1.
- A clean-state interactive installer; a noninteractive invocation exists for CI.
- Read-only/offline state backup through SQLite's backup API and all state files;
  integrity-checked restore to a **new** directory, never implicit overwrite.
- A separate host adapter prototype with peer-UID-gated socket and a read-only
  Xray/status/metrics API. An opt-in Xray restart requires the maintenance hold
  to be absent and is rate-limited. The adapter is NOT yet wired to the HUB.
- External Docker lifecycle ownership: HUB source updater is intentionally
  blocked in Docker mode in the accompanying source code change.

## Fresh install only

Use a pinned, reviewed Git checkout in /opt/router-orchestrator/release,
and run as root:

~~~bash
git rev-parse HEAD
sudo sh docker/install.sh
docker compose ps
python3 docker/manager.py status
~~~

The installer refuses an active owrt-remote.service, an existing .env,
existing data directory and occupied port. It requires Docker Compose.
Only the HUB is installed: TLS/Nginx, Xray and router enrollment are NOT
automatically configured. This is not an internet-facing deployment.

The standard state path is /opt/router-orchestrator/data.
Set .env RO_IMAGE, RO_STATE_DIR, RO_PORT, RO_PUBLIC_URL,
RO_XRAY_BACKEND=external_readonly (the only approved backend here).
.env is local and must be mode 0600. Do not commit it.

~~~bash
# Only after stopping the Docker HUB cleanly:
sudo docker compose stop hub
sudo python3 docker/manager.py backup
sudo python3 docker/manager.py restore /opt/router-orchestrator/backups/FILE.tar.gz \
  --destination /opt/router-orchestrator/restore-rehearsal
~~~

Never use a rehearsal snapshot as routine rollback after live writes.
Use the existing single-writer migration and parity gates documented in
docs/BELT_DOCKER_DATA_CONTINUITY_GATE.md.

## Explicitly not complete

- No live cutover coordinator or automatic change to host Nginx upstream.
- No container-to-host Xray apply (regenerating JSON/restarting real routers
  without parity checks is prohibited). Xray button is disabled by default.
- VPS terminal in Docker must NOT be mistaken for a host shell. The source
  now blocks that action to avoid opening an unaudited container shell.
- Host adapter has no terminal transport and is not automatically enabled.
- Metrics are not yet wired; disk may represent container filesystem.
- Web Push, LuCI/SSH, external API and authenticated sessions must be retested
  with exactly matched source and live snapshot before any production cutover.
- A guarded CLI upgrade/rollback switcher exists for Docker-only installations.\n  It preserves current live state, snapshots after stop and reverts the image on\n  failed health. It has NOT been exercised against a real fleet or certified\n  for database schema rollback; the operator must explicitly acknowledge risk.
- Dependencies have exact version pins but not per-wheel hashes/SBOM.
- Production TLS/cert ownership, unattended renewals and separate Xray
  container migration are OUT OF SCOPE of this first HUB-only draft.

## Production cutover prerequisites

1. Verify private, offline backup/restore with complete state, Xray and
   Nginx/systemd service metadata.
2. Test a source-exact image from the actually installed Belt HUB,
   not merely a development branch's v119 code.
3. Test all five WSS router LuCI/SSH paths and history/heartbeat; report
   AX3000T as known degraded, not a new migration regression.
4. Implement and test Xray apply, VPS terminal, updater and host resources
   without giving the HUB Docker socket, privileged mode or host root mount.
5. Implement a fail-safe single-writer handover / rollback coordinator.
6. Prove schema rollback compatibility, push, auth, re-login, backups and
   failure injection. Only then perform a supervised operator cutover.

See docs/DOCKER_HUB_SHADOW_REHEARSAL.md and
docs/ROUTER_ORCHESTRATOR_ARCHITECTURE.md for existing tests/architecture.

## Docker-only upgrade / rollback (NOT a Belt handover)

After reviewing and checking out a new immutable commit, run:

~~~bash
sudo python3 docker/manager.py upgrade --acknowledge-schema-risk
sudo python3 docker/manager.py rollback --confirm
~~~

Upgrade builds a source-hashed image from a clean Git tree, stops only the
Docker HUB, makes an offline state backup, switches the image reference and
checks /health. A failed check switches back the old image **on the current
state**, not the old snapshot. Rollback uses the retained previous image.
Schema backward compatibility is NOT guaranteed; a container may exit after
modifying the DB. Both operations are blocked while the old host HUB runs.
An image health check is not router-level acceptance, and operators must
retain the old image until an independent LuCI/SSH/heartbeat matrix passes.
