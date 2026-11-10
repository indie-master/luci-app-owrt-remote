# Belt: guarded confirmation of four-WSS HUB SQLite state

Status: operator-gated. Use only after a private multi-WSS stage has
`STAGED MATCH` with `legacy=1` and Xray syntax `PASS`.

## Observed staging, 2026-11-10

- `PaxoBs`, `WR3000S_2`, `main` are confirmed WSS/443 on their
  routers but remain TCP/18443 in HUB SQLite.
- `Netis_N6_router5` already has WSS/443 in HUB SQLite.
- `AX3000T-Shteinberg` deliberately remains TCP/18443.
- The staging check from
  `/root/ro-hub-db-parity-462nmsoi` found:
  `LEGACY=0: DIFFERS`, `LEGACY=1: MATCH`,
  `LEGACY=1: Xray syntax PASS`.
- No server-side Xray or HUB daemon change is needed to rectify
  these DB rows. The reconciled DB must keep TCP registrations by
  setting `vps_allow_legacy=1` on the three migrated rows.

## Guarded tool

`tools/belt_wss_db_reconcile_apply.py` reads the stage artefacts and
validates the real DB, installed Python HUB source, Xray JSON and
UUID/route identity parity against the stage, then runs one
SQLite transaction modifying exactly three DB fields
(`vps_transport=ws`, `vless_port=443`,
`vps_allow_legacy=1`) on each of those three named routers.

Default invocation is read-only preflight. The explicit `--apply`
does the following:

1. Checks HUB health and both systemd services, process IDs and
   installed source/config hashes; verifies the stage is root-only.
2. Creates/preserves `/var/lib/owrt-remote/.xray-maintenance-hold`
   before any DB mutation to prevent an automatic server Xray rewrite.
3. Takes a consistent SQLite backup via `sqlite3.Connection.backup`
   as `apply-before.sqlite` inside the private staging directory.
4. Uses `BEGIN IMMEDIATE`, exact baseline checks and conditional row
   updates; no router or server Xray config is changed.
5. Runs installed HUB `render-xray` into a private file with isolated
   staging state. Compares the entire normalized generated Xray
   document against deployed Xray and verifies syntax with the installed
   core. Confirms HUB/Xray processes and source/config hashes unchanged.
6. If post-commit verification fails, restores only the old three
   fields on these routers, leaving concurrent heartbeat metadata
   untouched. Leaves the maintenance hold even on failure.

It never restarts services, changes Xray JSON, reconfigures Nginx or
applies anything to `AX3000T-Shteinberg`. It never removes the
maintenance hold automatically.

## Operator commands

Download a pinned release of both tools from `feature/indie-branding`
to the same protected directory, verify `python3 -m py_compile`,
then run:

```bash
python3 /tmp/belt_wss_db_reconcile_apply.py \
  --stage /root/ro-hub-db-parity-462nmsoi \
  --migrate main WR3000S_2 PaxoBs \
  --existing-wss Netis_N6_router5 \
  --leave-tcp AX3000T-Shteinberg
```

The expected output includes `PREFLIGHT: PASS` and
`READ ONLY`. After reviewing:

```bash
python3 /tmp/belt_wss_db_reconcile_apply.py \
  --stage /root/ro-hub-db-parity-462nmsoi \
  --migrate main WR3000S_2 PaxoBs \
  --existing-wss Netis_N6_router5 \
  --leave-tcp AX3000T-Shteinberg --apply
```

After `APPLIED`, rerun the read-only
`tools/belt_fleet_config_audit.py` with all four WSS router IDs,
and probe live reverse LuCI/SSH on all four. Verify Hub UI, heartbeat,
and `owrt-remote-xray.service` uptime/PID are unchanged.

Only after acceptance and ensuring no other owner/operation needs
the maintenance hold may the operator explicitly remove
`/var/lib/owrt-remote/.xray-maintenance-hold`. Don't delete a
pre-existing hold from another operation. No automatic removal here.

## Manual reversal if required

If a later Hub issue is directly attributed to the DB transport values,
while Xray config remains unchanged and the backup/stage exists,
invoke the same tool with `--rollback` (instead of `--apply`).
It restores exactly the original three values per router and keeps the
maintenance hold. Reverting HUB DB will temporarily reintroduce
transport drift; do not re-render server Xray from the reverted DB.
Any emergency deployment should rely on the full private SQLite backup,
not a raw file copy into a live WAL database.

All staging artifacts contain live credentials. Never publish them,
their raw JSON or SQLite backups. The tool is not a general-purpose
fleet updater and does not replace the guarded OpenWrt client rollout.
