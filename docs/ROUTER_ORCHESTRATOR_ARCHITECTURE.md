# Router Orchestrator — Architecture Decision Record

Status: **proposed / development branch only**. This document does not authorize
production deployment or changes to the Belt VPS or any OpenWrt router.

## Naming and provenance

- Product/repository name (planned): **Router Orchestrator**
- VPS Web UI product label (exact spelling): **Rmote HUB**
- Proposed eventual repository: `indie-master/router-orchestrator`
- Current GitHub repository: `indie-master/luci-app-owrt-remote`
- Upstream: https://github.com/kzolotarev95/luci-app-owrt-remote
- The upstream copyright notice and MIT LICENSE stay intact. Clearly credit the
  original author in the README and About dialog. Do not claim upstream authorship.
- Keep legacy identifiers (`owrtremote`, `owrt-remote`, reverse tags, API paths,
  SQLite fields and service names) until a separately tested compatibility migration.
  Branding is NOT a wire-protocol migration.
- Never rename the GitHub repository before all installers, self-update URLs, CI
  references, release manifests and rollback links have been retargeted and tested.

## One backend, multiple interfaces

Both the UI and the opt-in Telegram bot must call the SAME authenticated Hub API
and shared job engine. Neither interface may run arbitrary shell strings or use
unrestricted SSH. Implement a typed, capability-gated operations API with explicit
per-router authorization, idempotency keys, job IDs, status, timeout and audit trail.

Proposed components:

1. `hub-api`: authentication, router registry, inventory, statuses, metrics,
   capability negotiation, RBAC and API.
2. `hub-worker`: serialized per-router operations, staging, snapshots,
   apply/verify/rollback, reconnect monitoring, concurrency limits and audit.
3. `telegram-bot` (disabled by default): separate optional container, polls
   Telegram only when enabled; strict user ID AND chat ID allowlist, scoped roles,
   confirmation for risky operations, rate limit and no secrets in notifications.
   Use token via Docker secret or tightly protected environment source, never
   source-controlled. Require explicit setup; no default credentials.
4. `reverse`: existing Xray TCP none + WSS infrastructure, still isolated from
   configuration-management worker. Configuration changes only through validated
   staging and guarded rollout.
5. `edge`: HTTPS reverse proxy, auth, WebSocket and security headers, keeping
   443 publicly exposed and management APIs private.
6. `router-agent`: tiny OpenWrt shell/ubus/rpcd adapter. Outbound-only control
   channel, local policy enforcement, safe transactions, watchdog, version and
   capability advertisement. Must work on constrained OpenWrt hardware.

### Bot feature parity

The bot must expose every **authorized capability** that the UI exposes, not
a second implementation of the operation. Planned categories:

- Fleet dashboard: online/offline, last heartbeat, tunnel transport, client and
  agent versions, CPU/RAM/disk, uptime, temperatures and WAN link metrics where
  hardware provides them.
- Router: safe reboot, service inspection, network/DNS/interface diagnostic,
  WAN status, route reachability, health and logs with redaction.
- Clients: DHCP leases (historical association), ARP/neighbor state and Wi-Fi
  station list (actual observed association) must be distinguished. Block/unblock,
  parental/scheduled access, per-device bandwidth limits and monitoring only if
  the router advertises and validates the corresponding capability. Resolve
  identity by MAC/lease across interfaces, do not assume DHCP == online.
- Integrations: Podkop, Forkop and Tachyon. Each is a separate versioned
  adapter with detection, read-only status, validated config export/import/diff,
  start/stop/reload/restart and transaction-safe edits. These packages may
  overlap in networking/firewall/DNS; detect incompatible configurations and
  refuse unsafe simultaneous policy control.
- Bot UI: compact inline keyboards and Back; edit status messages where
  appropriate; destructive actions require confirmation with expiring nonce.
  Commands `/start`, `/routers`, `/status`, `/help` are initial read-only scope.

### Authorization / audit

Roles: viewer, operator, administrator. Per-router grants and command-level
permissions. Never allow Telegram usernames to authenticate: use numeric user IDs
bound to a verified administrator, allowlisted chat IDs, optional time-limited
approval for high-risk network edits, and immutable audit events. Do not send
configs, subscription tokens, UUIDs, private keys or full logs to Telegram.

A remote operator must not be able to block its own only management path:
guard the Hub/Belt control channel, router management interface, active
admin device and reverse tunnel endpoints. Emergency rollback remains local.

## Docker-first VPS deployment

Use Docker Compose for `hub-api`, `hub-worker`, optional `telegram-bot`,
reverse Xray and private dependencies, with persistent named volumes and
healthchecks. Existing Nginx can initially stay host-managed during migration;
do not seize 443 or 18443 while production owns them. Pin image versions/digests,
avoid privileged containers, scope bind mounts and secrets, use a non-root
runtime where possible. Keep admin API on private Compose network.

Maintain a migration/restore CLI and consistent SQLite backup; a later
transactional database or PostgreSQL migration is separate. Database/schema
migrations must be additive and reversible during compatibility window.
No auto-application of pulled `latest` images. No simultaneous host-systemd
and Docker Xray binds on production ports. First prove the stack in staging,
then separate phased cutover with tested restoration of old services.

## Router-side installation and compatibility

- Install via versioned, checksum-verified package/bundle (`opkg`/`apk`
  supported where applicable), not an unpinned remote shell pipeline.
- Prefer a stable channel; allow `candidate` for a named pilot; pin rollout
  by immutable build ID. Installation is atomic at bundle level, not just each
  file, with durable A/B or snapshot rollback where storage permits.
- Preserve `/etc/config/owrtremote`, Xray binary choice, UUIDs, reverse tags,
  LuCI/SSH ports, WAN routing, package configurations and secrets. Never
  reset unrelated OpenWrt packages.
- Protocol handshake: `agent_version`, `hub_version`, `api_min`, `api_max`,
  `capabilities`. New Hub + old agent remains functional for existing
  operations; new agent + old Hub remains functional for the negotiated core
  subset. New features require explicit capability support. Test at least
  current and previous two supported minor protocol generations (and deployed
  legacy v119/TCP until deliberately retired).
- No strong guarantee of compatibility with every future/unknown upstream
  package version. Unsupported versions must be rejected safely before apply.
- Management continuity: preserve TCP none by default, no automatic WSS switch.
  All transport changes require staged TCP+WSS acceptance, outside-in LuCI/SSH
  probe and local watchdog auto-rollback on non-acknowledgment.
- Never roll out to multiple routers or upgrade active Xray from a UI cosmetic
  update. Per-device and fleet rollout canaries must have a tested abort path.

## Acceptance gates before any production rollout

1. CI syntax/security tests, API-contract tests, migration tests and signed
   build manifest verification.
2. Old Hub vs new client AND new Hub vs old client tests, including v119 TCP none.
3. Disconnect/failure-injection test: power loss, failed download, invalid
   bundle, interrupted web services, vanished reverse tunnel, DNS outage.
4. Local watchdog reverts unconfirmed change without relying on Belt/Telegram.
5. Exact live router/client UUID and routing tags are unchanged across upgrade.
6. Named canary `main` with local recovery access, then sequential rollout
   with mandatory health acceptance; no fleet automation until validated.
7. Belt existing `.xray-maintenance-hold` and Xray-core remain unchanged
   throughout the current client pilot. Never infer success from LISTEN alone.

## Baseline as inspected on 2026-10-28

The operator reports on Belt: Hub/Xray services active, Xray-core 26.3.27,
PID 350908, TCP 18443, localhost WSS 18444, Hub 8088 listening, HUB health
`{"ok":true}`, and `.xray-maintenance-hold` present. Current 5 routers:
4 legacy TCP, 1 WSS; one TCP router has intentionally stopped Xray.
This is operator-reported status, not independent live verification.
