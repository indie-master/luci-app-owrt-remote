# Router Orchestrator: unified Docker and transport manager

Status: **implemented release candidate; not deployed, not production-accepted**.

## Goals and scope

Router Orchestrator is a control-plane for OpenWrt fleet management, not a
general VPN subscription panel. Keep existing LuCI/SSH reverse access, WoL,
router heartbeat, audit events, and RBAC. Do not overwrite legacy TCP or WSS
profiles during migration. Persist identities and route mappings independently
from transports.

## Runtime architecture

- One Compose project: `router-orchestrator`.
- Three project containers: `hub`, `xray`, `nginx`.
- Dedicated root-owned narrow host adapter with Unix SO_PEERCRED validation
  and strictly allowlisted Docker lifecycle operations. Never mount the Docker
  socket in the public-facing HUB, and never grant `docker` group to HUB.
- `nginx` serves project web assets/API/terminal WebSocket. In embedded mode,
  host nginx TCP/SNI routing is preserved without duplicating :443 ownership.
  In standalone mode, project nginx owns 80/443 and certificates.
- `xray` provides reverse tunnels. The management listener and the router
  entry transports need distinct exposure and carefully mapped healthchecks.
- State, TLS materials and router identities are external durable volumes.
- `docker compose` should work from a single stable directory without
  COMPOSE_FILE overlays; migration must preserve all currently active mounts.

## Transport profiles

The owner-only `/transport-configurator/` mini-panel and a strict pure renderer
in `docker/transport_profiles.py` generate candidate server stream settings and
OpenWrt client outbound for legacy TCP, WSS/TLS, REALITY/TCP, XHTTP/TLS,
and gRPC/TLS. A protected Host Adapter validates a candidate listener with
the deployed Docker Xray binary. **Applying profiles to real OpenWrt routers
remains intentionally disabled** until the OpenWrt agent supports the extra
transports and live canary/fallback migration is implemented. Additional
modes (e.g. REALITY with XHTTP or gRPC) require separately reviewed schemas.

The interactive page must implement:

1. List enabled/disabled connection profiles, distinguish server entrance,
   reverse tunnel, SSH tunnel and web endpoint.
2. Create/edit/clone one profile via strict field schemas; reject unknown
   values and conflicting bind ports/SNI/path, never expose private keys.
3. Generate both server Xray configuration and router outbound in memory.
4. Preview JSON diff, mask credentials, run the *same target Xray binary*
   with `run -test`, then verify equivalent supported client core schema.
5. Stage new parallel listener first, distribute candidate settings only to
   a selected pilot router, require positive live reverse-tunnel feedback.
6. Promote with explicit admin approval and rollback handle; retain the old
   transport until the new one is proven and the router confirms persistence.
7. Never apply a transport to an offline router without a recoverable path.
8. Preserve the baseline legacy and WSS configurations on updates.

TLS materials should be uploaded to a dedicated protected directory; REALITY
keys generated on host, private keys never returned via web logs or alerts.

## Xray core upgrade manager

Do not modify a running container's executable. The manager should fetch
official releases, exclude prereleases/drafts, verify signed checksums where
available, pin tag AND image digest, build immutable candidate image, and run
`xray run -test` with exact production mounts. New core versions can pass
syntax tests but break router compatibility, therefore use a canary router
and heartbeat/LuCI/SSH functional probes. Preserve current and two known-good
images. Never trust a mutable `latest` tag as the rollback reference.

Upgrade workflow: download -> verify -> prepare -> canary -> swap -> health ->
observe reverse connections -> commit. On failure, return to known-good image
and its known-good config, then verify old listeners and functional access.
A hard restart through the HUB must use the same fenced host-adapter control.

## Transaction and security requirements

- Exclusive lifecycle lock and immutable transaction journal stored outside
  containers, with pre-operation file hashes and crash recovery.
- Ensure systemd host Xray stays disabled during Docker management; a recovery
  fallback may re-enable it only after Docker has been stopped and ports freed.
- Deny control mutations while an upgrade is running and throttle restarts.
- Preserve authorization for owner-only Xray/transport actions and CSRF.
- Host adapter must never accept arbitrary container name, image, shell, URL,
  filesystem path, or Docker flags from browser-origin data.
- Healthcheck must check process ownership AND network listener AND one
  router's actual reverse access; a listening port is not proof of availability.
- Rollback MUST work when the new Xray is unhealthy (do not gate recovery on
  currently healthy state).
- Do not destroy staging, old images, or systemd rescue until after reboot
  recovery, pilot testing and verified restore.

## Existing Belt migration notes

The current Belt uses 14 Xray TCP ports, a legacy host systemd rollback unit,
an owner-only host adapter, HUB terminal mounts, nginx stream/TLS routing and
Remnawave on the same host. Consolidation MUST keep all those bindings.
The temporary `next-release/docker-compose.yml` was created from the live
Compose configuration as a baseline; it has not yet been activated.

## Acceptance gates

- Web UI and owner VPS terminal (password-based sudo) work.
- WoL with SSH password and with key, on a real selected router.
- All online routers reconnect and LuCI/SSH reverse forwarding works.
- Legacy TCP and WSS survive no-op reload and explicit Xray restart.
- Pilot REALITY/TCP, XHTTP and gRPC profiles each pass both Xray tests and
  real OpenWrt agent compatibility checks before advertised as available.
- Reboot with controlled recovery and external watchdog rollback succeeds.
- Rollback a failed core and a failed transport without altering old identity.
- Host nginx and Remnawave are untouched.
- Only afterwards: remove staging containers and unused project images,
  retaining the last known-good releases and backups.


## Core Manager recovery and image retention

For an approved unified host configuration, keep the Stack inputs and private
state under `/opt/router-orchestrator` and the reviewed source under
`/opt/router-orchestrator/src`. Validate the Stack, then prepare its units
with `sudo python3 docker/manager.py stack --env-file /absolute/path/to/stack.env install-host-services`.
This writes the adapter, watchdog and timer unit files and their shared,
root-owned `/opt/router-orchestrator/managed-stack.conf`. Running the same
command again validates the existing files. A conflicting selector, unit or
drop-in requires operator review. Service activation is a separate approved step.

The watchdog and adapter startup retry interrupted Core Manager transactions.
If a switch left Xray absent, recovery creates only Xray using the previous
verified image. A restart job is settled after the original core and recorded
router channels are verified, without issuing another restart.

For cleanup, supply the same `RO_MANAGED_STACK_ENV` from the trusted operator
environment. The default is dry-run. The plan is computed under the deployment
and lifecycle locks and keeps immutable IDs as well as references. Apply
requires the existing release-acceptance marker and clear transaction state.

The Docker integration workflow pins two distinct official Linux amd64 Xray
images by digest and checks their versions.
The fixture exercises restart, upgrade, rollback and automatic recovery from
a stopped or removed candidate across the matrix. It exercises real reverse LuCI and SSH connections and
uses synthetic timestamps in the HUB database as heartbeat evidence. Physical
OpenWrt acceptance remains a separate release gate.
