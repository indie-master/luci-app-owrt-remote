#!/bin/sh
# SYNTHETIC ONLY: Exercise original bundle stage/snapshot, on-disk apply,
# watchdog timeout, secondary guard and confirmation on fake OpenWrt files.
# Nothing under live /etc, /usr, /www or /var is written. No procd registration,
# service restart, ubus mutation, UCI change, SSH modification or Xray signal.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
cycle_mode="${1:-legacy}"
case "$cycle_mode" in legacy|--self-test-procd|--run-procd) ;;
  *) echo 'SYNTHETIC-CYCLE: invalid mode' >&2; exit 1 ;;
esac
[ "$#" -le 1 ] || exit 1

die() { printf 'SYNTHETIC-CYCLE: FAIL - %s\n' "$*" >&2; exit 1; }
ok() { printf 'SYNTHETIC-CYCLE: PASS - %s\n' "$*"; }
tool_dir="$(cd -P "$(dirname "$0")" && pwd -P)" || die 'tool folder unavailable'
for tool in router_bundle_guard.sh router_pilot_transaction.sh router_pilot_secondary_guard.sh router_pilot_procd_coordinator.sh; do
    [ -f "$tool_dir/$tool" ] && [ ! -L "$tool_dir/$tool" ] ||
        die "missing paired script: $tool"
    sh -n "$tool_dir/$tool" || die "invalid shell syntax: $tool"
done

# Record only digests: the real router files are NEVER copied or displayed.
live_before=''
for file in /usr/sbin/owrt-remote /etc/config/owrtremote /etc/xray/owrt-remote-client.json; do
    if [ -f "$file" ]; then
        live_before="$live_before$(sha256sum "$file")\n"
    fi
done

FIXTURES=''
cleanup() {
    for root in $FIXTURES; do
        case "$root" in
            /tmp/ro-pilot-fixture.*)
                [ -d "$root" ] && [ ! -L "$root" ] && rm -rf "$root" ;;
        esac
    done
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

FILES='usr/sbin/owrt-remote
usr/lib/owrt-remote/wan.sh
usr/lib/owrt-remote/logs.sh
etc/init.d/owrt-remote-logs
etc/init.d/owrt-remote
etc/hotplug.d/iface/99-owrt-remote
www/cgi-bin/owrt-remote
usr/share/luci/menu.d/luci-app-owrt-remote.json
usr/share/rpcd/acl.d/luci-app-owrt-remote.json
www/luci-static/resources/view/owrt_remote.js'
create_fixture() {
    top="$(mktemp -d /tmp/ro-pilot-fixture.XXXXXX)" ||
        die 'mktemp -d failed'
    case "$top" in /tmp/ro-pilot-fixture.*) ;; *) die 'unsafe temp path' ;; esac
    FIXTURES="$FIXTURES $top"
    root="$top/router"
    src="$top/source"
    mkdir -p "$root" "$src"
    : > "$root/.ro-pilot-synthetic-root"
    : > "$src/manifest.txt"
    set -- manifest.txt
    for rel in $FILES; do
        mkdir -p "$src/payload/$(dirname "$rel")" "$root/$(dirname "$rel")"
        printf 'FAKE-NEW-%s\n' "$rel" > "$src/payload/$rel"
        printf 'FAKE-OLD-%s\n' "$rel" > "$root/$rel"
        case "$rel" in
            usr/sbin/owrt-remote|etc/init.d/*|etc/hotplug.d/*|www/cgi-bin/*)
                file_mode=0755 ;;
            *) file_mode=0644 ;;
        esac
        digest="$(sha256sum "$src/payload/$rel" | awk '{print $1}')"
        printf '%s %s %s\n' "$digest" "$file_mode" "$rel" >> "$src/manifest.txt"
        set -- "$@" "payload/$rel"
    done
    mkdir -p "$root/etc/config" "$root/etc/xray"
    printf 'FAKE-ONLY-OPENWRT-UCI\n' > "$root/etc/config/owrtremote"
    printf 'FAKE-ONLY-XRAY-JSON\n' > "$root/etc/xray/owrt-remote-client.json"
    archive="$top/candidate.tar"
    tar -cf "$archive" -C "$src" "$@" || die 'candidate creation failed'
    digest="$(sha256sum "$archive" | awk '{print $1}')"
    export RO_TEST_MODE=1 RO_TEST_ROOT="$root" RO_TEST_ROUTER_ID=main \
           RO_TEST_TRANSPORT=tcp RO_TEST_PORT=18443
    unset RO_TEST_NOW
    RO_TEST_WATCHDOG_MODE=fixture-background
    export RO_TEST_WATCHDOG_MODE
    sh "$tool_dir/router_bundle_guard.sh" stage "$archive" "$digest" ||
        die 'stage refused fake bundle'
    sh "$tool_dir/router_bundle_guard.sh" snapshot ||
        die 'snapshot refused fake router'
    state="$root/etc/router-orchestrator/upgrade"
    cp "$tool_dir/router_pilot_secondary_guard.sh" "$state/secondary-guard.sh"
    [ -s "$state/backup/original.sha256" ] ||
        die 'sealed original backup missing'
}

assert_orig() {
    for rel in $FILES; do
        [ -f "$root/$rel" ] || die "fake original absent: $rel"
        cmp -s "$root/$rel" "$state/backup/files/$rel" ||
            die "rollback mismatch: $rel"
    done
    [ ! -e "$state/pending" ] || die 'pending remained after rollback'
    [ -f "$root/etc/config/owrtremote" ] ||
        die 'fake UCI unexpectedly missing'
}

assert_new() {
    for rel in $FILES; do
        cmp -s "$root/$rel" "$src/payload/$rel" ||
            die "candidate mismatch: $rel"
    done
    [ ! -e "$state/pending" ] || die 'pending remained after confirmation'
    [ -f "$state/confirmed" ] || die 'confirmed marker missing'
}

# Optional full transaction test: fake files + either detached test workers or
# actual supervised procd instances (with explicit --run-procd).
if [ "$cycle_mode" != legacy ]; then
  txn="$tool_dir/router_pilot_transaction.sh"
  coordinator="$tool_dir/router_pilot_procd_coordinator.sh"
  primary_pid=''
  secondary_pid=''
  if [ "$cycle_mode" = --run-procd ]; then
    command -v ubus >/dev/null 2>&1 || die 'ubus unavailable'
    command -v jq >/dev/null 2>&1 || die 'jq unavailable'
    ubus list | grep -Fxq service || die 'procd service API unavailable'
    previous="$(ubus call service list '{}' |
      jq -r 'keys[]' | grep '^ro-pilot-supervised-' || true)"
    [ -z "$previous" ] || die "a previous supervised test remains: $previous"
  fi

  cleanup_supervised() {
    rc="$?"
    trap - EXIT HUP INT TERM
    set +e
    if [ -n "${state:-}" ] && [ -d "$state" ]; then
      [ ! -f "$state/pending" ] ||
        sh "$txn" rollback >/dev/null 2>&1 || true
      if [ -f "$state/armed" ] && [ ! -f "$state/pending" ]; then
        sh "$txn" abort-supervised >/dev/null 2>&1 || true
      fi
      if [ "$cycle_mode" = --run-procd ] &&
         [ -s "$state/procd-service-name" ]; then
        sh "$coordinator" stop >/dev/null 2>&1 || true
      fi
    fi
    [ -z "$primary_pid" ] || kill "$primary_pid" 2>/dev/null || true
    [ -z "$secondary_pid" ] || kill "$secondary_pid" 2>/dev/null || true
    may_delete=1
    if [ -n "${state:-}" ] && [ -f "$state/pending" ]; then may_delete=0; fi
    if [ -n "${state:-}" ] &&
       [ -s "$state/procd-service-name" ]; then may_delete=0; fi
    if [ "$may_delete" = 1 ]; then
      cleanup
    else
      printf 'SYNTHETIC-CYCLE: WARNING - fixture retained at %s until rollback/cleanup\n' "$top" >&2
    fi
    exit "$rc"
  }
  trap cleanup_supervised EXIT
  trap 'exit 130' HUP INT TERM

  create_fixture
  unset RO_TEST_WATCHDOG_MODE RO_TEST_NOW RO_TEST_CONFIRM_CRASH_AT
  export RO_PILOT_TIMEOUT=30
  sh "$txn" arm-supervised "$digest" ||
    die 'cannot arm test transaction'
  [ ! -f "$state/pending" ] || die 'arm wrote pending too early'
  assert_orig
  ok 'ten original FAKE files intact after arming'

  if [ "$cycle_mode" = --run-procd ]; then
    sh "$coordinator" start ||
      die 'real procd did not start both watchdogs'
    ok "both real procd workers ready: $(cat "$state/procd-service-name")"
  else
    sh "$state/transaction.sh" watchdog > "$state/primary.log" 2>&1 &
    primary_pid="$!"
    sh "$state/secondary-guard.sh" > "$state/secondary.log" 2>&1 &
    secondary_pid="$!"
    i=0
    while [ "$i" -lt 8 ]; do
      [ -f "$state/watchdog-ready" ] &&
      [ -f "$state/secondary-ready" ] && break
      sleep 1
      i=$((i+1))
    done
    [ -f "$state/watchdog-ready" ] &&
    [ -f "$state/secondary-ready" ] ||
      die 'offline workers not ready'
  fi

  sh "$txn" apply-supervised "$digest" ||
    die 'supervised fake-root apply failed'
  for rel in $FILES; do
    cmp -s "$root/$rel" "$src/payload/$rel" ||
      die "fake candidate not applied: $rel"
  done
  ok 'two watchdogs ready before ten FAKE files changed'

  deadline="$(cat "$state/deadline.uptime")"
  case "$deadline" in ''|*[!0-9]*) die 'invalid fixed deadline' ;; esac
  limit=$((deadline+12))
  i=0
  while [ "$i" -lt 48 ]; do
    [ -f "$state/rolled-back" ] && [ ! -f "$state/pending" ] && break
    now="$(awk 'NR==1{print int($1)}' /proc/uptime)" ||
      die 'uptime unavailable'
    [ "$now" -lt "$limit" ] || break
    sleep 1
    i=$((i+1))
  done
  [ -f "$state/rolled-back" ] && [ ! -f "$state/pending" ] ||
    die "ten-file rollback exceeded fixed deadline $deadline"
  [ ! -f "$state/armed" ] || die 'rollback left armed marker'
  assert_orig
  for rel in etc/config/owrtremote etc/xray/owrt-remote-client.json; do
    cmp -s "$root/$rel" "$state/backup/files/$rel" ||
      die "fake critical configuration changed: $rel"
  done
  ok 'real transaction watchdog restored all ten FAKE client files'

  if [ "$cycle_mode" = --run-procd ]; then
    sh "$coordinator" stop || die 'procd temporary service deletion failed'
    remaining="$(ubus call service list '{}' |
      jq -r 'keys[]' | grep '^ro-pilot-supervised-' || true)"
    [ -z "$remaining" ] || die "procd service still registered: $remaining"
    ok 'temporary real procd service removed'
  fi
  live_after=''
  for file in /usr/sbin/owrt-remote /etc/config/owrtremote /etc/xray/owrt-remote-client.json; do
    [ ! -f "$file" ] || live_after="$live_after$(sha256sum "$file")\n"
  done
  [ "$live_after" = "$live_before" ] ||
    die 'live agent/UCI/Xray hash changed'
  ok 'real agent/UCI/Xray files unchanged'
  ok 'full supervised synthetic rollback completed'
  exit 0
fi

printf '%s\n' '===== ISOLATED SYNTHETIC OPENWRT UPDATE TEST ====='
printf '%s\n' 'All client and UCI/Xray files below are FAKE and live under /tmp.'

# Case A: primary watchdog rollback without confirmation.
create_fixture
RO_PILOT_TIMEOUT=4 sh "$tool_dir/router_pilot_transaction.sh" apply "$digest" ||
    die 'fake-root apply failed (timeout case)'
sleep 6
[ -f "$state/rolled-back" ] || die 'timeout did not trigger rollback'
assert_orig
ok 'unconfirmed synthetic install automatically rolled back'

# Case B: independently supervised secondary guard uses same original deadline.
create_fixture
RO_PILOT_TIMEOUT=20 sh "$tool_dir/router_pilot_transaction.sh" apply "$digest" ||
    die 'fake-root apply failed (secondary case)'
deadline="$(cat "$state/deadline.uptime")"
RO_TEST_NOW="$((deadline + 1))" \
    sh "$tool_dir/router_pilot_secondary_guard.sh" ||
    die 'independent guard failed synthetic rollback'
[ -f "$state/rolled-back" ] || die 'secondary guard did not restore'
assert_orig
ok 'independent deadline guardian restored original synthetic files'

# Case C: valid operator confirmation preserves the fake candidate.
create_fixture
RO_PILOT_TIMEOUT=20 sh "$tool_dir/router_pilot_transaction.sh" apply "$digest" ||
    die 'fake-root apply failed (confirm case)'
nonce="$(cat "$state/confirm-nonce")"
sh "$tool_dir/router_pilot_transaction.sh" confirm "$nonce" ||
    die 'fake-root confirm failed'
assert_new
ok 'confirmed synthetic candidate retained; UCI/Xray never changed'

live_after=''
for file in /usr/sbin/owrt-remote /etc/config/owrtremote /etc/xray/owrt-remote-client.json; do
    if [ -f "$file" ]; then
        live_after="$live_after$(sha256sum "$file")\n"
    fi
done
[ "$live_before" = "$live_after" ] ||
    die 'live file digest changed during synthetic test - investigate'
ok 'live agent/UCI/Xray digests unchanged'
printf '%s\n' 'SYNTHETIC-CYCLE: PASS - fake-root stage, backup, timeout rollback, secondary recovery, and confirm'
printf '%s\n' 'No production client, Xray, procd service, or router configuration changed.'
