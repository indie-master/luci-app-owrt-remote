# Docker WSS enrollment gate — six-router Belt fleet

Status: **draft / no live Xray apply authorized**.

This update addresses a production-blocking failure: the Docker HUB must never
report successful addition of a new router when the host Xray configuration
was not updated. It must protect legacy TCP and all existing WSS identities.

## API contract

- `POST /api/router`: Docker accepts *new WSS/443* routers only while the
  explicitly configured host adapter is available. The request is authenticated
  via existing add-router permission; SQLite insertion and Xray synchronization
  are serialized within the HUB process.
- Successful host registration returns HTTP 200 with `ok=true`.
- Failed host registration returns **HTTP 503**, `ok=false`,
  `pending_registration=true`, and the already-saved router record. This is
  *not* a completed installation, and should not be presented as one.
- `POST /api/router/{id}/xray/sync`: owner-only retry for an existing WSS
  record. No second router row or UUID is created. A failed retry returns
  HTTP 503. This API must not be exposed to non-owners.
- Old TCP router AX3000T-Shteinberg is not migrated or repaired here.
- The host adapter currently allows only **one** WSS router (2 clients,
  2 reverse portals, 2 routes) in each applied Xray change. Unrelated changes
  and identity mutation are blocked, and maintenance hold is respected.

## Run a safe preview on Belt before enabling writes

From a pinned, clean checkout in `/opt/router-orchestrator/release`:

```bash
sudo python3 tools/orchestrator_docker_wss_enrollment_preview.py \
  --snapshot /opt/router-orchestrator/backups/orchestrator-docker-rehearsal-xn5xakyp \
  --expect-commit "$(git rev-parse HEAD)"
```

The preview compares the archive to *current* live router identities and host
Xray SHA256 to reject stale snapshots. It then restores the snapshot to a new
private temporary directory, imports the **candidate** HUB under its private
state path, checks existing Xray-to-database identity and port parity, preserves
all non-generated host configuration, adds a synthetic 7th WSS record to the **temporary**
SQLite copy. The preview verifies existing DB/Xray identity/port parity,
then constructs a **strictly additive** candidate: exactly two WSS clients,
two reverse listeners and two rules for the synthetic new router. Historical
changes in the existing host Xray JSON remain untouched, even where the
new HUB's whole-config generator differs. The preview invokes the same
`validate_transition` used by the host adapter and removes the restored
copy afterwards.

A PASS establishes only code/config compatibility, **not a real Xray restart,
WSS router connection or permission to enable mutations**.

If BLOCKED, do not run `xray.apply` or remove
`/var/lib/owrt-remote/.xray-maintenance-hold` to force success. Investigate
the mismatch; take a new private snapshot if the fleet changed.

## Still required before production

- Supervised host adapter deployment (Unix-socket peer UID 23088), strictly
  bounded write capability, Xray candidate validation and rollback check.
- Verify real WSS LuCI/SSH and heartbeat for one new router with independent
  network access for emergency rollback.
- Add a visible `Retry registration` action for pending rows in the UI.
- Confirm concurrent updates from other process instances cannot race with
  this process-local lock (the supported production deployment is one HUB).
- Demonstrate uninterrupted usability of the existing five WSS routers after
  a controlled Xray restart. Avoid touching AX3000T.
