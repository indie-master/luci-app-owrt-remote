# Router Orchestrator client release plan — existing fleet and new OpenWrt

**Current state: release candidate preparation, not permission for fleet deployment.**
Use the original v119-based fork; preserve every existing reverse UUID, route,
client configuration, port, transport and HUB API. No Belt changes, no
`.xray-maintenance-hold` bypass and no Xray-core upgrade are part of
this client release.

## Two installation paths

### A — Existing managed router (including Cudy `main`)

**Never run the bare `install.sh` or the Hub's legacy “Update all routers”
command as a substitute for a guarded transaction.** The existing installer
replaces files one by one, restarts selected management services, and does
not restore the entire original bundle if remote access is lost.

The ten-file candidate bundle is built by
`tools/build_router_bundle.py`, with no UCI or Xray JSON included.
The guarded updater must validate SHA-256, seal original files, ensure
both supervised watchdog workers are ready *before* touching files,
write a durable `pending` marker, perform atomic replacements,
and auto-restore if the Belt acceptance check misses the fixed deadline.
A reboot while pending must restore the original files before the regular
owrt-remote service starts. After confirmation, remove the watchdog
instances and recovery hook safely. Do not signal or restart the Xray
instance as a side effect.

**Supervised transaction work in this branch:** the synthetic-only
`arm-supervised SHA256` command now validates the sealed backup and candidate,
creates the original absolute deadline and boot recovery hook, and arms the
watchdogs **without modifying the ten client files**. After a supervisor
starts both processes and they write distinct `watchdog-ready` and
`secondary-ready` acknowledgments, `apply-supervised SHA256` rechecks
the deadline, integrity and originals under the transaction lock,
then replaces the bundle under a durable `pending` marker. Missing
either acknowledgment prevents all mutation. Independent synthetic tests
cover both missing-ready cases, real worker readiness and rollback.
These commands remain hard-blocked on real OpenWrt and are **not** a
production updater or permission to install on `main`.

The new `tools/router_pilot_procd_coordinator.sh` is likewise
**synthetic-only**, restricted to marked and canonical `/tmp` router
fixtures. It registers primary and secondary instances with the
real procd `service add` JSON schema, verifies both readiness
markers **and** both running-instance statuses, and refuses to remove
the service while the transaction is `armed` or `pending`. An
independent emergency cleanup is scheduled only after a bounded
recovery interval. CI uses a fake `ubus` implementation to validate
the exact payload and to exercise arm → coordinated readiness →
apply-supervised → rollback → stop, without touching production.

### Correction: first combined Cudy run proved shell supervision, NOT real procd

Cudy operator executed the combined `--run-procd` command pinned to
`2984b9e72b6877821c8a38a1b45cf99dca8f5e01`. Its output showed
all ten fake files restored, unmodified real agent/UCI/Xray SHA-256,
and existing `heartbeat` and `xray` instances. **However**, it did
not show `both real procd workers ready` or
`temporary real procd service removed`.

Audit found a POSIX-shell global-variable collision: `create_fixture()`
used `mode=0755`/`mode=0644` for file permission bits, overwriting
the script's `mode=--run-procd`. The combined test consequently ran
the offline shell watchdog branch and incorrectly appeared to pass
the *real procd* gate. No production files were changed. Thus the
first combined hardware run is **synthetic-worker PASS, real procd
gate NOT TESTED**.

Revised the script to use distinct `cycle_mode` and `file_mode`
variables and added a CI regression that mocks `ubus` and rejects
`service add`: explicit `--run-procd` must fail closed after an
attempted real service registration, never fall back to shell workers
or print a false full-test PASS. The new hardware run must include
both missing procd-specific PASS lines before this gate is accepted.

### Cudy result: ten-file synthetic rollback PASS; real procd mode NOT established

The operator's 2026-11-05 Cudy output reports `STAGED`,
`SNAPSHOT READY`, `SUPERVISED ARM READY`,
`SUPERVISED APPLIED ON DISK`, and successful restoration of all ten
fake client files, with unchanged live agent/UCI/Xray hashes. The outer
check also reports no temporary supervised service and preserved
`heartbeat` and `xray` instances.

**Important evidence gap:** the output does **not** include
`SYNTHETIC-CYCLE: PASS - both real procd workers ready: ...` or
`SYNTHETIC-CYCLE: PASS - temporary real procd service removed`,
which are unconditionally emitted by the `--run-procd` branch of
the exact pinned source script. The observed output instead matches
`--self-test-procd` (ordinary shell workers). Therefore *do not mark*
the full real procd + ten-file integration hardware gate as PASS.
Require a follow-up run with an explicitly visible `--run-procd`
command and both procd-specific PASS lines before proceeding.

This discrepancy does not negate the earlier real-procd marker-only
tests or the full ten-file synthetic rollback tests. The working
router never had its original client updated in this rehearsal.

**Combined procd/ten-file hardware rehearsal prepared:**
`tools/openwrt_synthetic_update_cycle.sh --run-procd` creates a unique
marked `/tmp/ro-pilot-fixture.XXXXXX/router` fake OpenWrt tree, builds
the ten-file candidate and sealed backup, performs `arm-supervised`,
registers both watchdogs as a **temporary** real procd service through
`router_pilot_procd_coordinator.sh`, checks that both are ready,
then uses `apply-supervised` to install the candidate into the FAKE
root. It deliberately withholds confirmation and checks that the
original absolute timeout restores all ten original files; both fake
UCI and fake Xray JSON must stay unchanged. After a terminal rollback,
it deletes and checks the temporary procd service and compares hashes
of three **real** router files before/after without opening or copying
their content. No actual owrt-remote, Xray, WAN or Belt services are
restarted or changed.

An offline `--self-test-procd` variant uses shell child workers
without `ubus` and runs in CI with POSIX and BusyBox ash. Real `--run-procd`
MUST be executed only by the operator on Cudy after a full green,
commit-pinned CI run. If cleanup is uncertain, the script preserves its
temporary root for safe diagnosis instead of deleting the files
still needed by procd. A separate emergency worker retries synthetic
rollback before removing its temporary service.

**Remaining bridge:** independently pass `--run-procd` on physical Cudy.
Then implement separately audited production supervisor/boot recovery,
activate and rollback the *running* heartbeat without touching Xray,
and perform the Belt-originated acceptance gate. The live updater
remains hard-disabled until all these gates pass.

**Release blockers (not yet completed):**
- The experimental `tools/router_pilot_transaction.sh` deliberately
  rejects live operation; its background process is synthetic-only.
- Real procd registration and both readiness handshakes are not wired
  into the actual transaction entry point.
- The early boot recovery hook still needs a tested production
  implementation, including power loss between install stages.
- Starting the **new running agent** without restarting the separate
  Xray reverse process and checking rollback after activation needs a
  dedicated integration test and one physical-device pilot.
- End-to-end post-activation proof must be obtained from Belt:
  live LuCI, SSH banner, both reverse links, heartbeat and unchanged
  UCI/Xray hashes, then explicit one-time confirmation.
- A remote rollback must also restore **runtime behavior** (not only
  on-disk files), and prevent a stale new worker from continuing to run.

The operator's on-device tests have already proven fake-root bundle
apply/restore, primary and secondary procd SIGKILL/respawn, fixed-deadline
secondary handling, and cleanup without disturbing live `heartbeat`
and `xray`. Those are necessary but not sufficient for a real upgrade.

### B — Never-enrolled / fresh OpenWrt

`tools/router_fresh_install.sh` has a separate `--check` preflight
and an explicit `--install FULL_40_HEX_COMMIT` operation.

- Refuses an existing agent, UCI, Xray remote-client JSON, web key or
  unfinished upgrade and never adopts an old managed router.
- Checks filesystem path symlinks, available overlay, required commands,
  and an OpenWrt root environment.
- Downloads the entire pinned file set under a temporary staging root
  **before** running the installer. No floating `main` branch is used.
- Uses the existing v119-compatible installer and preserves its behavior.
  Optional WOL package installations are skipped; a real fresh install
  still prepares Xray and may restart `rpcd` / `uhttpd`.
- Initial installation may still partially fail. It is for a router
  with **local recovery access**, not remote replacement of an existing
  reverse tunnel. A successful installation is not yet enrollment in HUB.

This new script is evaluated in CI against a **synthetic /tmp router**
and existing-client/non-canonical-path rejection. Do not use it on a
production device until the matching commit's checks are green and its
fresh-hardware validation has been completed. Local installation
requires separately provisioning the correct HUB-issued UCI settings
and verifying reverse connectivity.

## CI-built immutable candidate

The `wss-tests` workflow creates a deterministic archive and SHA-256
file only after its tests pass. Artifact:

`openwrt-client-candidate-<full GitHub commit SHA>`

Contents:
- `owrt-remote-client-candidate.tar`
- `owrt-remote-client-candidate.tar.sha256`
- `RELEASE-INFO.txt` (source commit and explicit non-deployable status)

Validate with `sha256sum -c` against the sibling checksum file.
**Important:** GitHub Actions triggered by `pull_request` check out a
*temporary PR merge commit*, not necessarily the feature-branch HEAD.
The artifact name uses this actual checkout SHA; the metadata now
includes `CHECKOUT_SHA`, event type and the original `PR_HEAD_SHA`,
and marks PR merge artifacts `PR_MERGE_TEST_TREE=YES`, `DEPLOYABLE=NO`.
Never use a PR-merge artifact as a production release. When the
branch is frozen and reviewed, explicitly dispatch the workflow at
an immutable ref, verify its checkout SHA and both green jobs,
then separately approve its bundle for a canary.

An artifact is **not** authorization to execute production `apply`.
The full workflow, including the Xray compatibility job, must be green.

## 2026-11-06 integrated Cudy result and consolidated fixes

Actual Cudy `--run-procd` hardware log confirmed:

- Both real procd workers ready: `ro-pilot-supervised-a55d4486`.
- All ten **fake** files installed under the marked `/tmp` router root.
- Both safeguards successfully restored the original ten fake files after
  the pinned deadline; hashes of actual agent, UCI and Xray did not change.
- The coordinator requested `service delete` and printed cleanup success,
  but the immediate separate `ubus service list` still showed the
  `ro-pilot-supervised-a55d4486` service. Therefore **end-to-end
  temporary-service cleanup was NOT proven** in that run. Real
  production update remained disabled.

Inspection of OpenWrt's procd service deletion shows the request may
complete before all instances terminate and the service disappears from
the list. `router_pilot_procd_coordinator.sh` now polls a bounded
25 times, using an exact service-name lookup, and deletes the persisted
temporary service ID **only after actual absence is observed**. If
deletion is delayed beyond the limit, it fails closed and preserves
the ID and fixture. CI simulates both delayed and permanently stuck
service removal.

Additional audit found the supervised transaction's temporary
destination paths had literal `-# Router Orchestrator client release plan — existing fleet and new OpenWrt

**Current state: release candidate preparation, not permission for fleet deployment.**
Use the original v119-based fork; preserve every existing reverse UUID, route,
client configuration, port, transport and HUB API. No Belt changes, no
`.xray-maintenance-hold` bypass and no Xray-core upgrade are part of
this client release.

## Two installation paths

### A — Existing managed router (including Cudy `main`)

**Never run the bare `install.sh` or the Hub's legacy “Update all routers”
command as a substitute for a guarded transaction.** The existing installer
replaces files one by one, restarts selected management services, and does
not restore the entire original bundle if remote access is lost.

The ten-file candidate bundle is built by
`tools/build_router_bundle.py`, with no UCI or Xray JSON included.
The guarded updater must validate SHA-256, seal original files, ensure
both supervised watchdog workers are ready *before* touching files,
write a durable `pending` marker, perform atomic replacements,
and auto-restore if the Belt acceptance check misses the fixed deadline.
A reboot while pending must restore the original files before the regular
owrt-remote service starts. After confirmation, remove the watchdog
instances and recovery hook safely. Do not signal or restart the Xray
instance as a side effect.

**Supervised transaction work in this branch:** the synthetic-only
`arm-supervised SHA256` command now validates the sealed backup and candidate,
creates the original absolute deadline and boot recovery hook, and arms the
watchdogs **without modifying the ten client files**. After a supervisor
starts both processes and they write distinct `watchdog-ready` and
`secondary-ready` acknowledgments, `apply-supervised SHA256` rechecks
the deadline, integrity and originals under the transaction lock,
then replaces the bundle under a durable `pending` marker. Missing
either acknowledgment prevents all mutation. Independent synthetic tests
cover both missing-ready cases, real worker readiness and rollback.
These commands remain hard-blocked on real OpenWrt and are **not** a
production updater or permission to install on `main`.

The new `tools/router_pilot_procd_coordinator.sh` is likewise
**synthetic-only**, restricted to marked and canonical `/tmp` router
fixtures. It registers primary and secondary instances with the
real procd `service add` JSON schema, verifies both readiness
markers **and** both running-instance statuses, and refuses to remove
the service while the transaction is `armed` or `pending`. An
independent emergency cleanup is scheduled only after a bounded
recovery interval. CI uses a fake `ubus` implementation to validate
the exact payload and to exercise arm → coordinated readiness →
apply-supervised → rollback → stop, without touching production.

### Correction: first combined Cudy run proved shell supervision, NOT real procd

Cudy operator executed the combined `--run-procd` command pinned to
`2984b9e72b6877821c8a38a1b45cf99dca8f5e01`. Its output showed
all ten fake files restored, unmodified real agent/UCI/Xray SHA-256,
and existing `heartbeat` and `xray` instances. **However**, it did
not show `both real procd workers ready` or
`temporary real procd service removed`.

Audit found a POSIX-shell global-variable collision: `create_fixture()`
used `mode=0755`/`mode=0644` for file permission bits, overwriting
the script's `mode=--run-procd`. The combined test consequently ran
the offline shell watchdog branch and incorrectly appeared to pass
the *real procd* gate. No production files were changed. Thus the
first combined hardware run is **synthetic-worker PASS, real procd
gate NOT TESTED**.

Revised the script to use distinct `cycle_mode` and `file_mode`
variables and added a CI regression that mocks `ubus` and rejects
`service add`: explicit `--run-procd` must fail closed after an
attempted real service registration, never fall back to shell workers
or print a false full-test PASS. The new hardware run must include
both missing procd-specific PASS lines before this gate is accepted.

### Cudy result: ten-file synthetic rollback PASS; real procd mode NOT established

The operator's 2026-11-05 Cudy output reports `STAGED`,
`SNAPSHOT READY`, `SUPERVISED ARM READY`,
`SUPERVISED APPLIED ON DISK`, and successful restoration of all ten
fake client files, with unchanged live agent/UCI/Xray hashes. The outer
check also reports no temporary supervised service and preserved
`heartbeat` and `xray` instances.

**Important evidence gap:** the output does **not** include
`SYNTHETIC-CYCLE: PASS - both real procd workers ready: ...` or
`SYNTHETIC-CYCLE: PASS - temporary real procd service removed`,
which are unconditionally emitted by the `--run-procd` branch of
the exact pinned source script. The observed output instead matches
`--self-test-procd` (ordinary shell workers). Therefore *do not mark*
the full real procd + ten-file integration hardware gate as PASS.
Require a follow-up run with an explicitly visible `--run-procd`
command and both procd-specific PASS lines before proceeding.

This discrepancy does not negate the earlier real-procd marker-only
tests or the full ten-file synthetic rollback tests. The working
router never had its original client updated in this rehearsal.

**Combined procd/ten-file hardware rehearsal prepared:**
`tools/openwrt_synthetic_update_cycle.sh --run-procd` creates a unique
marked `/tmp/ro-pilot-fixture.XXXXXX/router` fake OpenWrt tree, builds
the ten-file candidate and sealed backup, performs `arm-supervised`,
registers both watchdogs as a **temporary** real procd service through
`router_pilot_procd_coordinator.sh`, checks that both are ready,
then uses `apply-supervised` to install the candidate into the FAKE
root. It deliberately withholds confirmation and checks that the
original absolute timeout restores all ten original files; both fake
UCI and fake Xray JSON must stay unchanged. After a terminal rollback,
it deletes and checks the temporary procd service and compares hashes
of three **real** router files before/after without opening or copying
their content. No actual owrt-remote, Xray, WAN or Belt services are
restarted or changed.

An offline `--self-test-procd` variant uses shell child workers
without `ubus` and runs in CI with POSIX and BusyBox ash. Real `--run-procd`
MUST be executed only by the operator on Cudy after a full green,
commit-pinned CI run. If cleanup is uncertain, the script preserves its
temporary root for safe diagnosis instead of deleting the files
still needed by procd. A separate emergency worker retries synthetic
rollback before removing its temporary service.

**Remaining bridge:** independently pass `--run-procd` on physical Cudy.
Then implement separately audited production supervisor/boot recovery,
activate and rollback the *running* heartbeat without touching Xray,
and perform the Belt-originated acceptance gate. The live updater
remains hard-disabled until all these gates pass.

**Release blockers (not yet completed):**
- The experimental `tools/router_pilot_transaction.sh` deliberately
  rejects live operation; its background process is synthetic-only.
- Real procd registration and both readiness handshakes are not wired
  into the actual transaction entry point.
- The early boot recovery hook still needs a tested production
  implementation, including power loss between install stages.
- Starting the **new running agent** without restarting the separate
  Xray reverse process and checking rollback after activation needs a
  dedicated integration test and one physical-device pilot.
- End-to-end post-activation proof must be obtained from Belt:
  live LuCI, SSH banner, both reverse links, heartbeat and unchanged
  UCI/Xray hashes, then explicit one-time confirmation.
- A remote rollback must also restore **runtime behavior** (not only
  on-disk files), and prevent a stale new worker from continuing to run.

The operator's on-device tests have already proven fake-root bundle
apply/restore, primary and secondary procd SIGKILL/respawn, fixed-deadline
secondary handling, and cleanup without disturbing live `heartbeat`
and `xray`. Those are necessary but not sufficient for a real upgrade.

### B — Never-enrolled / fresh OpenWrt

`tools/router_fresh_install.sh` has a separate `--check` preflight
and an explicit `--install FULL_40_HEX_COMMIT` operation.

- Refuses an existing agent, UCI, Xray remote-client JSON, web key or
  unfinished upgrade and never adopts an old managed router.
- Checks filesystem path symlinks, available overlay, required commands,
  and an OpenWrt root environment.
- Downloads the entire pinned file set under a temporary staging root
  **before** running the installer. No floating `main` branch is used.
- Uses the existing v119-compatible installer and preserves its behavior.
  Optional WOL package installations are skipped; a real fresh install
  still prepares Xray and may restart `rpcd` / `uhttpd`.
- Initial installation may still partially fail. It is for a router
  with **local recovery access**, not remote replacement of an existing
  reverse tunnel. A successful installation is not yet enrollment in HUB.

This new script is evaluated in CI against a **synthetic /tmp router**
and existing-client/non-canonical-path rejection. Do not use it on a
production device until the matching commit's checks are green and its
fresh-hardware validation has been completed. Local installation
requires separately provisioning the correct HUB-issued UCI settings
and verifying reverse connectivity.

## CI-built immutable candidate

The `wss-tests` workflow creates a deterministic archive and SHA-256
file only after its tests pass. Artifact:

`openwrt-client-candidate-<full GitHub commit SHA>`

Contents:
- `owrt-remote-client-candidate.tar`
- `owrt-remote-client-candidate.tar.sha256`
- `RELEASE-INFO.txt` (source commit and explicit non-deployable status)

Validate with `sha256sum -c` against the sibling checksum file.
**Important:** GitHub Actions triggered by `pull_request` check out a
*temporary PR merge commit*, not necessarily the feature-branch HEAD.
The artifact name uses this actual checkout SHA; the metadata now
includes `CHECKOUT_SHA`, event type and the original `PR_HEAD_SHA`,
and marks PR merge artifacts `PR_MERGE_TEST_TREE=YES`, `DEPLOYABLE=NO`.
Never use a PR-merge artifact as a production release. When the
branch is frozen and reviewed, explicitly dispatch the workflow at
an immutable ref, verify its checkout SHA and both green jobs,
then separately approve its bundle for a canary.

An artifact is **not** authorization to execute production `apply`.
The full workflow, including the Xray compatibility job, must be green.

 suffixes instead of the current
shell PID `-$`. They have been corrected and validated against the
actual resulting Git blob, with a contract regression.

`tools/router_heartbeat_activation.sh` provides a separate instance
contract: `--inspect` is live read-only; `--activate` and
`--restore` work **ONLY** on uniquely named synthetic procd
services. It issues SIGTERM specifically to `heartbeat` and
requires a changed heartbeat PID and identical Xray PID.
Tests simulate both successful respawn and unexpected Xray PID changes.
**Real heartbeat activation remains disabled** until the pilot.

Finally `tools/openwrt_release_hardware_gate.sh` collects all
*remaining synthetic hardware acceptance* into one operator command.
Given a full pinned commit, it downloads its peer scripts, runs the real
procd plus ten-file fake-root rollback, checks all procd-specific output,
requires complete cleanup, and compares working Xray and heartbeat
PIDs before and after. It neither edits the real service nor runs a
production upgrade.

**The above is not fleet-ready on its own.** Before a single real
update on `main`, finish and review the production supervisor,
boot-time recovery, heartbeat-only activation/rollback and Belt-side
LuCI+SSH acceptance. Confirm that initial heartbeat activation and
the rollback path each preserve the existing reverse Xray PID.
Only then can the rollout progress from `main` to the other
routers one at a time.

## Rollout sequence when production updater is ready

1. Freeze one full source commit and record archive digest; verify no
   accidental Xray version change, transport migration or secret changes.
2. Use local recovery access to pilot existing Cudy `main` (TCP none
   on 18443), with Belt-originated acceptance probes and rollback
   rehearsed on a disposable test router.
3. If and only if this pilot remains healthy, canary a second
   independently recoverable TCP router; then separately canary WSS.
4. Roll out to further existing routers **one at a time**, with
   explicit stop-on-first-failure, verified rollback and no blind
   retry after SSH disconnect. Never reuse `main`'s identity/port
   assumptions for a different router.
5. Test first-time enrollment on a clean spare router using the
   fresh-only bootstrap, issued unique HUB credentials, and
   new-device onboarding steps.
6. Publish a stable immutable release/tag only after all acceptance
   gates are completed. The old installer and v119 contract remain
   available as a documented fallback.

A mass-update button must not claim success from installer exit code
alone. Require new code version, service health, both reverse paths and
Belt-side SSH/LuCI checks after any controlled activation. **No automatic
fleet rollout or Belt changes are authorized at this stage.**
