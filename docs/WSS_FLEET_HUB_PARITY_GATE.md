# Router Orchestrator: fleet WSS snapshot and next release gate (2026-11-10)

Status: **development branch only; no automatic production rollout**.

## Confirmed operator observations

The Belt WSS reverse endpoint is currently
`sprt-wrt.cdnvc.xyz:443` (TLS/WebSocket, path `/owrt-ws/`),
proxied by Nginx to dedicated Xray inbound
`owrt-remote-wss` at `127.0.0.1:18444`. The original
`owrt-remote-vless` TCP/18443 inbound remains available.
Four routers have successfully completed the one-device-at-a-time
WSS pilot: `Netis_N6_router5`, `WR3000S_2`, `main`,
`PaxoBs`. Their two reverse identities per router are registered
on WSS. `PaxoBs` returned HTTP 200 for LuCI/18110 and a Dropbear
banner for SSH/19110, with eight established WSS connections on Belt.
`main` and `WR3000S_2` have reported CONFIRMED WSS. The final
end-to-end audit should always be repeated after subsequent changes.

`AX3000T-Shteinberg` remains **deliberately deferred** and must
not be migrated, restarted or force-updated as a side effect of the
other four routers. All transport modifications require local or
independently tested recovery access.

Xray 26.11.9 (OpenWrt) prohibits VLESS/none to public endpoints;
this was the cause of a stopped reverse instance on `main` before
the WSS switch. Restoring old TCP files with a modern core is *not*
a sufficient remote recovery guarantee. Do not use an automatic
fleet-wide transport migration.

## High-priority risk after the pilot

The operator manually appended the existing per-router TCP UUIDs
to the WSS inbound on Belt. The HUB's SQLite database is its own
source of truth for generating server Xray configurations. A later
HUB `render-xray`, add/delete router, reinstall or Docker cutover
could lose those WSS identities unless DB transport state and
generated Xray parity are reconciled first.

**No automatic DB write is authorized merely by a successful
WSS client connection.** Back up the live DB consistently, never
copy an active SQLite database with plain `cp` in WAL mode, and
do not print/upload raw UUIDs, tokens, hub.db or generated Xray.

## Read-only fleet drift check on Belt

Retrieve an immutable commit containing
`tools/belt_fleet_config_audit.py`, then run:

```bash
python3 -m py_compile /tmp/belt_fleet_config_audit.py
python3 /tmp/belt_fleet_config_audit.py \
  --db /var/lib/owrt-remote/hub.db \
  --xray /etc/xray/owrt-remote.json \
  --expect-wss Netis_N6_router5 WR3000S_2 main PaxoBs \
  --expect-tcp AX3000T-Shteinberg
```

The script reads SQLite in URI `mode=ro`, checks per-router
HUB transport/port against explicit operator-confirmed WSS/TCP
states, both per-router VLESS identities across server inbounds,
LuCI/SSH reverse port mapping, and routing rules. It redacts all
UUID values. A missing HUB transport field or stale DB state
causes a **nonzero exit**. It never modifies DB, config, services,
kernel state or router settings.

Dual registration in TCP and WSS is *not proof of live WSS use*.
The operator must explicitly supply `--expect-wss`; the check
does not claim to observe actual live transport, process reload
or health. Re-run Belt-side LuCI/SSH probes separately for
maintenance acceptance.

If audit finds stale DB fields: STOP. Do not call `render-xray`
on production, do not clear `.xray-maintenance-hold` or run
`vps/install-vps.sh` until a separate SQLite snapshot and
reviewed transport reconciliation plan exist.

## Next implementation milestones

1. **Inventory reconciliation.** Compare HUB DB versus live Xray and
   implement separately reviewed, backed-up, reversible updates to the
   minimum required DB transport fields. Preserve UUIDs, reverse tags,
   individual LuCI/SSH ports and legacy TCP fallback.
2. **Generic guarded router updater.** Bring the current synthetic
   ten-file bundle transaction through real mixed-fleet hardware
   canaries. Do not enable legacy `Update all` or touch the router's
   Xray binary/config on a UI update.
3. **Docker staging of the *existing* full HUB**, not a rewrite.
   The separate `docker-edition` branch is a candidate reference,
   NOT proof of compatibility with the current multi-WSS fleet.
   Run parallel staging on `127.0.0.1:18088`, with isolated
   copied state and *no* public Xray/443/18443 binds, Nginx
   takeover, or production SQLite writes. Keep production
   host-HUB (`127.0.0.1:8088`), Xray, Nginx, DB and backup untouched.
4. **Typed management API + UI**, preserving the original v119
   capabilities and wire identifiers. The optional Telegram bot
   uses the same authorized API, default disabled. Podkop,
   Forkop and Tachyon adapters follow capability gating.

Retain `feature/indie-branding`, the original upstream MIT
license and copyright, and existing per-router installation URLs
until explicit release approval.
