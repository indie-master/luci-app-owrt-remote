#!/bin/sh
# Synthetic-only bridge from a two-phase guarded transaction to REAL procd.
# Never allow this experimental bridge to register any worker against /.
# The live transaction is still hard-disabled in this branch.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
die() { printf 'PILOT-PROCD: BLOCKED - %s\n' "$*" >&2; exit 1; }
say() { printf 'PILOT-PROCD: %s\n' "$*"; }

[ "${RO_TEST_MODE:-0}" = 1 ] || die 'production supervisor not enabled'
action="${1:-}"
case "$action" in start|stop|emergency-cleanup) ;; *) die 'usage: start | stop | emergency-cleanup ROOT NAME' ;; esac
if [ "$action" = emergency-cleanup ]; then
    root="${2:-}"
else
    root="${RO_TEST_ROOT:-}"
fi
case "$root" in
    /tmp/ro-transaction-test-*/router|/tmp/ro-pilot-fixture.*/router) ;;
    *) die 'not a permitted temporary fake router' ;;
esac
[ -d "$root" ] && [ ! -L "$root" ] ||
    die 'fake router root is unsafe'
[ "$(cd -P "$root" && pwd -P)" = "$root" ] ||
    die 'fake router root is not canonical'
[ -f "$root/.ro-pilot-synthetic-root" ] &&
[ ! -L "$root/.ro-pilot-synthetic-root" ] ||
    die 'synthetic-root marker missing'
state="$root/etc/router-orchestrator/upgrade"
[ -d "$state" ] && [ ! -L "$state" ] || die 'transaction state missing'
prefix="$root"
for component in etc router-orchestrator upgrade; do
    prefix="$prefix/$component"
    [ ! -L "$prefix" ] || die 'nested symlink in fake router state'
done
command -v ubus >/dev/null 2>&1 || die 'ubus unavailable'
command -v jq >/dev/null 2>&1 || die 'jq unavailable'

service_present() {
    query="$(jq -nc --arg n "$1" '{name:$n,verbose:true}')" ||
        die 'cannot encode service query'
    ubus call service list "$query"
}
delete_service() {
    request="$(jq -nc --arg n "$1" '{name:$n}')" ||
        die 'cannot encode service deletion'
    ubus call service delete "$request"
}
# ubus service/delete acknowledges a deletion request, not necessarily
# the full asynchronous shutdown of every procd-managed instance. Never
# remove our recovery identity or call the test PASS until service/list no
# longer exposes the exact name. Fail closed on ubus or JSON errors.
service_absent() {
    seen="$(service_present "$1")" || return 1
    [ -n "$seen" ] || return 1
    printf '%s\n' "$seen" |
        jq -e --arg n "$1" 'type == "object" and (has($n) | not)' >/dev/null 2>&1
}
wait_deleted() {
    expected="$1"
    max_checks="${2:-25}"
    # Tests can shorten the polling bound without changing the normal
    # OpenWrt limit. Valid only for this marked fake-root coordinator.
    if [ -n "${RO_TEST_MAX_DELETE_POLLS:-}" ]; then
        max_checks="$RO_TEST_MAX_DELETE_POLLS"
        case "$max_checks" in ''|*[!0-9]*) die 'invalid test delete poll limit' ;; esac
        [ "$max_checks" -ge 1 ] && [ "$max_checks" -le 25 ] ||
            die 'test delete poll limit outside 1..25'
    fi
    checks=0
    while [ "$checks" -lt "$max_checks" ]; do
        if service_absent "$expected"; then
            return 0
        fi
        sleep 1
        checks=$((checks+1))
    done
    return 1
}
name_ok() {
    case "$1" in
      ro-pilot-supervised-????????)
          case "${1#ro-pilot-supervised-}" in
              *[!0-9a-f]*|'') return 1 ;;
              *) return 0 ;;
          esac ;;
      *) return 1 ;;
    esac
}

if [ "$action" = emergency-cleanup ]; then
    name="${3:-}"
    name_ok "$name" || die 'untrusted emergency service name'
    # Detached in procd. Only remove test supervision once a terminal
    # state is reached. If rollback fails, DO NOT abandon a pending update.
    sleep 90
    if [ -f "$state/pending" ]; then
        RO_TEST_MODE=1 RO_TEST_ROOT="$root" RO_TEST_ROUTER_ID=main \
        RO_TEST_TRANSPORT=tcp RO_TEST_PORT=18443 \
            /bin/sh "$state/transaction.sh" rollback >/dev/null 2>&1 || exit 1
    fi
    if [ -f "$state/armed" ]; then
        RO_TEST_MODE=1 RO_TEST_ROOT="$root" RO_TEST_ROUTER_ID=main \
        RO_TEST_TRANSPORT=tcp RO_TEST_PORT=18443 \
            /bin/sh "$state/transaction.sh" abort-supervised >/dev/null 2>&1 || exit 1
    fi
    [ -f "$state/rolled-back" ] || [ -f "$state/confirmed" ] || exit 1
    delete_service "$name" >/dev/null 2>&1 || true
    exit 0
fi

if [ "$action" = stop ]; then
    [ -s "$state/procd-service-name" ] ||
        die 'no recorded temporary supervisor service'
    name="$(cat "$state/procd-service-name")"
    name_ok "$name" || die 'invalid recorded service name'
    [ ! -e "$state/pending" ] ||
        die 'pending transaction: must not stop rollback supervision'
    [ ! -e "$state/armed" ] ||
        die 'armed transaction: must not stop rollback supervision'
    [ -f "$state/confirmed" ] || [ -f "$state/rolled-back" ] ||
        die 'no terminal transaction state'
    if ! service_absent "$name"; then
        delete_service "$name" || die 'supervised service deletion request failed'
        wait_deleted "$name" 25 ||
            die "service delete acknowledged but still listed after polling limit: $name; identity preserved"
    fi
    # Only now is it safe for the caller to remove the fake router tree.
    rm -f "$state/procd-service-name" ||
        die 'temporary supervisor identity could not be cleared'
    say 'PASS - supervised fake-router service removed and absence verified'
    exit 0
fi

[ "$#" -eq 1 ] || die 'start takes no positional arguments'
[ -f "$state/armed" ] && [ ! -f "$state/pending" ] ||
    die 'arm-supervised must run before registering watchdogs'
[ -f "$state/transaction.sh" ] &&
[ ! -L "$state/transaction.sh" ] ||
    die 'transaction worker missing'
[ -f "$state/secondary-guard.sh" ] &&
[ ! -L "$state/secondary-guard.sh" ] ||
    die 'secondary worker missing'
[ -s "$state/deadline.uptime" ] || die 'pinned deadline missing'
[ ! -e "$state/procd-service-name" ] ||
    die 'service already registered'
id="$(dd if=/dev/urandom bs=4 count=1 2>/dev/null |
    hexdump -v -e '4/1 "%02x"')" || die 'no random service id'
[ "${#id}" -eq 8 ] || die 'invalid service nonce'
case "$id" in ''|*[!0-9a-f]*) die 'non-hex service nonce' ;; esac
name="ro-pilot-supervised-$id"
existing="$(service_present "$name")" ||
    die 'cannot verify procd service name uniqueness'
if printf '%s\n' "$existing" | jq -e --arg n "$name" 'has($n)' >/dev/null 2>&1; then
    die 'procd service name collision'
fi

self="$(cd -P "$(dirname "$0")" && pwd -P)/$(basename "$0")" ||
    die 'cannot resolve coordinator path'
[ -f "$self" ] && [ ! -L "$self" ] ||
    die 'coordinator must be a regular file'
# This is an exact JSON table accepted by procd's service/add API.
payload="$(jq -nc --arg n "$name" --arg r "$root" --arg st "$state" --arg script "$self" '{
  name:$n,
  instances:{
    watchdog:{
      command:["/bin/sh",($st+"/transaction.sh"),"watchdog"],
      env:{RO_TEST_MODE:"1",RO_TEST_ROOT:$r,RO_TEST_ROUTER_ID:"main",RO_TEST_TRANSPORT:"tcp",RO_TEST_PORT:"18443"},
      respawn:[3600,2,0]
    },
    secondary:{
      command:["/bin/sh",($st+"/secondary-guard.sh")],
      env:{RO_TEST_MODE:"1",RO_TEST_ROOT:$r,RO_TEST_ROUTER_ID:"main",RO_TEST_TRANSPORT:"tcp",RO_TEST_PORT:"18443"},
      respawn:[3600,2,0]
    },
    safety_cleanup:{
      command:["/bin/sh",$script,"emergency-cleanup",$r,$n],
      env:{RO_TEST_MODE:"1"}
    }
  }
}')" || die 'failed to build procd JSON'
# Persist the exact service identifier before registration. If ubus
# rejects add or SSH disconnects immediately afterwards, cleanup can still
# target this one generated name without enumerating unrelated services.
printf '%s\n' "$name" > "$state/procd-service-name" ||
    die 'cannot record supervisor identity'
ubus call service add "$payload" >/dev/null ||
    die 'cannot register procd supervisors: identity preserved for cleanup'
i=0
while [ "$i" -lt 8 ]; do
    if [ -f "$state/watchdog-ready" ] && [ -f "$state/secondary-ready" ]; then
        status="$(service_present "$name")" || die 'service status unavailable'
        if printf '%s\n' "$status" | jq -e --arg n "$name" '
            .[$n].instances.watchdog.running == true and
            .[$n].instances.secondary.running == true' >/dev/null 2>&1; then
            say "PASS - both supervised workers ready: $name"
            exit 0
        fi
    fi
    sleep 1
    i=$((i+1))
done
die 'supervisor readiness failed; do NOT run apply-supervised (arm intact)'
