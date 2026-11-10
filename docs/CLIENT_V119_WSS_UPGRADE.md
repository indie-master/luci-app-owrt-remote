# Client v119 — staged upgrade from TCP to WSS

## Provenance

This fork's feature branch is based directly on the original
kzolotarev95/luci-app-owrt-remote v119, commit
5956daa8c26915bb18ff77e18c7339c1f1f74791 (27 October 2026).
The client package retains original-v119 OpenWrt agent, LuCI/CGI interface,
service init, WAN helpers, logging and Xray boot-cache logic.

The original agent targets the yichya/openwrt-xray 26.10.21 release, which
is a separate version from OpenWrt Remote v119.

The fork installer and Hub's router updater default to downloading clients
from this feature branch, not upstream main. An explicit RAW_URL overrides the
installer source. Do not run the upstream install.sh by accident: that will
overwrite the WSS support with a TCP-only client.

## Upgrade existing routers before changing transport

- The installer preserves existing /etc/config/owrtremote without overwriting it.
- Missing vps_transport defaults to tcp, so updating an old agent does NOT
  automatically change its UUIDs, reverse tags, or connection transport.
- Agent can render both TCP and WSS/TLS. WSS has CA certificate verification,
  TLS SNI, HTTP/1.1 WebSocket and scoped Freedom finalRules for LuCI and SSH.
- Installer writes each component to a temporary sibling before atomic rename.
  It does not transactionally install all files; keep a separate backup.
- Original installer reloads uhttpd/rpcd and logs, but does not intentionally
  restart the primary reverse tunnel itself.
- Xray bootstrapping reuses a valid configured binary; it will not silently
  upgrade every working executable. Check that the selected binary supports
  the WSS and finalRules syntax before attempting migration.

The fork installer refuses a potentially dangerous case: if a manually
WSS-patched router is running a WebSocket Xray configuration while its UCI
still lacks vps_transport=ws, installation aborts before changing files.
First inspect the currently working WSS client JSON, then record its already
working settings in UCI (without restarting the tunnel):

    # ONLY on an already-working WSS router
    uci set owrtremote.main.vps_transport='ws'
    uci set owrtremote.main.vps_sni='YOUR_VERIFIED_TLS_DOMAIN'
    uci set owrtremote.main.vps_ws_path='/owrt-ws/'
    uci commit owrtremote

Keep the existing vps_port=443, UUIDs, reverse tags, vps_host and xray_bin.
Do NOT run these commands on a legacy TCP router.

On one pilot **legacy TCP router**, back up its UCI file, current agent,
rendered client JSON and the Xray binary selection, then install the fork
client without changing the transport:

    wget -O /tmp/owrt-v119-installer.sh \
      'https://raw.githubusercontent.com/indie-master/luci-app-owrt-remote/feature/wss-reverse-v119/install.sh'
    # First pilot: do not install/change the Xray core or optional WOL packages.
    OWRT_REMOTE_SKIP_XRAY_PREPARE=1 OWRT_REMOTE_SKIP_WOL_PACKAGES=1 \
      sh /tmp/owrt-v119-installer.sh

For this first pilot, both environment switches leave the currently installed
Xray executable/cache and optional WOL packages unchanged. They do not disable
updating the agent, init scripts or GUI; uhttpd/rpcd may reload. Back up
the router's settings and management scripts before running the installer.
The current HUB maintenance hold must stay active while the deployed HUB
still defaults to restarting Xray on WAN-reconnect. The source branch now
defaults this behavior to disabled, but the already-installed HUB must be
updated separately before that change takes effect.

Verify the installed agent, settings, Xray binary version, outbound
streamSettings and LuCI/SSH access from Belt. The router must still use TCP.
Only after this pilot succeeds should you consider Hub's existing
sequential "Обновить все роутеры" updater. It also defaults to the fork's
v119-based installer; do not update every router at the same time.

## Staged transport migration on Belt

The server can temporarily authorize a single migrating router on both the
legacy TCP inbound and the WSS inbound using the same two reverse UUIDs.
This is a staging state and must be removed after the migration.

1. Upgrade the router client on its existing TCP link, verify that TCP still
   works, and confirm the router's core supports WSS and finalRules.
2. On Belt, with the NEW v119+WSS Hub already safely deployed, **stage**:

    python3 /opt/owrt-remote/owrt-remote-hub.py set-router-transport --id YOUR_ROUTER_ID --transport ws --stage --sync-xray

3. Check that the old TCP reverse link continues to work. Configure WSS on
   that ONE router (port 443, correct TLS domain and /owrt-ws/), preserving
   the UUIDs and Xray path, validate client JSON and restart that router's
   tunnel in a maintenance window.
4. Verify both LuCI and SSH through the WSS reverse connection on Belt.
5. Only after WSS is working, **finalize** and remove the legacy fallback:

    python3 /opt/owrt-remote/owrt-remote-hub.py set-router-transport --id YOUR_ROUTER_ID --transport ws --finalize --sync-xray

If WSS fails, restore the router's TCP UCI config locally and then revert
the Belt record:

    python3 /opt/owrt-remote/owrt-remote-hub.py set-router-transport --id YOUR_ROUTER_ID --transport tcp --sync-xray

**Important:** Server-side TCP fallback does not automatically recover the
router's own UCI configuration if WSS cannot establish a connection. Keep an
out-of-band administration path or test a local rollback/watchdog in advance.
Do not assume this is a zero-downtime or one-click remote migration.

A special --direct --sync-xray mode exists exclusively for a router whose
WSS reverse connection already works but whose old Hub DB still says TCP.

## Deployment status

These changes exist in a draft Pull Request based on the original v119.
Synthetic tests and a Belt snapshot validation do not mean the new Hub or
router package has been deployed. Live Belt, Netis and other routers remain
unchanged by repository edits.
