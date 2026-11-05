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
    # The management shell may have disconnected. The two workers still
    # restore pending files; this fallback removes ONLY this test service.
    sleep 90
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
    delete_service "$name" || die 'supervised service deletion failed'
    rm -f "$state/procd-service-name"
    say 'PASS - supervised fake-router service removed after terminal state'
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
[ -z "$(service_present "$name" | jq -r --arg n "$name" '.[$n].instances | keys[]?' 2>/dev/null || true)" ] ||
    die 'procd service name collision'

self="$0"
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
ubus call service add "$payload" >/dev/null ||
    die 'cannot register procd supervisors'
printf '%s\n' "$name" > "$state/procd-service-name"
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
