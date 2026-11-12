# New OpenWrt router: safe WSS/443 enrollment on Belt

Status: staged development branch, **not a production auto-deploy**.
The proven Movietonn / Xiaomi AX3000T registration (Nov 2026) showed
why creating a router in HUB SQLite alone is insufficient: while a HUB
`.xray-maintenance-hold` is present the automatic server Xray refresh
is intentionally blocked. The newly created router is saved, but both
WSS VLESS IDs and reverse LuCI/SSH tunnels can be absent on the
live Xray service.

## Operator-gated single-router enrollment

`tools/belt_new_wss_enroll.py` is generic: it does not hardcode a
router model, ID, IPv4 address, UUID, or fleet size.

1. The router must be already created in HUB SQLite, with
   `vps_transport=ws`, `vless_port=443`, and `vps_allow_legacy=0`
   (fresh WSS router rather than TCP migration).
2. Default invocation with `--router-id SOME_NEW_ID` is a
   dry-run. It backs up the live SQLite into a private root-only
   temporary directory, renders the exact server Xray config through
   the **installed HUB Python** with isolated state, and tests it
   with the installed Xray binary.
3. It strips **exactly two** WSS identities and two new reverse
   listeners/rules from the rendered candidate, then requires the
   remaining normalized JSON to equal **all** of the currently
   running Xray's configuration. This catches any unrelated DB drift,
   legacy TCP fallback changes, missing existing routers, altered
   UUIDs/ports, and unsafe Xray regeneration.
4. An already registered router is reported as a no-op and does not
   cause any Xray service restart.
5. The explicit `--install` uses a private backup, preserves or
   creates `.xray-maintenance-hold`, changes only the dedicated
   Xray JSON and restarts only `owrt-remote-xray.service`.
   On restart or listener health failure, it attempts to restore
   the original config and service. It never touches Nginx, HUB
   SQLite, or any router. The maintenance hold stays in place.

The script must be executed on Belt as root, with
`belt_wss_db_reconcile_stage.py` in the same directory.
It does not print or publish credentials. Its private staging files
**contain server UUIDs**, so never send them to clients.

Example for a hypothetical **new, not yet registered** router:

```bash
python3 tools/belt_new_wss_enroll.py --router-id NEW_ROUTER_ID
# Review "PREFLIGHT: PASS", Xray syntax, and unchanged-fleet parity.
# Only when the production change window is approved:
python3 tools/belt_new_wss_enroll.py --router-id NEW_ROUTER_ID --install
```

Do not run `--install` for Movietonn; it has already joined WSS.
Before use on the production server, review the exact commit and
confirm tests succeeded. Check online state, LuCI HTTP, SSH banner,
heartbeats and already-connected routers after the dedicated Xray
restart. The script does not prove the *new* client is online: that
requires installation and first successful connection.

## Client-side installation

A new clean OpenWrt router uses the pinned fresh-only
`tools/router_fresh_install.sh` followed by a **personal**
`OpenWrt config` export. Client firmware and hardware architecture
must pass bootstrap preflight. Do not run it on an existing managed
router. The exported UCI host is now a **bare hostname**, while
`hub_url` and `public_url` remain HTTPS URLs.
The HTTP authentication token exposed by the current UCI export is
global, not a per-router secret, so do not publish full config or
token-bearing installer artifacts in a public repository.

## Next step: one-command onboarding

Implement this after the above manual operator gates are proven:

1. Owner-only action `Подготовить установку` on a newly registered
   router in the Remote HUB UI. Check explicit WSS readiness.
2. Issue an expiring, single-use, random token (only its digest stored
   server-side). Bind it to router ID, provisioned credentials and
   first-use state. No shared HUB bearer token in the public script
   or an unauthenticated endpoint.
3. Client copies one short HTTPS command. The public bootstrap
   checks OpenWrt, version pin, architecture, prerequisites and
   existing agent. The token authorizes a separate private config
   retrieval over TLS; only then the client activates its reverse
   tunnels. Use explicit revocation and recovery for failed installs.
4. HUB confirms both LuCI and SSH over reverse channels plus
   heartbeat. Do not mark success just because the installer exits 0.
5. No fleet-wide update, Docker migration, or removal of the
   existing `.xray-maintenance-hold` is implied by this plan.

The current generic Belt WSS enrollment tool is the safe foundation
for that workflow, **not yet a clickable one-time install link**.
