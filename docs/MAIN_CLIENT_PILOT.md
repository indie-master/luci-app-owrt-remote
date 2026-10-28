# Client v119 — first pilot on router `main` (NOT PaxoBs)

**Status: plan / read-only preflight. DO NOT run the installer yet.**
The operator selected the router labelled `main`, because local recovery is
possible in the evening. The repo branch name `main` is unrelated to that
router name. Keep `PaxoBs` untouched.

## Why the existing installer is not yet a safe remote one-click update

`install.sh` preserves the existing UCI file and the pilot environment
switches skip Xray preparation and optional WOL package installation, but
currently:

- It replaces agent, init, hotplug, LuCI and CGI files **one at a time**;
  a later failure leaves a mixed-version installation.
- It stops/restarts its logging service and restarts `rpcd`, then reloads
  or restarts `uhttpd` if needed. This is not a no-op for remote management.
- It does not implement automatic recovery of the previous entire bundle
  after loss of end-to-end LuCI/SSH over the reverse tunnel.
- It has no built-in local watchdog verifying an out-of-band acceptance
  acknowledgement from Belt. Xray is not intentionally restarted by this
  pilot, but that alone does not prove that the reverse connection survives.

Therefore **do not click "Update all routers", change UCI transport, run the
bare installer, or remove Belt's HOLD** until the guarded pilot tooling and
rollback are implemented and tested.

## Step 1: safe read-only inventory

Run on the *known router `main`* only, in a terminal with alternate
local recovery access. Nothing below prints UUIDs or auth tokens.

The versioned **read-only** guard is `tools/openwrt_pilot_preflight.sh`.
Review it before executing. It deliberately fails if live UCI is not TCP/18443,
if either reverse outbound is not TCP none, or if required utilities are absent.
The script is a diagnostic ONLY: it does **not** install or create a rollback
watchdog. CI covers shell syntax and rejection of WSS/unexpected ports.


```sh
printf '%s\n' '===== ID ====='
hostname
ubus call system board 2>/dev/null | jsonfilter -e '@.model' -e '@.release.description' || true

printf '%s\n' '===== TRANSPORT ====='
uci -q get owrtremote.main.vps_transport || echo tcp
uci -q get owrtremote.main.vps_port || true
uci -q get owrtremote.main.xray_config || true
uci -q get owrtremote.main.xray_bin || true

printf '%s\n' '===== SERVICE STATUS ====='
for s in owrt-remote rpcd uhttpd; do
  /etc/init.d/"$s" status 2>/dev/null || true
done
printf '%s\n' '===== VERSION ====='
grep -m1 '^OWRT_REMOTE_UI_VERSION=' /www/cgi-bin/owrt-remote 2>/dev/null || true
printf '%s\n' '===== STORAGE ====='
df -h /overlay /tmp 2>/dev/null || true
printf '%s\n' '===== Xray PROCESSES ====='
pidof xray 2>/dev/null || true
printf '%s\n' '===== CONFIG HASH ====='
sha256sum /etc/config/owrtremote 2>/dev/null || true
```

Check in the Hub that the selected `main` row really maps to the router
being queried. Verify the old transport remains `tcp`, port 18443 and the
running Xray version/path/JSON match expectations. Never paste full UCI,
Xray JSON or `web.key` to public logs.

## Step 1 result: operator-tested baseline (2026-10-28)

Read-only preflight on `main` / Cudy WBR3000UAX v1 (OpenWrt 24.10.5): **PASS**.
`router_id=main`, two `TCP none` reverse outbounds, port `18443`, Xray
26.3.27/arm64, `owrt-remote`, `rpcd` and `uhttpd` running.
Overlay free 57.2 MiB; /tmp free 181.0 MiB (tmpfs, not persistent storage).
Installed router UI: `2026-07-02-mobile-terminal-batch-v25`.

Live memory `/proc/meminfo`:
- Total: 497416 KiB (485.8 MiB);
- Free: 241848 KiB (236.2 MiB);
- Available: 272600 KiB (266.2 MiB, **use this for memory checks**);
- Page cache: 133888 KiB (130.8 MiB);
- Shared: 63348 KiB (61.9 MiB); swap 0.
- Existing Xray 26.3.27 resident `VmRSS`: 30976 KiB (30.3 MiB), `VmHWM` 31844 KiB (31.1 MiB), 9 threads. Its virtual `VmSize` is **not** RSS.
- No OOM events were found in visible `dmesg`. `/proc/pressure/memory`
  and requested `/proc/vmstat` OOM counters returned no text; this does
  **not** prove zero historic OOMs or memory pressure.

The older BusyBox `free -m` display printed values corresponding to KiB;
`/proc/meminfo` above is the authoritative source for units.

**Gate:** memory and staging storage look sufficient for a **single** low-impact
client pilot on this ~512 MB device. This does not authorize installing until
the bundle-level rollback + local watchdog is implemented/tested. Separate
real-hardware tests are required for 128/256 MB routers. Do not touch Belt's
maintenance HOLD or the selected Xray executable.

## Step 2: guarded upgrade tooling, to be implemented and tested first

The dedicated pilot entry point must:

1. Verify the router identity with a pre-shared expected agent/router ID,
   accepted `tcp` transport and port, writable overlay/free space,
   no concurrent install, and the presence of local recovery access.
2. Download **all** version-pinned files and manifest, verify SHA-256 and
   syntax, and check storage limits before changing any live file.
3. Create a private, persistent, complete snapshot of all files the installer
   may replace, their modes, `/etc/config/owrtremote`, the selected Xray
   binary/config identity, and the running service state. Preserve unknown
   user customizations.
4. Arm a router-local watchdog with a deadline BEFORE any installation.
   The watchdog must survive a dropped management tunnel and roll back
   affected files/services on missing explicit, authenticated acceptance.
   Never use "the router can ping the VPS" as the only acceptance condition.
5. Apply staged files as a coherent versioned bundle, keep Xray-core and its
   boot cache unchanged, preserve `TCP none` and all UUID/route/port fields,
   and avoid gratuitous restart of the reverse tunnel.
6. From Belt, test HTTP 200 for LuCI **and** a valid SSH banner through the
   existing reverse port pair. Verify stable Xray config/process identity.
   Only then send explicit acceptance to cancel the watchdog.
7. On any failure or lost confirmation, automatically restore the previous
   bundle locally and reestablish old services. Log a redacted failure
   report. Keep an independently available local emergency access route.

The watchdog timeout needs real-world measurements and explicit approval
before implementation; it must not be chosen blindly.

## Guarded bundle milestone (development branch only)

Two **new, separate** tools now exist. This does NOT mean the live upgrade is
ready or that the original installer has been replaced:

- `tools/build_router_bundle.py`: creates a deterministic, UNCOMPRESSED tar
  containing exactly the ten files the original `install.sh` updates (excluding
  the UCI config, selected Xray binary, optional packages and service actions).
  Outputs SHA-256; no router commands.
- `tools/router_bundle_guard.sh`: accepts a **pre-downloaded local bundle and
  a separately reviewed exact SHA-256**. The `stage` command rejects mismatched
  hashes, unexpected archive members, links and invalid per-file checksums.
  It limits archives to 6 MiB and requires 24 MiB free on persistent overlay.
  The `snapshot` command prepares a private persistent copy of the old ten
  files plus UCI and default client JSON. Existing backups are never
  overwritten. Only the `main` router on TCP none/18443 is supported.
- Importantly, this version **does not implement `apply`, `confirm`,
  `watchdog` or `rollback` yet**. Those commands are intentionally rejected.
  It neither updates nor restarts any service, nor installs a boot recovery
  mechanism. Do not interpret `STAGED` or `SNAPSHOT READY` as safe to run
  the original `install.sh`.

### Developer-only offline checks

On a disposable source checkout (not Belt or the router):

```sh
python3 tools/build_router_bundle.py --out /tmp/ro-candidate.tar
python3 -m unittest discover -s tests -p 'test_router_bundle_guard.py' -v
```

CI runs the builder and eight isolated synthetic-root tests, checking digest
mismatches, archive path/link injection, wrong router/transport/port, repeat
snapshot protection and absence of any apply action.

### Outstanding safeguards before pilot

- Complete the router-local automatic rollback/watchdog with a persistent
  boot-safe recovery mechanism and authenticated outside-in LuCI + SSH
  acknowledgment from Belt; test power loss and remote connection drop.
- Make the 10-file installation transaction recoverable on interrupted writes;
  preserve exact UCI/client JSON, Xray binary, reverse tags and auth state.
- Add explicit rollback validation, concurrency locks, space and RAM budgets,
  release manifest provenance, per-platform BusyBox `tar -xOf` validation
  and actual hardware exercise before permitting `apply`.
- All previous original service behavior (including WOL, updates, WAN,
  logging, user auth) must still work. Never install this experimental bundle
  with an untested manual copy step.

## Step 3: after the guarded pilot

- If `main` passes end-to-end LuCI/SSH tests, keep it on `TCP none`.
- Defer its WSS migration to a separate controlled window using server-side
  temporary dual authorization and a router-local fallback.
- Keep other routers unchanged until a stable rollout channel, compatibility
  matrix and rollback tests exist.
- Continue to keep Belt's Xray 26.3.27 and maintenance hold unchanged.

Live updates are NOT performed by this document or any GitHub commit.
