#!/bin/sh
# Isolated heartbeat-only activation contract for Router Orchestrator.
# --inspect: read-only on any router; --activate/--restore: FAKE-ROOT ONLY.
# Never invoke init.d/owrt-remote restart/stop or signal the Xray instance.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
die() { printf 'HEARTBEAT-ACTIVATE: BLOCKED - %s\n' "$*" >&2; exit 1; }
say() { printf 'HEARTBEAT-ACTIVATE: %s\n' "$*"; }

action="${1:-}"
case "$action" in --inspect|--activate|--restore) ;; *)
  die 'usage: --inspect | --activate | --restore' ;;
esac
[ "$#" -eq 1 ] || die 'unexpected parameters'
command -v jq >/dev/null 2>&1 || die 'jq required'
command -v ubus >/dev/null 2>&1 || die 'ubus required'

if [ "${RO_TEST_MODE:-0}" = 1 ]; then
  root="${RO_TEST_ROOT:-}"
  case "$root" in /tmp/ro-runtime-test-*/router) ;;
    *) die 'not a marked /tmp runtime fixture' ;;
  esac
  [ -d "$root" ] && [ ! -L "$root" ] || die 'invalid test root'
  [ "$(cd -P "$root" && pwd -P)" = "$root" ] ||
    die 'test root not canonical'
  [ -f "$root/.ro-runtime-synthetic-root" ] &&
  [ ! -L "$root/.ro-runtime-synthetic-root" ] ||
    die 'test marker missing'
  service="${RO_TEST_SERVICE_NAME:-}"
  case "$service" in ro-runtime-test-????????)
    tail="${service#ro-runtime-test-}"
    case "$tail" in ''|*[!0-9a-f]*) die 'nonhex test service name' ;; esac ;;
    *) die 'test-only service name required' ;;
  esac
else
  [ -z "${RO_TEST_ROOT:-}" ] || die 'test root in live mode'
  [ -z "${RO_TEST_SERVICE_NAME:-}" ] || die 'test service in live mode'
  [ "$action" = --inspect ] ||
    die 'production heartbeat activation blocked until canary review'
  service=owrt-remote
  root=''
fi

state="$root/etc/router-orchestrator/upgrade"
if [ "$action" = --activate ]; then
  [ -f "$state/pending" ] && [ ! -L "$state/pending" ] ||
    die 'not in a pending synthetic transaction'
elif [ "$action" = --restore ]; then
  [ -f "$state/rolled-back" ] && [ ! -L "$state/rolled-back" ] ||
    die 'not in a rolled-back synthetic transaction'
fi

read_status() {
  request="$(jq -nc --arg n "$service" '{name:$n,verbose:true}')" ||
    die 'cannot encode service query'
  ubus call service list "$request"
}
read_pid() {
  json="$1"; instance="$2"
  printf '%s\n' "$json" | jq -er --arg n "$service" --arg i "$instance" '
    .[$n].instances[$i]
    | select(.running == true)
    | .pid | select(type == "number" and . > 1) | tostring
  '
}
before="$(read_status)" || die 'cannot read procd status'
xray_old="$(read_pid "$before" xray)" || die 'xray not running'
heartbeat_old="$(read_pid "$before" heartbeat)" || die 'heartbeat not running'
if [ "$action" = --inspect ]; then
  say "PASS - read-only service=$service xray_pid=$xray_old heartbeat_pid=$heartbeat_old"
  exit 0
fi

# A procd signal is targeted to only ONE named instance.
signal="$(jq -nc --arg n "$service" '{name:$n,instance:"heartbeat",signal:15}')" ||
  die 'cannot encode heartbeat-only SIGTERM'
ubus call service signal "$signal" >/dev/null ||
  die 'heartbeat-only SIGTERM rejected'
say "heartbeat-only signal submitted to $service"
i=0
while [ "$i" -lt 25 ]; do
  current="$(read_status)" || die 'procd status unavailable after signal'
  xray_now="$(read_pid "$current" xray)" ||
    die 'xray not running after heartbeat signal'
  [ "$xray_now" = "$xray_old" ] ||
    die "xray PID changed $xray_old -> $xray_now: stop rollout"
  heartbeat_now="$(read_pid "$current" heartbeat 2>/dev/null || true)"
  if [ -n "$heartbeat_now" ] && [ "$heartbeat_now" != "$heartbeat_old" ]; then
    say "PASS - heartbeat restarted $heartbeat_old -> $heartbeat_now"
    say "PASS - xray PID unchanged: $xray_old"
    exit 0
  fi
  i=$((i+1))
  sleep 1
done
die 'heartbeat did not respawn within 25s; xray never signaled'
