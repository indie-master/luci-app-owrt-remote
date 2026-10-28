# Belt: guarded v119 + WSS Hub-only deployment

This is a **manual, deliberate** migration, not an unattended update. The
working Xray 26.3.27 executable, /etc/xray/owrt-remote.json, and the
owrt-remote-xray.service unit are not replaced or restarted by this tool.

Prerequisites:
- Existing production already serves 5 routers: 8 legacy TCP clients on
  TCP/18443, 2 WSS clients on localhost/18444 through Nginx, 10 reverse rules.
- Previous offline Belt snapshot and new Xray 26.10.21 syntax tests passed.
- HUB service and Xray service are independently managed in systemd.
- Production access to the VPS terminal does NOT depend only on the Hub itself.
- A maintenance window is available for a brief interruption of the Hub web
  interface. Reverse tunnels remain managed by the independent Xray service.
- The tool assumes the Hub state directory matches its SQLite DB parent.

## One-time source checkout (pinned revision)

Run only on the Belt VPS as root, with enough free private disk space:

    set -euo pipefail
    umask 077
    REPO=/root/owrt-hub-reviewed-v119
    test ! -e "$REPO"
    git clone --depth 1 --branch feature/wss-reverse-v119 \
      https://github.com/indie-master/luci-app-owrt-remote.git "$REPO"
    git -C "$REPO" rev-parse HEAD

Compare the printed revision to the explicitly approved commit in the project
conversation. **Do not run a floating branch revision without reviewing it.**
Keep this checkout intact for rollback or repeatable diagnostics.

## Phase 1: read-only preflight

    python3 "$REPO/tools/deploy_belt_hub_v119.py" check \
      --source "$REPO/vps/owrt-remote-hub.py"

This:
- Checks independent systemd services, health, reverse listen ports and
  currently running Xray PID and SHA-256.
- Checks that the live Hub state directory and SQLite parent agree, and that
  no prior Xray maintenance hold exists.
- Runs a private SQLite snapshot of the **unmodified** production DB,
  simulates the current WSS record migration, renders the exact live Xray
  config and validates it against the installed Xray.
- Simulates TCP+WSS dual authorization for one old router, preserving all
  8 legacy TCP credentials, and validates that syntax against Xray.
- Never stops/restarts any service and does not modify the live SQLite DB or
  working Xray JSON.

Expected: "STAGING: PASS" then "CHECK: PASS. Nothing installed or restarted."

## Phase 2: controlled Hub-only application

Do not run until phase 1 and a separate recovery/SSH path both pass:

    python3 "$REPO/tools/deploy_belt_hub_v119.py" apply \
      --source "$REPO/vps/owrt-remote-hub.py"

The updater:
1. Rechecks the entire preflight before touching production.
2. Stops ONLY the owrt-remote Hub service.
3. Takes a consistent SQLite backup, the previous Hub code, Xray executable
   and JSON configuration snapshots, and both systemd unit descriptions
   under a private mode-0700 directory in /root.
4. Adds vps_transport and vps_allow_legacy columns, identifying the already
   working WSS router from the current live Xray reverse tags. Changes only
   its DB transport to WSS/443; the other four routers stay TCP/18443, and
   both UUIDs, reverse tags, LuCI and SSH ports remain unchanged.
5. Regenerates Xray JSON to a PRIVATE candidate file and checks that it is
   semantically identical to the live configuration. Does not install it.
6. Atomically replaces only /opt/owrt-remote/owrt-remote-hub.py.
7. Starts only the Hub service, checks the exact new Python revision via
   /health, verifies the original Xray PID and executable/JSON SHA-256 and
   both reverse listening ports.
8. Leaves /var/lib/owrt-remote/.xray-maintenance-hold in place: automatic
   Xray reload/restart (even after WAN reconnect) is blocked in the new Hub
   until the operator validates live TCP and WSS and explicitly releases the
   hold. Router registration that requires an Xray reload may return an error
   while maintenance hold is present.

The command prints the private "Rollback snapshot" path. Record it locally.

## Phase 3: verify live reverse traffic (not just open ports)

Use actual LuCI/SSH probes on the known WSS router, then separately inspect
the known reverse LuCI/SSH port pair of EACH legacy TCP router. Record
successful HTTP and SSH banners, check established TCP/WSS inbound sessions,
compare the Xray MainPID with its value before the upgrade, and check the
HUB heartbeat/UI. Do not confuse service "active" or Xray "-test" with
end-to-end connectivity.

Only after real traffic acceptance, remove the hold manually:

    test -f /var/lib/owrt-remote/.xray-maintenance-hold
    rm -- /var/lib/owrt-remote/.xray-maintenance-hold

Removing the hold allows normal Hub Xray management again. Do not remove it
blindly while diagnosis or rollback is pending.

## Explicit rollback

If the upgraded Hub fails, the script attempts immediate Python+SQLite
rollback. If you discover a problem later, use the EXACT path printed during
the original apply step:

    python3 "$REPO/tools/deploy_belt_hub_v119.py" rollback \
      --backup "/root/owrt-hub-rollback-XXXXXX/files"

Rollback stops only the Hub, restores the original Python and SQLite backup,
starts the Hub, checks its health and Xray hashes, and removes its own
maintenance hold. It does NOT restart or overwrite the Xray binary or config.

A rollback snapshot is sensitive: it contains real UUIDs, tokens in the DB,
and a copy of the running Xray binary. Keep it mode 0700 and never upload
it to a public GitHub repository.

## Explicit out-of-scope actions

- Never run vps/install-vps.sh as a live upgrade over Belt.
- Do not run Hub "render-xray" with output /etc/xray/owrt-remote.json before
  the database state is aligned and the candidate has been validated.
- Do not update or replace /usr/local/bin/xray here.
- Do not migrate legacy routers to WSS during this Hub software upgrade.
- Do not merge experimental branches or bulk-update routers during rollout.

The checked-in script is constrained to the verified Belt baseline.
Any unexpected router count, port, UUID, DB status, or process change must
trigger diagnosis instead of forceful deployment.
