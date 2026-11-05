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

## Prototype transaction milestone (NOT YET cleared for the real router)

New opt-in `tools/router_pilot_transaction.sh` adds the separately gated
`apply`, `confirm`, `rollback`, a short-lived autonomous watchdog and
an OpenWrt `S08` early-boot recovery hook. **The original installer remains
unchanged.** `router_bundle_guard.sh` continues to reject `apply`.

This new script:

- Refuses all targets except the explicitly selected `main`, TCP/18443;
  preserves the selected Xray binary and current TCP-none JSON and never
  invokes service, network, firewall or Xray restarts.
- Requires pre-downloaded/pinned tar and per-file manifest digests plus a
  verified persistent snapshot of all ten original files and critical UCI/JSON
  digests. Aborts when any original changed after its backup, or when a backup
  was corrupted.
- Checks available RAM and persistent overlay reserve, writes a persistent
  `pending` marker *before* any live code-file change, and uses per-file
  same-directory atomic replacement.
- Starts the short-lived watchdog before mutation; if an operator does not
  explicitly confirm via a unique nonce before the deadline, it restores
  previous code files from the private snapshot. A router reboot while pending
  invokes early-boot rollback before the usual reverse tunnel service starts.
- Requires external operator checks from Belt (LuCI HTTP/SSH banner, Xray
  fingerprint and actual reverse link) before nonce confirmation. The
  tool does **not** independently assert that those external checks passed.
  Never issue `confirm` until outside-in verification is recorded.
- Keeps the recovery copy after confirmation for review. A confirmed update
  still needs a separately protected normal reboot/service activation test;
  the early-boot hook intentionally rolls back rather than testing new code
  if a reboot occurs **before** confirmation.

### Validation status and remaining safety gates

CI tests synthetic filesystems under Ubuntu `sh` AND BusyBox `ash` with
BusyBox applets, including success, timeout, unexpected reboot, incorrect
nonce, duplicate apply, mismatched identity/port/transport, private config
changes, damaged candidate and damaged recovery snapshots.

**Do NOT execute `apply` on Cudy yet.** The prototype now **hard-rejects all live-mode calls** to `apply`, `confirm`, `rollback`, `watchdog` or `boot-recover` (only an explicitly isolated synthetic root runs). Live enablement requires a separately reviewed release. The prototype has not been tested
on the actual Cudy/OpenWrt firmware with its built-in `tar`, rc.common,
procd, overlay filesystem, reboot lifecycle or live reverse tunnel. It
currently installs files without service activation; therefore a simple
HTTP/SSH check only proves that the *old running connection* survived.
A separate reversible functional activation/reboot acceptance gate is needed
before calling the v119 client fully upgraded. Test an interrupted write,
unavailable VPS, watchdog process termination and power loss in a disposable
OpenWrt environment before real execution.

No `install.sh` commands or router-side deployment commands are published
here intentionally; only the developer canary fixture is covered by CI. Do
not remove Belt's maintenance HOLD or update Xray-core.

## Next hardware gate: isolated OpenWrt applet probe (safe, no update)

First real Cudy WBR3000UAX / OpenWrt 24.10.5 run (operator):
`PROBE: FAIL - missing applet: od`. The probe stopped before creating any
temporary fixture or touching live paths. **No package installation is
required.** `od` was an unnecessary hard dependency. The probe and
experimental transaction now use a secure 16-byte nonce from
`/dev/urandom` encoded by BusyBox `hexdump`, or `od` if present, or
`openssl rand -hex 16` if available. They fail closed if no secure encoding
method works (never use timestamp/PID fallback). The transaction attempts
random generation **before** adding its boot recovery hook.

Second Cudy hardware probe (pinned `aa6803fbb0206493b46a0f4d9f288995a210e517`)
stopped at `PROBE: FAIL - missing applet: nohup`, again **before any
synthetic fixture was created**. This is a material runtime blocker for the
transaction prototype, whose watchdog used `nohup`. The isolated probe now
treats `nohup` as an **optional diagnostic**, reports availability of
`nohup`, `start-stop-daemon`, `setsid`, `procd`, and `ubus` in one
pass, and prints `WATCHDOG BLOCKED` if `nohup` is absent. It also
collects the complete required-applet inventory before stopping, avoiding
serial discovery of one missing utility at a time. The transaction separately
rejects missing `nohup` before placing a recovery init script; all live
transactions remain hard-blocked.

Important: **do not silently replace `nohup` with a plain background
`&`**. The local rollback watchdog must survive shell/session disconnection
and be independently supervised; prefer evaluating OpenWrt `procd` with a
separate recovery service under controlled test conditions. A present
alternative applet does not establish durable supervision.

The initial `od` failure does not establish that tar, BusyBox extraction,
overlay capacity or the rest of the pilot passed; re-run the immutable
CI-verified corrected probe on the router to check those separately.



`tools/openwrt_hardware_compat_probe.sh` provides a **separate,
non-mutating-to-production** check of the exact router's BusyBox tools,
tar streaming (`tar -xOf`), checksum, copy/rename, random nonce,
overlay `df -Pk`, and `MemAvailable` parsing.

The script creates one private `mktemp -d /tmp/ro-hardware-probe.XXXXXX`
directory, uses only synthetic text fixtures inside it, and deletes only
that temporary directory. It does **not** run the transaction updater,
read credentials, edit /etc/config, restart Xray, stop procd, change
routing/DNS or invoke an init script.

After the CI check has passed, obtain the script at a **pinned Git commit
SHA**. Review before use. On the chosen `main` OpenWrt shell:

```sh
wget -O /tmp/ro-hardware-probe.sh 'https://raw.githubusercontent.com/indie-master/luci-app-owrt-remote/<VERIFIED_COMMIT_SHA>/tools/openwrt_hardware_compat_probe.sh'
sh -n /tmp/ro-hardware-probe.sh && sh /tmp/ro-hardware-probe.sh
```

The placeholder SHA must be replaced with the latest known CI-passed
commit: do NOT run the literal placeholder. This gate is *not* a full
client update test and **cannot** establish that reboot, persistent
rollback or reverse access will work after installing the new agent.
The original on-router client must stay running and unchanged.

## Hardware compatibility result: PASS (2026-11-05)

The operator ran the exact pinned isolated probe
`0f7d75df597871bf7f8a6e46ebc5a99c9a5ff795` on the chosen
Cudy WBR3000UAX v1 / OpenWrt 24.10.5:

- All required filesystem applets found; `tar -xOf`, `cmp -s`,
  `cp -p`, same-directory rename and SHA-256: **PASS**.
- `dd` and `hexdump` present, `od` and `openssl` absent;
  random-nonce generation using the secure fallback: **PASS**.
- `uci`, `jq`, `sed`, `id`, `kill`, `ln`, `rmdir`: present.
- `nohup`: absent. `start-stop-daemon`, `setsid`, `procd`
  and `ubus`: present. **The legacy prototype watchdog remains blocked**.
- `/overlay` free **67512 KiB** (~65.9 MiB), `MemAvailable`
  **256808 KiB** (~250.8 MiB).
- Final test output: `PROBE: PASS - isolated temporary filesystem checks
  complete. No client upgrade was performed.`
- The diagnostic `busybox: applet not found` appeared when invoking
  `busybox` with no applet to show its version. It did not invalidate the
  successful tested filesystem applets.

### Independent procd watchdog design (development only)

`tools/router_pilot_procd_recovery.init` is a **NON-DEPLOYABLE,
test-only blueprint** using the official OpenWrt
`USE_PROCD=1` / `start_service` contract. It is NOT wired into the
legacy installer, the live client or `router_pilot_transaction.sh`, and
refuses live registration:

- `pending` at boot => invoke local `boot-recover` *before* allowing
  other pilot actions;
- `armed` with no pending update => register a foreground
  `rollback-watchdog` instance under `procd`;
- no markers => no process starts; no changes to the existing
  `owrt-remote` procd service.

The new synthetic-root tests mock the procd interface, check registration,
error propagation, fail-closed production refusal, and boot recovery
ordering. Passing them proves **script structure only**, not procd's real
process lifetime, SSH-session isolation, or reboot recovery.

Important open items before any live client upgrade:

1. Integrate actual procd supervision (without `nohup`) with the updater
   while preserving `USE_PROCD=1` and existing v119 services.
2. Ensure unexpected watchdog termination triggers prompt local rollback,
   without merely restarting and resetting the deadline. A bounded
   monotonic deadline and fail-safe recovery across power loss are mandatory.
3. Demonstrate `procd` instance registration and status, tunnel interruption,
   manual rollback, reboot recovery and persistent snapshot integrity in
   disposable OpenWrt or a carefully authorized local test.
4. Confirm newly installed agent functionality after activation; live
   LuCI/SSH reachability can be supplied by old running processes and is not
   proof of the updated agent's behavior.
5. Keep production `apply` blocked until separately reviewed and approved.

## Fixed-deadline procd integration tests (development only)

The experimental `router_pilot_transaction.sh` now computes an immutable
deadline from Linux `/proc/uptime` and saves `deadline.uptime` before
arming the local watchdog. Every new watchdog invocation reads that **same
absolute boot-relative deadline**. It does not reset the countdown on
process restart, and it fails closed by attempting a snapshot rollback
when deadline metadata is unreadable or corrupt during a pending update.
No wall-clock/NTP changes can extend the deadline. On a router reboot,
the separate early boot recovery path immediately restores the old files
when a `pending` marker exists; it does not reuse the old boot's uptime.

The non-deployable `router_pilot_procd_recovery.init` has separate
same-boot versus real-boot behavior:

- Normal `start_service` with `armed`, including `pending`, registers
  a supervised watchdog without prematurely rolling back.
- `boot` with `pending` runs `boot-recover` before normal v119
  `owrt-remote` startup.
- Both service registration and the watchdog process are still **test-only**;
  the `RO_TEST_MODE=1`/synthetic-root restrictions and live-mode hard
  rejection remain in force.
- CI includes a synthetic integration test that captures the procd command
  and environment, launches the **real transaction watchdog** with a
  simulated uptime beyond the original deadline, and checks full snapshot
  restoration. A second test checks boot-time rollback using the real
  transaction script. These tests do not contact live procd.

**Release blockers remain:** real-device supervision, safe service shutdown
after confirmation, repeated watchdog crashes exhausting procd retry limits,
signal/session behavior, critical-file integrity under power loss, actual
boot testing, out-of-band Belt confirmation, and proof that the *new*
client agent works after controlled activation. We must not infer live
readiness from a successful simulation. Original installer and live Xray
remain untouched.

## Confirmation / procd supervisor lifecycle safety (development only)

The synthetic transaction and procd blueprint now cover several
**previously unsafe states**, with dedicated CI tests:

- `confirm` rechecks `deadline.uptime` **under the transaction lock**
  before recording success. Even when the watchdog has died or not yet
  woken up, an expired deadline rejects confirmation and restores the
  original client code.
- A successful commit writes the `confirmed` marker, then removes
  `armed`, and **only then** removes `pending`. If a power failure
  occurs after either of the first two steps, boot recovery sees pending,
  restores the original files, and removes the stale success marker.
  Fault-injection tests simulate both interruption points.
- Backup integrity is checked separately from the live UCI/Xray JSON
  contents. `apply` and `confirm` still require unchanged private
  configurations, but **rollback must not refuse to restore the agent**
  just because an unrelated UCI change happened during the pilot.
  The rollback never overwrites UCI or Xray JSON.
- The test-only procd blueprint now exposes `cleanup_confirmed` as
  an **explicit coordinator step**. It refuses a pending or armed
  transaction and, after confirmation, invokes a mocked
  `procd_kill ro-pilot-recovery rollback-watchdog`. The synthetic
  integration test exercises this gate with the real test transaction.
  It is **not wired into the live confirmation path**.

### Still unresolved before approving production

OpenWrt `respawn` with a finite retry limit can stop restarting a
frequently crashing watchdog. The blueprint currently uses `respawn
3600 2 3`, which **is not adequate as a sole fail-safe**; retries can
be exhausted before the original deadline. An indefinitely restarting
watchdog (`retry=0`) alone would not fix a persistent failing program
and would also need explicit service cleanup after confirmation.
Neither behavior has been validated on real Cudy `procd`.

The eventual production flow must supervise the worker independently
of the SSH session, preserve the fixed deadline, provide an independent
local recovery mechanism when supervision fails, unregister the service
after `confirm`, and validate it under abrupt connection loss,
forced worker exits and power-cycle recovery. The existing live-mode
lockout remains enabled, and the original v119 installer is unchanged.

## Independent procd guard + disposable real-Cudy exercise (development only)

The recovery blueprint now launches **two independent foreground instances**:

1. `rollback-watchdog`: the original transaction deadline worker.
2. `independent-deadline-guard`: a separate shell implementation that
   independently reads the same persisted `/proc/uptime` deadline and,
   if the primary watchdog is crashed, invokes the **sealed transaction
   rollback** when `pending` remains.

Both use `procd_set_param respawn 3600 2 0`; the trailing `0` means
**unlimited retries** in OpenWrt procd. These are NOT independent of
procd itself or of the underlying rollback script. Early boot recovery
remains essential. Both instances are stopped by the explicit
test-only, post-commit cleanup gate.

The **full shell-only hardware fixture** is
`tools/openwrt_synthetic_update_cycle.sh`. It:

- Creates dedicated `/tmp/ro-pilot-fixture.XXXXXX/router` directories
  with an explicit `.ro-pilot-synthetic-root` marker and synthetic
  samples of all ten client files, plus FAKE UCI and FAKE Xray JSON.
- Generates an 11-entry tar bundle in shell (no Python/Node required),
  performs the real bundle-guard `stage` and `snapshot`, then runs the
  test-mode transaction code and its background fixture watchdog.
- Demonstrates a failed/no-confirm timeout rollback, an independent
  secondary rollback, and a successful one-time-token confirmation.
- Checks the real agent, UCI and Xray **hashes only** before/after;
  it never copies, prints or edits those live files.
- Removes only its own validated temporary fixture directories under
  `/tmp`. It **does not register procd**, alter init scripts, restart
  clients, signal Xray or trigger any network configuration.
- Uses the plain background watchdog **solely for this synthetic fixture**.
  It does not claim to survive SSH disconnection or make the live update
  safe. No production use of this launcher is permitted.

For a later router-specific exercise, download these four **matching,
commit-pinned** files to a dedicated `/tmp` folder and run only
`openwrt_synthetic_update_cycle.sh`:

```sh
REV='<CI-PASSED-FULL-COMMIT-SHA>'
mkdir -p /tmp/ro-pilot-validation
BASE="https://raw.githubusercontent.com/indie-master/luci-app-owrt-remote/$REV/tools"
for name in router_bundle_guard.sh router_pilot_transaction.sh router_pilot_secondary_guard.sh openwrt_synthetic_update_cycle.sh; do
  wget -O "/tmp/ro-pilot-validation/$name" "$BASE/$name" || exit 1
done
sh -n /tmp/ro-pilot-validation/openwrt_synthetic_update_cycle.sh &&
sh /tmp/ro-pilot-validation/openwrt_synthetic_update_cycle.sh
```

Do not execute the placeholder SHA. Replace it with a **verified,
successfully tested** commit from GitHub Actions, and review the scripts.
The Cudy hardware exercise validates file operations and rollback on a
fake filesystem; it still does **not** validate real procd registration,
power-loss recovery, SSH lifetime, running-agent activation, or any
real update. These remain release blockers. The existing live-mode
`apply` hard-lock remains in place.

## Hardware evidence: real OpenWrt procd respawn PASS (2026-11-05)

The operator ran a **real procd** test on Cudy WBR3000UAX v1,
OpenWrt 24.10.5, using a uniquely named temporary `procd` service.
The output confirmed:

- Initial isolated worker: PID `31736`.
- `ubus call service signal` used to terminate **that test instance only**.
- Replacement isolated worker: PID `31751`.
- `PASS: procd automatically restarted isolated worker`.
- The script printed `Cleaning up isolated service`; this is a request
  for deletion, **not independent proof** that the registration disappeared.

The test did not invoke the transaction installer, operate on the real
`owrt-remote` instances `heartbeat` or `xray`, change the TCP/18443
link, or update actual files.

### Next, separate **dual-instance** procd test (still no real upgrade)

`tools/openwrt_procd_dual_isolated_probe.sh` plus
`tools/openwrt_procd_dual_worker.sh` exercises two actual procd instances
under a randomly named temporary service. The primary can be signalled by
`ubus`, and the separately supervised secondary expires a **FAKE**
deadline and clears a **FAKE** `pending` marker under `/tmp`.
A third cleanup instance removes the service after 75 seconds even if
the initiating SSH session disconnects; the initiating shell additionally
attempts service deletion and fixture cleanup in a trap.

The test never launches the real transaction installer, real rollback,
Xray, real Remote HUB service, UCI mutation, firewall changes, network
traffic or SSH configuration changes. It is opt-in, **not read-only**,
because it registers a temporary service. The command `--self-test`
runs only a local fake deadline worker and never calls `ubus`; the
`--run` mode requires separate operator execution after a successful CI.

Post-run safety checklist:

- `ubus call service list '{"name":"ro-procd-dual-..."}'` must no longer
  contain the exact test service (unique name comes from script output).
- Existing `owrt-remote` procd service still has `heartbeat` and `xray`.
- The synthetic marker-only rollback is **not proof** that real procd
  supervision can perform full transaction rollback. It merely verifies
  two independent managed instances and a timer under real OpenWrt.

Do not unlock production `apply`, merge the draft PR, update other
routers or remove Belt's HOLD based on these results.

## Cudy dual-instance real-procd test: PASS (2026-11-05)

The operator executed the commit-pinned dual-instance probe at
`c9835c495da6c8b9262f79c4ca59fda825d70021` on the
Cudy WBR3000UAX v1 (OpenWrt 24.10.5). **Actual on-device stdout**:

- Offline marker-only secondary deadline: `PASS`.
- Dynamically registered isolated test service:
  `ro-procd-dual-9e316732`, with primary PID `23682` and secondary
  PID `23683`.
- `ubus service signal` forcibly terminated only the primary test
  instance. procd relaunched primary `23682 -> 23706`: `PASS`.
- The secondary independently expired the same original fictitious
  deadline and deleted the **FAKE** `pending` marker: `PASS`.
- The script requested service deletion, and an independent
  `ubus call service list` check returned
  `PASS: test services removed`.
- The original `owrt-remote` service still listed both
  `heartbeat` and `xray` instances.
- Router-reported `jq` is compiled without ONIGURUMA; its
  `test`/`match`/`sub` builtins are unavailable. External
  `awk '/^ro-procd-(probe|dual)-/'` worked for checking residual
  temporary services. Do not use `jq test()` on this router.

**What is proven:** two separate real procd-managed instances can
coexist, primary SIGKILL/respawn works, secondary follows an independent
synthetic deadline, and its temporary service can be cleaned up without
removing the original Remote HUB instances.

**What is NOT proven:** a real `procd`-managed secondary performing
rollback of all 10 synthetic files in one connected hardware test,
automatic recovery when the secondary itself crashes, real power-cycle
safety, service activation of new agent files, and end-to-end
Belt-originated SSH/LuCI probes after activation. All real upgrade
paths remain explicitly disabled. Do not interpret hardware
marker-only PASS as approval to install the new client.

## Follow-up hardware gate: secondary instance SIGKILL/respawn

New **explicitly opt-in** `--run-secondary-crash` mode in
`tools/openwrt_procd_dual_isolated_probe.sh` validates the missing
failure direction on a newly created `ro-procd-dual-<random>`
temporary test service. The first on-device real procd test
(`ro-procd-dual-9e316732`) established primary restart and
independent marker-only secondary timer; this follow-up instead:

- Starts separately supervised primary and secondary instances.
- Sends SIGKILL *only* to that test service's `secondary` instance via
  `ubus call service signal`.
- Requires secondary instance to restart with a different PID and
  exactly two secondary starts; verifies primary did not restart.
- Requires the restarted secondary to expire the **same original
  fictitious deadline** and clear the `/tmp`-only `pending` marker.
- Removes the ephemeral service and marker files. Safety cleanup
  instance independently requests service deletion after 75 seconds.

This **does not** update files, register a persistent init script,
use the transaction rollback code, touch existing Remote HUB/Xray
instances, or reboot. Only run after the matching commit's CI is
green and read-only residual-service check is clean.

## Cudy secondary-crash test: restart PASS, deadline observation INCONCLUSIVE (2026-11-05)

Operator ran commit `034189c996e9083755c8e7803a32918a11ce5d81`:

- Offline secondary marker-only self-test: **PASS**.
- Isolated real procd service `ro-procd-dual-2bedb7af`,
  initial primary PID `31328`, secondary PID `31329`.
- The actual test secondary was killed and relaunched by procd:
  `31329 -> 31350`: **PASS**.
- The test then printed
  `PROCD-DUAL: FAIL - independent secondary deadline did not fire`.
  It did **not** report a successful fake rollback. This is **not**
  evidence that a live client rollback failed: there was no live
  client upgrade and no real rollback in this test.

### Root-cause analysis and next revision

The script pinned its fake deadline to `/proc/uptime + 16s` *before*
procd registration. After observing the restarted secondary, it checked
for only 13 one-second poll iterations. A ~1–2s respawn delay plus
registration overhead and integer uptime rounding could make the
observer exit **before** the original deadline. This timing bug is
the most likely explanation, but the old test lacked worker logs,
so the precise reason is not established.

The updated script therefore polls until the original absolute
deadline **plus 8 seconds grace**, with a separate bounded iteration
limit, and prints read-only event diagnostics on failure. The
secondary worker records its start and expiry uptime values under
the isolated `/tmp` fixture. A new offline test kills and relaunches
the real secondary worker and asserts that it preserves the deadline
and clears only fake pending. The emergency procd cleanup timer is
extended to 75s so it cannot prematurely remove the test service
during the observation window.

Do not rerun the old pinned commit; use only a **fully green**,
matching new SHA and the existing residual-service precheck.
The test remains fixture-only. Real `apply`, Belt, Xray and working
Remote HUB services are unchanged and still off limits.

## Step 3: after the guarded pilot

- If `main` passes end-to-end LuCI/SSH tests, keep it on `TCP none`.
- Defer its WSS migration to a separate controlled window using server-side
  temporary dual authorization and a router-local fallback.
- Keep other routers unchanged until a stable rollout channel, compatibility
  matrix and rollback tests exist.
- Continue to keep Belt's Xray 26.3.27 and maintenance hold unchanged.

Live updates are NOT performed by this document or any GitHub commit.
