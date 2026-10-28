# OWRT Remote: WSS reverse + legacy TCP (experimental fork)

This feature branch is based on upstream v119 (2026-10-27), **not** on the older fork `main` (2026-10-08). Do not install it over a production HUB without reviewing the upstream differences.

## Transport contract

| Router records | Server inbound | Client outbound | Security |
|---|---|---|---|
| Existing SQLite rows, after migration | `owrt-remote-vless` TCP, legacy listener preserved (Belt :18443) | TCP to existing port | Existing behaviour unchanged |
| Newly registered rows | `owrt-remote-wss` on `127.0.0.1:18444` | VLESS WSS on public :443 | TLS verified, ALPN http/1.1, SNI from HUB URL, WS path `/owrt-ws/` |

Both inbounds use the same reverse routing scheme but different client lists. They are **not** exposed via Remnawave; the deployed Nginx HTTPS virtual host terminates TLS and proxies `/owrt-ws/` to `127.0.0.1:18444`. This Nginx location must already exist. No change to Nginx stream, port 443, Remnawave, or existing TCP clients is required.

Each modern WSS router has two restricted Freedom outbounds for LuCI/SSH. `finalRules` allow TCP to the configured ports, additionally pinning a literal IPv4 destination (for example `127.0.0.1/32`). Legacy client settings are deliberately unchanged. New OpenWrt agents support `vps_transport=tcp|ws` and `vps_sni`, with `tcp` as the default for older UCI configurations.

## Source and upgrades

The experimental fork self-update source defaults to `indie-master/luci-app-owrt-remote/feature/wss-reverse-v119`, not upstream. `RAW_REPO_BASE` can override it. This prevents a panel self-update from silently reverting WSS support. Pin a reviewed release branch or commit before production deployment. Docker support lives in a separate `docker-edition` branch and is not deployed here.

## Development checks

```sh
python3 -m py_compile vps/owrt-remote-hub.py
sh -n files/usr/sbin/owrt-remote
python3 -m unittest discover -s tests -p 'test_wss_transport.py' -v
```

See the WSS reverse compatibility GitHub Actions workflow for the CI results.

## Read-only Belt staging (recommended next step)

Use the pinned GitHub feature branch on a separate checkout. **Never install it over the live HUB or launch a maintenance/update command yet.** Running `stage_belt_wss.py` will read the live SQLite DB through read-only mode, create an online consistent backup in a private directory, and migrate **only the copy**. It will then generate a candidate from the new v119 Python source, compare it semantically with the currently working Xray configuration (8 TCP and 2 WSS clients), and run Xray `-test`. It neither writes to `/etc/xray/owrt-remote.json` nor restarts any systemd service.

```sh
set -eu
umask 077
SRC=/root/owrt-remote-v119-wss-check
test ! -e "$SRC"
git clone --depth 1 --branch feature/wss-reverse-v119 \
  https://github.com/indie-master/luci-app-owrt-remote.git "$SRC"
git -C "$SRC" rev-parse HEAD
python3 "$SRC/tools/stage_belt_wss.py" \
  --hub-source "$SRC/vps/owrt-remote-hub.py" \
  --db /var/lib/owrt-remote/hub.db \
  --live /etc/xray/owrt-remote.json \
  --xray-bin /usr/local/bin/xray \
  --output-root /root \
  --simulate-fallback
```

The script does not print real UUIDs, tokens or router credentials. All private output goes to `/root/owrt-wss-v119-*/` with mode 0700/0600. The optional `--simulate-fallback` additionally tests the next planned TCP-to-WSS cutover for one synthetic selection of an existing router **inside the database snapshot only**. It keeps all 8 legacy TCP clients and expects 4 WSS clients (2 existing plus 2 for the migrating router), with identical UUIDs, preserved LuCI/SSH routing and a successful Xray `-test`. This is a syntax/reverse-contract rehearsal, not proof that two simultaneous live reverse sessions can coexist. No production services or files are changed.

Only a completed **`SAFE STAGING PASSED`** and **`Semantic match ... YES`** can green-light the next *manual* deployment planning step. An error means **stop and investigate, do not force an install**. In particular, tests against generated synthetic data are not a substitute for this check with the real five-router database.

For the already-migrated WSS router, the generated OpenWrt UCI export now preserves any previously selected `owrtremote.main.xray_bin` (e.g. the tested softfloat build) rather than blindly reverting to `/usr/bin/xray`.

## Safe migration of Belt (not an unattended installer)

1. Keep the working `/etc/xray/owrt-remote.json`, `/opt/owrt-remote/owrt-remote-hub.py`, `/var/lib/owrt-remote/hub.db` and Nginx configuration untouched until deployment. Make permission-restricted backups and use `sqlite3.Connection.backup()` for a consistent DB snapshot.
2. Run the new HUB generator **against a copy of the DB**, never the live DB. An existing DB acquires `vps_transport='tcp'` for every existing row. In the Belt staging copy **only**, identify the active WSS router from the live Xray reverse tags, then assign `vps_transport='ws'`, `vless_port=443`. The staging tool performs this automatically.
3. Render to a private staging JSON file, not `/etc/xray/owrt-remote.json`. Verify the current TCP listener address/port stays the same, original UUID/reverse client mappings are unchanged, no existing `entry-*` or `ssh-*` tags disappear, and no local listening port collides. On Belt the expected state after staging is **8 TCP clients**, **2 WSS clients**, **10 routing rules**, with localhost `18444` for WSS.
4. Validate the staged config with the **exact Xray binary running on Belt** (`/usr/local/bin/xray run -test -config /path/candidate.json`), test Nginx TLS verification / WebSocket Upgrade, and check that the OpenWrt agent is updated for WSS before moving any existing router.
5. Install the new HUB source **only after separate dependency/version review**. The new HUB generator validates the candidate with Xray before replacing the live file, takes a private backup, and tries to roll back a failed service restart. It reads and preserves the port of an existing `owrt-remote-vless` inbound rather than silently changing the legacy port.
6. Stage the DB transport migration separately: `set-router-transport --id YOUR_WSS_ROUTER_ID --transport ws --sync-xray`. The command preserves both UUIDs and reverse tags; it changes only transport and outbound port, with DB rollback on sync failure. Run it **only when the updated HUB is installed and candidate validation has passed**.
7. After the controlled Xray restart, verify :18443 and localhost :18444 listeners, WSS established sessions and actual LuCI/SSH over `127.0.0.1:18120` and `127.0.0.1:19120`. Verify every other router as well. Do not mistake successful `xray -test` or a listening TCP port for working reverse traffic.

**Never use the legacy `render-xray --out /etc/xray/owrt-remote.json` command from the older HUB while this custom WSS inbound is active.** It will regenerate a TCP-only server configuration.

## New routers after deployment

1. Register through the HUB web form, selecting **WSS / TLS · 443** (default) or explicit legacy TCP.
2. Registration stores distinct UUIDs and reverse tags and attempts a safe server Xray sync; the UI reports a sync error rather than claiming success.
3. Update/install the **fork agent with WSS support** on the new router *before* applying the generated configuration. An old agent does not understand `vps_transport=ws`.
4. Apply OpenWrt config from that router's HUB card; confirm both LuCI and SSH over the reverse tunnel.

Existing router records default to `tcp` during DB migration and no existing router is automatically switched. Manual changes via the transport CLI require an explicit operator action.

## Limitations

- WSS in Xray 26.x is deprecated in favour of XHTTP. It is retained here because the exact Belt/Netis path has been verified operationally.
- An Xray restart temporarily disconnects reverse connections. The upgrade cannot promise zero downtime.
- The base branch has substantial upstream changes since the old installed HUB. A green CI run is **not** equivalent to production approval or a successful migration of the live database.
- This branch is an experiment; do not merge or deploy without staging, backup, and a rollback window.
