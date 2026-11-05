#!/bin/sh
# Real procd supervision probe for disposable /tmp marker-only processes.
# NEVER installs/updates Remote HUB, changes UCI/Xray, restarts existing
# services, or opens network ports. Explicit --run required for ubus mutations.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
fail() { printf 'PROCD-DUAL: FAIL - %s\n' "$*" >&2; exit 1; }
say() { printf 'PROCD-DUAL: %s\n' "$*"; }

mode="${1:-}"
case "$mode" in --run|--self-test) ;; *) fail 'usage: sh probe.sh --self-test | --run' ;; esac
tool_dir="$(cd -P "$(dirname "$0")" && pwd -P)" || fail 'invalid script directory'
worker="$tool_dir/openwrt_procd_dual_worker.sh"
[ -f "$worker" ] && [ ! -L "$worker" ] || fail 'paired worker missing'
sh -n "$worker" || fail 'worker syntax error'

for app in mktemp mkdir rm cat awk grep sed wc sleep sh jq; do
  command -v "$app" >/dev/null 2>&1 || fail "missing applet $app"
done
if [ "$mode" = --run ]; then
  for app in ubus dd hexdump; do
    command -v "$app" >/dev/null 2>&1 || fail "missing $app"
  done
  ubus list 2>/dev/null | grep -Fxq service ||
    fail 'procd ubus service object unavailable'
fi

dir="$(mktemp -d /tmp/ro-procd-dual.XXXXXX)" || fail 'cannot make /tmp fixture'
case "$dir" in /tmp/ro-procd-dual.*) ;; *) fail 'unsafe mktemp directory' ;; esac
[ -d "$dir" ] && [ ! -L "$dir" ] || fail 'unsafe fixture'
: > "$dir/.dual-probe-marker"
: > "$dir/pending"
: > "$dir/armed"
now="$(awk 'NR==1{print int($1)}' /proc/uptime)" || fail 'no monotonic uptime'
case "$now" in ''|*[!0-9]*) fail 'invalid monotonic uptime' ;; esac
if [ "$mode" = --self-test ]; then
  timeout_seconds=2
else
  timeout_seconds=8
fi
printf '%s\n' "$((now + timeout_seconds))" > "$dir/deadline"

name=''
testpid=''
cleanup() {
  if [ -n "$name" ]; then
    d="$(jq -nc --arg n "$name" '{name:$n}')" || d=''
    if [ -n "$d" ]; then
      ubus call service delete "$d" >/dev/null 2>&1 ||
        printf 'PROCD-DUAL: WARNING - service deletion not confirmed: %s\n' "$name" >&2
    fi
  fi
  if [ -n "$testpid" ]; then
    kill "$testpid" >/dev/null 2>&1 || true
  fi
  # Remove only the exact directory created by mktemp, after service delete.
  case "$dir" in
    /tmp/ro-procd-dual.*)
      if [ -d "$dir" ] && [ ! -L "$dir" ]; then
        rm -rf "$dir"
      fi ;;
  esac
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

if [ "$mode" = --self-test ]; then
  sh "$worker" secondary "$dir" >/dev/null 2>&1 &
  testpid="$!"
  ok=0
  i=0
  while [ "$i" -lt 7 ]; do
    if [ -e "$dir/synthetic-rollback" ] && [ ! -e "$dir/pending" ]; then
      ok=1
      break
    fi
    sleep 1
    i=$((i+1))
  done
  [ "$ok" -eq 1 ] || fail 'offline secondary did not clear fake pending'
  say 'PASS - offline marker-only secondary deadline execution'
  exit 0
fi

id="$(dd if=/dev/urandom bs=4 count=1 2>/dev/null |
  hexdump -v -e '4/1 "%02x"')" || fail 'random service id unavailable'
case "$id" in ''|*[!0-9a-f]*) fail 'unsafe service id' ;; esac
[ "${#id}" -eq 8 ] || fail 'random service id length incorrect'
name="ro-procd-dual-$id"
say "isolated procd service: $name"
say 'Worker and fake rollback marker are the ONLY test targets.'

# Safety-cleanup instance deletes just this randomly named service after 45s
# in case the initiating SSH connection drops before normal cleanup.
payload="$(jq -nc --arg n "$name" --arg w "$worker" --arg d "$dir" '{
  name:$n, instances:{
    primary:{command:["/bin/sh",$w,"primary",$d],respawn:[3600,1,0]},
    secondary:{command:["/bin/sh",$w,"secondary",$d],respawn:[3600,1,0]},
    safety_cleanup:{command:["/bin/sh",$w,"safety-cleanup",$d,$n]}
  }
}')" || fail 'cannot construct safe ubus payload'

ubus call service add "$payload" >/dev/null ||
  fail 'could not register isolated service'
say 'registered two guarded worker instances and cleanup timer'

i=0
while [ "$i" -lt 8 ]; do
  if [ -s "$dir/primary-starts" ] && [ -s "$dir/secondary-starts" ]; then break; fi
  sleep 1
  i=$((i+1))
done
[ -s "$dir/primary-starts" ] || fail 'primary never started'
[ -s "$dir/secondary-starts" ] || fail 'secondary never started'
old="$(sed -n '1p' "$dir/primary-starts")"
secondary="$(sed -n '1p' "$dir/secondary-starts")"
case "$old:$secondary" in *[!0-9:]*|'') fail 'bad worker PIDs' ;; esac
say "initial primary pid=$old; independent secondary pid=$secondary"

signal="$(jq -nc --arg n "$name" '{name:$n,instance:"primary",signal:9}')"
ubus call service signal "$signal" >/dev/null ||
  fail 'could not signal primary test instance'

i=0
while [ "$i" -lt 9 ]; do
  new="$(sed -n '2p' "$dir/primary-starts" 2>/dev/null || true)"
  [ -n "$new" ] && break
  sleep 1
  i=$((i+1))
done
[ -n "${new:-}" ] && [ "$old" != "$new" ] ||
  fail 'primary was not restarted by procd'
say "PASS - primary restarted: $old -> $new"

i=0
while [ "$i" -lt 13 ]; do
  if [ -e "$dir/synthetic-rollback" ] && [ ! -e "$dir/pending" ]; then break; fi
  sleep 1
  i=$((i+1))
done
[ -f "$dir/synthetic-rollback" ] || fail 'independent secondary deadline did not fire'
[ ! -e "$dir/pending" ] || fail 'synthetic pending was not cleared'
[ "$(wc -l < "$dir/secondary-starts")" -eq 1 ] ||
  fail 'secondary unexpectedly restarted'
say 'PASS - secondary independently expired original deadline and cleared fake pending'
say 'PASS - no Remote HUB, UCI, Xray, TCP/18443 or live configuration modified'
say 'deleting isolated procd service and temporary marker files'
