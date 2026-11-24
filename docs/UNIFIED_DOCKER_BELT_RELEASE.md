# Router Orchestrator Docker release: Belt migration and acceptance

**Status:** release candidate, not deployed to Belt. Use an isolated Git worktree.
Do not merge or deploy until CI and the six-router acceptance matrix pass.

## Production baseline (2026-11-23)

- HUB: Docker, UID 23088; real VPS terminal accesses `roterm@127.0.0.1`
  through pinned SSH key; `sudo` prompts for the host password.
- Xray: Docker 26.3.27, host network, legacy systemd service disabled and
  retained for recovery.
- Original HUB Compose overlays, host-adapter, production nginx and Remnawave
  remain outside the candidate worktree.
- Five WSS routers and one legacy TCP router, one of which may be offline for
  unrelated reasons. Do not change router credentials during migration.

## Source → candidate (does not touch live containers)

From a root shell on Belt, after reviewing the PR and its green CI:

```sh
git -C /opt/router-orchestrator/release fetch origin \
  refs/heads/feature/router-orchestrator-unified-docker-transports-20261123:refs/remotes/origin/feature/router-orchestrator-unified-docker-transports-20261123
git -C /opt/router-orchestrator/release worktree add \
  -b feature/unified-docker-release \
  /opt/router-orchestrator/src \
  refs/remotes/origin/feature/router-orchestrator-unified-docker-transports-20261123
python3 /opt/router-orchestrator/src/deploy/prepare_belt.py
```

The preparer reads live image names and the old `.env`, checks SQLite
integrity, builds the portable HUB image, backs up `hub.db`, and writes
a standalone candidate Compose + .env to
`/opt/router-orchestrator/unified-candidate`. It does **not** restart
Xray/HUB/nginx or edit the shared nginx configuration.

Do not paste .env, auth state or secret keys into a public issue.

## Refreshing a previously staged candidate

The Belt candidate built at an earlier Git SHA is **not valid** after source
changes. Before applying, fast-forward the dedicated worktree and rebuild
the candidate safely, retaining the previous candidate in a private archive:

```sh
git -C /opt/router-orchestrator/release fetch origin \
  refs/heads/feature/router-orchestrator-unified-docker-transports-20261123:refs/remotes/origin/feature/router-orchestrator-unified-docker-transports-20261123
git -C /opt/router-orchestrator/src merge --ff-only \
  refs/remotes/origin/feature/router-orchestrator-unified-docker-transports-20261123
python3 /opt/router-orchestrator/src/deploy/refresh_belt_candidate.py
```

The preparer now saves both SQLite (online backup) and the other bounded
persistent HUB state (tokens, authentication, Global Logs, preferences).
Production containers are not modified. The guarded cutover refuses a
candidate whose source SHA differs from the current Git worktree.

## Controlled cutover

Only after the candidate image and backup are verified:

```sh
python3 /opt/router-orchestrator/src/deploy/apply_belt.py
```

This installs the allowlisted Host Adapter and Xray core-worker units,
creates the root-level single Compose entry point, and recreates the
HUB and project-only nginx. It **must leave the running Xray container
ID unchanged**. On failed initial checks it restores the old HUB
image/compose and host adapter. The shared host nginx is unchanged.

Use from then on:

```sh
cd /opt/router-orchestrator
docker compose ps
docker compose logs -f --tail 100 hub
docker compose logs -f --tail 100 xray
docker compose logs -f --tail 100 nginx
```

The existing production nginx HTTP vhost may still send requests
directly to HUB :8088 at this point. Separately change only that
specific upstream to `127.0.0.1:18089` after the new project nginx
healthcheck and websocket proxy have been verified. Back up the vhost,
run `nginx -t`, reload host nginx only if valid, and test from an
external browser. If the outer nginx is L4 passthrough using
`ssl_preread`, instead select
`deploy/nginx/embedded-stream-tls.conf` and route the HUB SNI to
`127.0.0.1:18445` with matching certificates. Do not send passthrough
TLS to the plaintext :18089 listener.

On a clean VPS where :80/:443 are exclusively available to this
project, set `RO_NGINX_CONF` to
`/opt/router-orchestrator/src/deploy/nginx/standalone.conf` and
provide TLS certs in `RO_TLS_DIR`; keep nginx for Remnawave separate
on shared hosts.

## Xray core version management

The owner-only `/xray-core/` panel lists stable releases and starts
fixed systemd maintenance jobs. The scripts reside in
`/opt/router-orchestrator/src/docker`.

Command-line equivalents (root only):

```sh
python3 /opt/router-orchestrator/src/docker/xray_releases.py list
python3 /opt/router-orchestrator/src/docker/xray_releases.py prepare
python3 /opt/router-orchestrator/src/docker/xray_lifecycle.py status
python3 /opt/router-orchestrator/src/docker/xray_lifecycle.py restart
python3 /opt/router-orchestrator/src/docker/xray_lifecycle.py rollback
```

The prepare action chooses the latest GitHub non-prerelease, attempts
to pull the matching official OCI image, validates its binary/version
and exact production configuration, and saves a pinned image-ID
manifest. If no matching stable OCI tag is available, it fails closed.
Upgrades are explicit, serialized and compare the image digest with
the manifest. Promote only after the old active routers have recovered
heartbeat and a live SSH banner (at least 80% of the previous online
set); otherwise restore the previous image.

This is a management mechanism, not a claim that every new Xray
release is compatible with older OpenWrt client cores. Canary first.

## Router transport configurator

The owner-only `/transport-configurator/` supports server and client
preview for TCP, WSS/TLS, REALITY/TCP, XHTTP/TLS and gRPC/TLS.
It rejects duplicate/reserved ports, unsafe TLS paths, invalid UUIDs,
and credential leakage. It calls the **current Docker Xray binary** in
an isolated container to validate a proposed server listener.

The configurator also loads the existing router fleet, generates a distinct
shadow reverse proposal with separate loopback LuCI/SSH test ports, and allows
an owner to run `owrt-remote transport-preflight` over SSH on a selected
router. That check uses the installed OpenWrt Xray binary and NEVER changes
its UCI settings or running tunnel. The router must first have the new agent
version installed. Shadow listener staging, pilot promotion, and automatic
router-side rollback are NOT yet implemented.


**Applying a transport to a real router is intentionally disabled.**
The current OpenWrt agent knows only TCP and WSS migrations. A new
reverse-bridge canary and durable fallback protocol are required before
REALITY/XHTTP/gRPC should be deployed to customer routers. Do not
manually paste preview-only client config as if it were a complete
remote-management config.

## Guarded experimental transport shadow stage (not production promotion)

The transport mini-panel now supports server and router-client preview,
per-router shadow reverse tags and tests, and a remote OpenWrt
`transport-preflight`. The agent additionally contains a separate
`transport-pilot-start` process with a 180-second automatic stop, preserving
current UCI and the original procd instance.

The root-only server transaction `docker/transport_stage.py` is deliberately
separate from ordinary Xray config reload. It adds a full new ingress plus
separate loopback LuCI/SSH reverse portals; it retains original inbounds
and routes byte-for-byte, validates with the active Docker Xray core,
backs up the exact original config, and checks that existing routers
recover after the unavoidable Xray process restart. It will roll
back on failure or after a 600-second lease; an independent systemd timer
also expires it after a reboot.

**This is not a complete transport migration.** The CLI currently
requires an explicitly prepared root-owned profile, a reachable pilot
ingress port, and a separately installed updated OpenWrt agent. The
profile must be tested on a noncritical pilot router first. The Web
button to actually change the primary connection stays disabled until
automatic confirmation, transport promotion and router-side recovery
have been demonstrated on real hardware. A temporary pilot must never
be counted as a persistent migration.

Use `python3 docker/transport_stage.py status` for an existing root-only
transaction inspection in a test environment. Never run `stage` on
Belt simply to inspect a configuration: it **restarts production Xray**.

## Post-cutover acceptance

- HUB owner and delegated-account RBAC; no delegated VPS root shell.
- Real terminal login, `sudo -k; sudo whoami` prompts for password.
- WoL on a selected router via password and via key if used.
- All six router records intact; check five expected online and the
  known-offline router separately; verify LuCI/SSH reverse tunnels.
- Global Logs: push, archive size growth, rotation, and delivery ACK.
- Xray same container ID during HUB/nginx cutover; explicit restart
  produces heartbeat recovery; simulated failed core triggers rollback.
- Nginx embedded WebSocket, external HTTPS, correct scheme/Origin.
- Controlled VPS reboot and independent rescue after both services
  recovered. Backups are restorable.
- Only then remove staging containers and review unused project images.

`docker/cleanup.py` defaults to dry-run; apply requires a root-owned
release-acceptance record with explicit fleet/reboot/rollback/WoL/log
successes. Never use global `docker system prune -a` on Belt.

## Remaining blockers

CI validates syntax, offline APIs, nginx configs, Docker build and
static tests. It cannot verify live OpenWrt REALITY/XHTTP/gRPC support,
router agent migration, remote LuCI/SSH access, reboot recovery or WoL
on actual devices. Those must be accepted before release is marked
production-ready.
