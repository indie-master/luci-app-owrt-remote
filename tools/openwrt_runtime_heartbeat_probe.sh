#!/bin/sh
# Test the *real* OpenWrt procd instance-scoped SIGTERM with two fake workers.
# All registered instances are under a random ro-runtime-test-XXXXXXXX
# service. This NEVER signals the actual owrt-remote or Xray processes.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
die() { printf 'RUNTIME-PROCD: FAIL - %s\n' "$*" >&2; exit 1; }
say() { printf 'RUNTIME-PROCD: %s\n' "$*"; }

[ "${1:-}" = --run ] && [ "$#" -eq 2 ] ||
    die 'usage: --run /path/to/router_heartbeat_activation.sh'
activation="$2"
[ -f "$activation" ] && [ ! -L "$activation" ] ||
    die 'activation script missing'
sh -n "$activation" || die 'activation shell syntax invalid'
for cmd in ubus jq mktemp dd hexdump awk sha256sum sleep; do
    command -v "$cmd" >/dev/null 2>&1 || die "missing $cmd"
done
ubus list | grep -Fxq service ||
    die 'actual OpenWrt procd service API unavailable'
# Never delete someone else's test service, even if its name resembles ours.
leftovers="$(ubus call service list '{}' | jq -r 'keys[]' |
    grep '^ro-runtime-test-' || true)"
[ -z "$leftovers" ] ||
    die "previous synthetic runtime service still registered: $leftovers"

top=''
name=''
registered=0
cleanup() {
    rc="$?"
    trap - EXIT HUP INT TERM
    set +e
    keep=0
    if [ "$registered" = 1 ] && [ -n "$name" ]; then
        delete="$(jq -nc --arg n "$name" '{name:$n}')" || keep=1
        [ "$keep" = 1 ] ||
            ubus call service delete "$delete" >/dev/null 2>&1 || keep=1
        n=0
        while [ "$keep" = 0 ] && [ "$n" -lt 25 ]; do
            status="$(ubus call service list \
                "$(jq -nc --arg n "$name" '{name:$n,verbose:true}')" 2>/dev/null)" || {
                    keep=1
                    break
                }
            if printf '%s\n' "$status" |
                jq -e --arg n "$name" 'type=="object" and (has($n)|not)' >/dev/null 2>&1; then
                break
            fi
            sleep 1
            n=$((n+1))
        done
        if [ "$keep" = 0 ] && [ "$n" -ge 25 ]; then keep=1; fi
    fi
    if [ "$keep" = 0 ] && [ -n "$top" ]; then
        case "$top" in /tmp/ro-runtime-test-*)
            [ -d "$top" ] && [ ! -L "$top" ] && rm -rf "$top" ;;
        esac
    elif [ -n "$top" ]; then
        say "WARNING - temporary service removal unconfirmed; keep fixture: $top"
        rc=1
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

top="$(mktemp -d /tmp/ro-runtime-test-XXXXXXXX)" ||
    die 'mktemp failed'
case "$top" in /tmp/ro-runtime-test-*) ;; *) die 'bad fixture location' ;; esac
[ "$(cd -P "$top" && pwd -P)" = "$top" ] || die 'fixture not canonical'
root="$top/router"
state="$root/etc/router-orchestrator/upgrade"
mkdir -p "$state"
: > "$root/.ro-runtime-synthetic-root"

nonce="$(dd if=/dev/urandom bs=4 count=1 2>/dev/null |
    hexdump -v -e '4/1 "%02x"')" || die 'random nonce unavailable'
case "$nonce" in ''|*[!0-9a-f]*) die 'random nonce not hex' ;; esac
[ "${#nonce}" -eq 8 ] || die 'bad nonce length'
name="ro-runtime-test-$nonce"

cat > "$top/worker.sh" <<'EOF'
#!/bin/sh
case "$1" in
    xray|heartbeat) exec sleep 180 ;;
    safety)
        sleep 110
        ubus call service delete "{\"name\":\"$2\"}" >/dev/null 2>&1 || true ;;
    *) exit 2 ;;
esac
EOF
sh -n "$top/worker.sh" || die 'fake worker script invalid'
payload="$(jq -nc --arg n "$name" --arg w "$top/worker.sh" '{
  name:$n,
  instances:{
    xray:{command:["/bin/sh",$w,"xray"],respawn:[3600,2,0]},
    heartbeat:{command:["/bin/sh",$w,"heartbeat"],respawn:[3600,2,0]},
    safety:{command:["/bin/sh",$w,"safety",$n]}
  }
}')" || die 'cannot assemble procd service registration'

# After this request there may be real procd workers. Always try safe cleanup.
registered=1
ubus call service add "$payload" >/dev/null ||
    die 'procd registration rejected; cleanup still requested for exact name'
export RO_TEST_MODE=1
export RO_TEST_ROOT="$root"
export RO_TEST_SERVICE_NAME="$name"

# Wait for both service instances to become independently visible.
i=0
ready=''
while [ "$i" -lt 12 ]; do
    read_status="$(ubus call service list \
        "$(jq -nc --arg n "$name" '{name:$n,verbose:true}')")" ||
        die 'cannot read temporary procd service'
    if printf '%s\n' "$read_status" | jq -e --arg n "$name" '
        .[$n].instances.xray.running == true and
        .[$n].instances.heartbeat.running == true and
        (.[$n].instances.xray.pid|type=="number") and
        (.[$n].instances.heartbeat.pid|type=="number")
    ' >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
    i=$((i+1))
done
[ "$ready" = 1 ] || die 'temporary procd instances were not both ready'
say "PASS - two fake procd instances registered: $name"

pid() {
    ubus call service list \
        "$(jq -nc --arg n "$name" '{name:$n,verbose:true}')" |
    jq -er --arg n "$name" --arg i "$1" \
        '.[$n].instances[$i] | select(.running==true) | .pid | select(type=="number") | tostring'
}
xray_before="$(pid xray)" || die 'fake xray PID missing'
heartbeat_before="$(pid heartbeat)" || die 'fake heartbeat PID missing'

sh "$activation" --inspect ||
    die 'read-only fake instance inspection failed'

: > "$state/pending"
sh "$activation" --activate ||
    die 'heartbeat-only activation failed or fake Xray PID changed'
xray_after="$(pid xray)" || die 'fake xray unavailable after activation'
heartbeat_after="$(pid heartbeat)" || die 'fake heartbeat unavailable after activation'
[ "$xray_before" = "$xray_after" ] ||
    die 'fake xray PID changed after activation'
[ "$heartbeat_before" != "$heartbeat_after" ] ||
    die 'fake heartbeat PID failed to change after activation'
say 'PASS - targeted fake heartbeat activation, fake Xray PID unchanged'

rm -f "$state/pending"
: > "$state/rolled-back"
sh "$activation" --restore ||
    die 'heartbeat-only rollback activation failed'
xray_restored="$(pid xray)" || die 'fake xray unavailable after rollback'
heartbeat_restored="$(pid heartbeat)" || die 'fake heartbeat unavailable after rollback'
[ "$xray_before" = "$xray_restored" ] ||
    die 'fake xray PID changed after rollback'
[ "$heartbeat_restored" != "$heartbeat_after" ] ||
    die 'fake heartbeat PID failed to change after rollback'
say 'PASS - targeted fake heartbeat rollback activation, fake Xray PID unchanged'
say 'PASS - isolated real-procd heartbeat lifecycle; no working instances signaled'
