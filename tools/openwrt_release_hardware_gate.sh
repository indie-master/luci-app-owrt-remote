#!/bin/sh
# One-command Cudy release acceptance: temporary real procd supervision,
# all 10 FAKE files under /tmp, and read-only live process fingerprints.
# It does NOT perform production upgrades or edit live client/UCI/Xray.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
fail() { printf 'RELEASE-GATE: BLOCKED - %s\n' "$*" >&2; exit 1; }
say() { printf 'RELEASE-GATE: %s\n' "$*"; }

mode="${1:-}"
case "$mode" in --run|--runtime-only) ;;
    *) fail 'usage: --run|--runtime-only EXACT_40_HEX_COMMIT' ;;
esac
[ "$#" -eq 2 ] ||
  fail 'usage: --run|--runtime-only EXACT_40_HEX_COMMIT'
rev="$2"
case "$rev" in ''|*[!0-9a-f]*) fail 'invalid commit' ;; esac
[ "${#rev}" -eq 40 ] || fail 'full immutable commit required'
for app in ubus jq wget sha256sum mktemp awk grep cat sh; do
  command -v "$app" >/dev/null 2>&1 || fail "missing $app"
done
ubus list | grep -Fxq service || fail 'procd service ubus API unavailable'

# Do not reuse names or auto-delete old test processes. A leftover service
# might still rely on its associated fake-root recovery files.
leftover="$(ubus call service list '{}' | jq -r 'keys[]' |
  awk '/^ro-(pilot-supervised|runtime-test|procd-(probe|dual))-/')"
[ -z "$leftover" ] || fail "old isolated procd service remains: $leftover"

main_before="$(ubus call service list '{"name":"owrt-remote"}')"
getpid() {
  printf '%s\n' "$1" | jq -er --arg instance "$2" '
    .["owrt-remote"].instances[$instance]
    | select(.running == true)
    | .pid | select(type=="number" and . > 1) | tostring
  '
}
xray_before="$(getpid "$main_before" xray)" ||
  fail 'working Xray not managed by procd'
heartbeat_before="$(getpid "$main_before" heartbeat)" ||
  fail 'working heartbeat not managed by procd'

work="$(mktemp -d /tmp/ro-release-hardware.XXXXXX)" ||
  fail 'failed to create temporary tool directory'
case "$work" in /tmp/ro-release-hardware.*) ;; *)
  fail 'unexpected tool directory' ;;
esac
directory_safe=1
cleanup() {
  exitcode="$?"
  trap - EXIT HUP INT TERM
  set +e
  surviving="$(ubus call service list '{}' 2>/dev/null |
    jq -r 'keys[]' 2>/dev/null |
    grep -E '^ro-(pilot-supervised|runtime-test)-' || true)"
  if [ -n "$surviving" ]; then
    directory_safe=0
    printf 'RELEASE-GATE: WARNING - test procd still registered: %s\n' "$surviving" >&2
    printf 'RELEASE-GATE: preserving tool directory: %s\n' "$work" >&2
  fi
  if [ "$directory_safe" = 1 ]; then
    case "$work" in /tmp/ro-release-hardware.*)
      [ -d "$work" ] && [ ! -L "$work" ] && rm -rf "$work" ;;
    esac
  fi
  exit "$exitcode"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

base="https://raw.githubusercontent.com/indie-master/luci-app-owrt-remote/$rev/tools"
for f in router_bundle_guard.sh router_pilot_transaction.sh \
         router_pilot_secondary_guard.sh router_pilot_procd_coordinator.sh \
         openwrt_synthetic_update_cycle.sh router_heartbeat_activation.sh \
         openwrt_runtime_heartbeat_probe.sh; do
  wget -O "$work/$f" "$base/$f" >/dev/null ||
    fail "failed to fetch pinned $f"
  [ -s "$work/$f" ] && [ ! -L "$work/$f" ] ||
    fail "unsafe or empty downloaded $f"
  sh -n "$work/$f" || fail "syntax error: $f"
done
# Safety check: this immutable revision MUST select real procd.
grep -Fq 'readonly cycle_mode' "$work/openwrt_synthetic_update_cycle.sh" ||
  fail 'old cycle script: mode is not immutable'
grep -Fq 'wait_deleted "$name"' "$work/router_pilot_procd_coordinator.sh" ||
  fail 'coordinator has no asynchronous teardown verification'

say "start: pinned commit $rev, mode=$mode"
say "pre-test Xray PID=$xray_before, heartbeat PID=$heartbeat_before"
log="$work/procd-acceptance.log"
if [ "$mode" = --runtime-only ]; then
  if sh "$work/openwrt_runtime_heartbeat_probe.sh" --run \
      "$work/router_heartbeat_activation.sh" > "$log" 2>&1; then
    cat "$log"
  else
    cat "$log"
    fail 'isolated heartbeat procd activation/restore failed'
  fi
  for phrase in \
    'PASS - targeted fake heartbeat activation, fake Xray PID unchanged' \
    'PASS - targeted fake heartbeat rollback activation, fake Xray PID unchanged' \
    'PASS - isolated real-procd heartbeat lifecycle'; do
    grep -Fq "$phrase" "$log" ||
      fail "runtime proof missing: $phrase"
  done
else
if sh "$work/openwrt_synthetic_update_cycle.sh" --run-procd > "$log" 2>&1; then
  cat "$log"
else
  cat "$log"
  fail "combined hardware rehearsal failed; see $log (if fixture retained)"
fi
for phrase in \
  'both real procd workers ready:' \
  'two watchdogs ready before ten FAKE files changed' \
  'real transaction watchdog restored all ten FAKE client files' \
  'temporary real procd service removed' \
  'real agent/UCI/Xray files unchanged' \
  'full supervised synthetic rollback completed'; do
  grep -Fq "$phrase" "$log" ||
    fail "required acceptance evidence missing: $phrase"
done
fi
after="$(ubus call service list '{"name":"owrt-remote"}')" ||
  fail 'cannot inspect original remote hub'
xray_after="$(getpid "$after" xray)" ||
  fail 'original Xray not running after isolated rehearsal'
heartbeat_after="$(getpid "$after" heartbeat)" ||
  fail 'original heartbeat not running after isolated rehearsal'
[ "$xray_before" = "$xray_after" ] ||
  fail "Xray PID changed ($xray_before -> $xray_after)"
[ "$heartbeat_before" = "$heartbeat_after" ] ||
  fail "heartbeat PID changed ($heartbeat_before -> $heartbeat_after)"

remaining="$(ubus call service list '{}' |
  jq -r 'keys[]' | grep -E '^ro-(pilot-supervised|runtime-test)-' || true)"
[ -z "$remaining" ] || fail "test procd still listed: $remaining"
say 'PASS - working Xray and heartbeat PIDs unchanged'
say 'PASS - temporary procd service absent'
if [ "$mode" = --runtime-only ]; then
  say 'PASS - real isolated procd heartbeat activation + rollback, no working process signaled'
else
  say 'PASS - combined hardware fake-root rollout/rollback gate'
fi
say 'NOTE - production deployment remains disabled; this is NOT a live client upgrade'
