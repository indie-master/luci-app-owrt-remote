# Non-regression contract: retain **all** original OpenWrt Remote HUB v119 functionality

Status: mandatory architectural requirement, draft branch only. This is an **additive fork** of [kzolotarev95/luci-app-owrt-remote](https://github.com/kzolotarev95/luci-app-owrt-remote) under MIT, with the original copyright/license retained.

Baseline: source snapshot `upstream-v119`, commit `5956daa8c26915bb18ff77e18c7339c1f1f74791` (2026-10-27). Existing bespoke fork enhancements and currently deployed legacy client behavior are also protected. The product is **Router Orchestrator** and web title **Remote HUB**; internal wire identifiers may remain `owrt-remote` / `owrtremote`.

## Rule

Keep the original HUB/agent source and its established behavior as the **system of record**. Docker packaging wraps the same proven services and volumes rather than replacing their semantics. Web UI restyling must not remove existing routes or controls. A Telegram bot calls the shared, permission-controlled existing operation layer; no duplicate implementation with subtly different behavior.

**All** original functions and user workflows remain supported, including obscure/optional features not individually listed here. The table below is an acceptance inventory, not permission to remove unlisted features. Never delete or disable working original functionality for simplicity, memory savings or new design.

## Protected capability inventory and acceptance

| Area | Functions to retain | Acceptance evidence |
|---|---|---|
| Router enrollment | Register, edit, group, delete with validation; original SQLite identity and UUID, ports, generated settings | Import real DB snapshot; no silent UUID/port/reverse-tag change |
| Connectivity | TCP VLESS `none` reverse LuCI/SSH, current WSS add-on, Nginx and remote reachability | Live LuCI HTTP and SSH banner probes via correct per-router ports; failed router explicitly maintenance-skipped only |
| Status | Online/offline, heartbeats, router and VPS health, status updates, last-seen | Original UI/API still report correct state; offline distinguish from unreachable apps |
| Web interfaces | Dashboard/cards, groups, original navigation, LuCI links, browser SSH terminal, mobile screens | Manual UI smoke and automated contract tests where practical |
| Authentication & security | Existing password, 2FA/TOTP, passkey, SSH ED25519, linked login providers, users/permissions, HTTPS/CSRF protections | Existing configured login methods continue to operate; new Telegram users default deny |
| Operations | Existing CLI and Hub actions, per-router configuration, VPN/Xray operations, diagnostic commands, Xray config validation | Legacy command/endpoint compatibility; no unintended Xray stop/reload/core replacement |
| Network | WAN status/selection, DNS, interface monitoring, failover/recovery, firewall/network safety and existing helpers | Network routing and remote tunnel stable through update, reconnect and router reboot |
| Monitoring | CPU/RAM/storage/traffic counters and existing per-client traffic views; logs and log archive/collection | UI and API fields remain available; bounded resource collection |
| Device tools | Original Wake-on-LAN and any existing supported router controls | Existing WOL actions continue to function even without new bot |
| Updates | Existing HUB self-update and router update capability, version checking, staging, existing safeguards | Legacy functionality retained; unattended apply gated until safe canary rollout and rollback |
| OpenWrt lifecycle | Original CGI/LuCI, rpcd ACL, init/procd, hotplug, heartbeat, logging/wan helper and Xray bootstrap/cache | Both installed old clients and upgraded ones keep working; no surprise package changes |
| Settings | UCI, persisted DB, secrets, custom routes, TLS/WS metadata and per-router overrides | Byte-stable existing configs unless an explicitly approved migration applies |
| Documentation | Original troubleshooting, installation and upstream provenance | Link to original maintained; never silently redirect upgrades to the wrong branch |

## Source and compatibility policies

1. Never replace `vps/owrt-remote-hub.py`, `files/usr/sbin/owrt-remote`, `files/www/cgi-bin/owrt-remote` or their services with a clean-sheet rewrite merely to add the bot, Docker or new branding. Any required edits must be isolated, backward compatible and reviewed against the upstream v119 snapshot.
2. The upstream feature inventory is a **minimum**. Fork-specific original behavior and live user settings, including the legacy `main` router, must be preserved too.
3. Version/capability negotiation supports a new HUB with old clients and a new client with the previous HUB for their mutually supported operations. Unsupported capabilities are hidden/disabled rather than attempted.
4. New modules (Telegram and Podkop/Forkop/Tachyon adapters) are opt-in and should be isolated from the original reverse tunnel, Xray service lifecycle and router WAN/DNS unless the operator explicitly runs a guarded action.
5. Docker migration is an infrastructure packaging change, not permission to rotate credentials, change ports, regenerate Xray JSON or drop original UI workflows. Retain durable DB/backups/config and a proven exit route back to host systemd.
6. UI rebranding changes **visible labels, style and destination links** only. It does not rename UCI keys, runtime service identifiers, database tables, Xray tags, reverse UUIDs or API routes.
7. Any import/upgrade is reversible with a private snapshot and documented rollback. A failed install must not leave partial files or a disabled tunnel. Original core/updater remain pinned until exact system tests pass.
8. Resource optimizations should remove overhead newly introduced by this fork, not disable the original monitoring, notifications, login or operations features.

## Explicit gates for the first router pilot: `main`

Operator read-only preflight 2026-10-28 reported `PREFLIGHT: READ-ONLY PASS`: router_id `main`, label Главный роутер, TCP/18443, two reverse TCP-none outbounds, Xray 26.3.27 ARM64, `owrt-remote`, `rpcd` and `uhttpd` running, /overlay 57.2 MiB available, /tmp 181 MiB available, currently installed router UI `2026-07-02-mobile-terminal-batch-v25`.

Original hashes (never edit the original or upload its contents/keys to the public repository):

- UCI: `8b8a633ccc85ffe88e2459164f3fd6875588e13507b4c24e480d6944a8df0ea5`
- Agent: `53e9fda04b55be07622516f24bb6eca1efac1bf6069ca620bebb6bfb12d5c080`
- Init: `526a34d1d7be76aabe31b80e3d6a7b6f53645ae7a8b83d7f4f62306690db1317`
- Client JSON: `b014e28bceb25d278605f6abdc7b511a5e09ef62b9c95a712a5c6481a30c03bc`

**This is only baseline evidence, not approval to run an update.** The current `install.sh` mutates multiple files one at a time and restarts rpcd/reloads uhttpd; there is no complete durable watchdog rollback yet. The pilot requires complete pre-download and verification, private snapshot, local rollback mechanism armed before mutation, one-router update **without touching Xray-core/WOL**, and verified Belt-side HTTP/SSH before acknowledgment. Preserve TCP none and hold on Belt.

## Review and CI expectations

- Run source regression tests and functional tests against the original v119 code and live-backup redacted fixtures.
- Record the source commit, input DB/config digests, unchanged/changed files, service and transport state before/after.
- Fail CI on accidental removal of original entrypoints, UI views, login options, endpoints, CLI commands, update handlers and event hooks.
- Test explicit old-HUB/new-client and new-HUB/old-client matrices plus both TCP and WSS; do not equate Xray syntax success with actual reverse LuCI/SSH.
- Test missing downloads, overlay-full/low-Memory, failed service restart, connection loss, router reboot and watchdog restoration.
- No production merge, auto-deploy, self-update, maintenance HOLD removal, Belt Xray upgrade or fleet upgrade from this draft.

**Principle:** extension should fail closed, while the original installed functions keep working.
