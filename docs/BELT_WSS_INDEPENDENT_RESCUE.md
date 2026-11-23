# Belt: independent Xray emergency rescue (before first live WSS pilot)

**Stage:** draft implementation and safety gates. Not authorization to apply
or restart Xray, not a Docker HUB cutover.

## Why an independent rescue is required

The six-router Belt inventory and Xray parity passed: 5 WSS and one TCP,
transaction NONE, host adapter read-only, production HUB and Xray active.
The candidate seventh WSS router passed the real installed Xray core syntax
check in an isolated preview. A future live Xray restart might still fail:
syntax acceptance cannot prove reverse LuCI, SSH, or heartbeat.

The original operator pilot rollback is located in a mutable Git checkout,
and it imports reviewed project sources. An independent root-owned recovery
utility must be present *before* any live Xray changes so recovery is possible
if Git, Docker, or the HUB is broken or unavailable.

## New rescue program

Source: `tools/orchestrator_wss_emergency_rescue.py`.

Fixed install path: `/usr/local/sbin/router-orchestrator-wss-rescue`,
owned by root, mode 0700. Its normal operation has **no** project imports,
network API dependencies, Git checkout, Docker, database or HUB dependency.

The operator pilot `apply` now fails closed unless the independent pinned
file exists with byte-identical SHA256 to the reviewed source and correct
root-only ownership/permissions.

The rescue supports exactly:

- `status`: **read-only**; show transaction phase and host Xray SHA256.
  The installed utility must not run maintenance or create files here.
- `restore --confirm --expect-candidate-sha256 <reviewed 64-hex SHA>`:
  **emergency only**; it obtains the same exclusive transaction lock as
  the operator pilot, verifies a root-private journal and backup, demands
  the expected candidate SHA, and rejects any host Xray config SHA that is
  neither the original nor this candidate. It then restores the exact
  original JSON with an fsynced atomic replace, restarts **only**
  `owrt-remote-xray.service`, checks local Xray TCP and WSS listeners and
  persists `ROLLED_BACK`. It keeps original backup and evidence. A crash
  between restoring the file and recording success is recoverable by
  repeating the exact command.
- The maintenance-hold file is required to be present and remains untouched.

**This does not demonstrate end-to-end health**: local listener readiness
does not prove the six router tunnels have recovered. Full remote LuCI/SSH,
heartbeat and old-router regression are separate live acceptance gates.

## Current read-only operator command

Once the approved commit has been checked out on Belt, **only**:

```bash
sudo python3 tools/orchestrator_wss_emergency_rescue.py status
sudo python3 tools/orchestrator_wss_commit_pilot.py status \
  --expect-commit "$(git rev-parse HEAD)"
```

Both commands use existing state and do not register a router, restart a
service, or modify live configuration. Expected before first application:
`WSS RESCUE STATUS: PASS` with `TRANSACTION: NONE`, and
`WSS PILOT STATUS: PASS` with six active routers. The pilot status
explicitly prints `INDEPENDENT RESCUE: NOT_INSTALLED_OR_UNVERIFIED` until
a byte-identical root-owned pinned copy is installed, then `PINNED_OK`.
Fleet health alone never authorizes an Xray apply.

## Separate opt-in pinned installation, not yet required for the status test

```bash
sudo bash tools/orchestrator_wss_rescue_install.sh \
  --install-rescue "$(git rev-parse HEAD)"
```

This operation writes only the root-owned standalone utility under
`/usr/local/sbin`; it refuses an existing executable, verifies the exact
reviewed Git SHA and clean checkout, and does not touch systemd or Xray.
Installing rescue alone is not permission to call `apply`.

Do not run `restore` without a real emergency and verified candidate SHA;
never delete the private journal or backup to bypass a safety gate.

## Before authorizing the first Xray commit

1. Confirm the actual new OpenWrt device and a dedicated owner-approved
   router ID (not an existing fleet ID). Do not create a live DB record yet.
2. Finish self-contained crash recovery tests, root-only installation
   acceptance, and rescue console access independent of HUB/Xray tunnels.
3. Record baseline real LuCI HTTP, SSH banner and heartbeat for the five
   reachable WSS devices (do not count the offline legacy TCP router as
   a fresh regression), with explicit post-restart checks.
4. Take a separate private tested backup of host Xray and hub.db and verify
   that the existing production state is still six active routers.
5. Only then plan a maintenance window, initial router provisioning,
   candidate preview / SHA approval, and a controlled Xray restart;
   a strict failure path restores the previous Xray. No Docker cutover
   occurs in this WSS pilot.
