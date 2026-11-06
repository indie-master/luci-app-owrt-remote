#!/bin/sh
# Independent SECONDARY rollback deadline monitor.
# DEVELOPMENT / SYNTHETIC ROOTS ONLY. No live router operations permitted.
# Runs as a separate procd instance, NOT a child of the primary watchdog.
# The original legacy v119 agent, Xray and network are never touched.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"

die() { printf 'SECONDARY-GUARD: BLOCKED - %s\n' "$*" >&2; exit 1; }
[ "${RO_TEST_MODE:-0}" = 1 ] ||
  die 'live mode prohibited until separately validated and released'

root="${RO_TEST_ROOT:-}"
case "$root" in
  /|''|*/../*|*/..|*//*) die 'unsafe synthetic root' ;;
  /tmp/ro-transaction-test-*/router|/tmp/ro-pilot-fixture.*/router) ;;
  *) die 'synthetic root must be a dedicated /tmp router fixture' ;;
esac
[ -d "$root" ] && [ ! -L "$root" ] ||
  die 'synthetic root missing or symlink'
[ -f "$root/.ro-pilot-synthetic-root" ] &&
[ ! -L "$root/.ro-pilot-synthetic-root" ] ||
  die 'synthetic-root marker missing'
state="$root/etc/router-orchestrator/upgrade"
[ -d "$state" ] && [ ! -L "$state" ] ||
  die 'no synthetic transaction state'

read_number() {
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  IFS= read -r number < "$1" || [ -n "${number:-}" ] || return 1
  case "$number" in ''|*[!0-9]*) return 1 ;; esac
  [ "${#number}" -le 12 ] || return 1
  printf '%s\n' "$number"
}

now_uptime() {
  if [ -n "${RO_TEST_NOW:-}" ]; then
    number="$RO_TEST_NOW"
    case "$number" in ''|*[!0-9]*) return 1 ;; esac
    [ "${#number}" -le 12 ] || return 1
    printf '%s\n' "$number"
  else
    [ -r /proc/uptime ] || return 1
    number="$(awk 'NR == 1 {print int($1)}' /proc/uptime)" || return 1
    case "$number" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "$number"
  fi
}

rollback_if_pending() {
  [ -f "$state/pending" ] || return 0
  [ -f "$state/transaction.sh" ] &&
  [ ! -L "$state/transaction.sh" ] ||
    die 'transaction script missing; cannot restore'
  # The transaction's own exclusive lock and sealed recovery snapshot still
  # decide whether recovery can proceed. Never bypass their checks.
  /bin/sh "$state/transaction.sh" rollback ||
    die 'rollback attempt failed; procd must retry secondary monitor'
  printf 'SECONDARY-GUARD: ROLLED BACK original client fixture\n'
}

[ -f "$state/armed" ] || [ -f "$state/pending" ] || exit 0
deadline="$(read_number "$state/deadline.uptime")" || {
  rollback_if_pending
  die 'missing/corrupt deadline'
}
# A supervised apply must not modify even one client file until both
# worker processes have reached their validated loops.
: > "$state/secondary-ready"

while [ -f "$state/armed" ] || [ -f "$state/pending" ]; do
  now="$(now_uptime)" || {
    rollback_if_pending
    die 'monotonic uptime unavailable'
  }
  if [ "$now" -ge "$deadline" ]; then
    if [ -f "$state/pending" ]; then
      rollback_if_pending
      exit 0
    fi
    # Apply has armed the monitors but not yet written pending. Remain
    # ready; apply will refuse any mutation if the deadline has expired.
  fi
  sleep 1
done
exit 0
