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

## Step 3: after the guarded pilot

- If `main` passes end-to-end LuCI/SSH tests, keep it on `TCP none`.
- Defer its WSS migration to a separate controlled window using server-side
  temporary dual authorization and a router-local fallback.
- Keep other routers unchanged until a stable rollout channel, compatibility
  matrix and rollback tests exist.
- Continue to keep Belt's Xray 26.3.27 and maintenance hold unchanged.

Live updates are NOT performed by this document or any GitHub commit.
