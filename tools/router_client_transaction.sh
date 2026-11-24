#!/bin/sh
# Single-router on-disk client transaction. No reload/restart of existing services.
# Persistent boot recovery + two independently supervised deadline monitors.
set -eu
umask 077
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
fail() { echo "CLIENT-TXN: BLOCKED - $*" >&2; exit 1; }
say() { echo "CLIENT-TXN: $*"; }
[ "$(id -u)" = 0 ] || fail 'root required'
[ -r /etc/openwrt_release ] || fail 'OpenWrt required'
S=/etc/router-orchestrator/upgrade
SELF="$S/client-transaction.sh"
INIT=/etc/init.d/ro-client-recovery
BOOT=/etc/rc.d/S08ro-client-recovery
UCI=/etc/config/owrtremote
XRAY=/etc/xray/owrt-remote-client.json
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
hash() { sha256sum "$1" | awk '{print $1}'; }
hex() { case "$1" in ''|*[!0-9a-f]*) return 1;; esac; [ "${#1}" = 64 ]; }
regular() { [ -f "$1" ] && [ ! -L "$1" ]; }
safe_path() {
    remain="${1#/}"; prefix=''
    while [ -n "$remain" ]; do
        part="${remain%%/*}"
        case "$part" in ''|.|..) fail 'unsafe path';; esac
        prefix="$prefix/$part"
        [ ! -L "$prefix" ] || fail "symlink path: $prefix"
        case "$remain" in */*) remain="${remain#*/}";; *) remain='';; esac
    done
}
for p in "$S" "$SELF" "$UCI" "$XRAY" "$INIT"; do safe_path "$p"; done
[ -d "$S" ] || fail 'prepared state missing'
command -v flock >/dev/null 2>&1 || fail 'kernel flock utility required'
safe_path "$S/client.lock"
exec 9>"$S/client.lock"
flock -x 9 || fail 'cannot lock transaction'
now() { awk 'NR==1 {print int($1)}' /proc/uptime; }
boot_id() { cat /proc/sys/kernel/random/boot_id; }
number() { case "$1" in ''|*[!0-9]*) return 1;; esac; [ "${#1}" -le 12 ]; }
mark() {
    printf '%s\n' "$2" > "$S/$1.new"
    mv -f "$S/$1.new" "$S/$1"
    sync
}
critical() {
    [ -z "$(uci -q changes owrtremote)" ] || fail 'uncommitted UCI changes'
    [ "$(hash "$UCI")" = "$(cat "$S/stage/uci.sha256")" ] || fail 'UCI changed'
    [ "$(hash "$XRAY")" = "$(cat "$S/stage/xray.sha256")" ] || fail 'Xray config changed'
}
xray_identity() {
    for pid in $(pidof xray 2>/dev/null || true); do
        [ -r "/proc/$pid/cmdline" ] || continue
        if tr '\000' '\n' < "/proc/$pid/cmdline" | grep -Fxq -e "$XRAY" -e "-config=$XRAY" -e "--config=$XRAY" -e "-c=$XRAY"; then
            printf '%s:' "$pid"
            awk '{print $22}' "/proc/$pid/stat"
        fi
    done
}
verify_backup() {
    safe_path "$S/backup/present.txt"
    safe_path "$S/backup/original.sha256"
    [ "$(wc -l < "$S/backup/present.txt")" = 10 ] || fail 'bad backup inventory'
    [ "$(wc -l < "$S/backup/original.sha256")" = 10 ] || fail 'bad backup hashes'
    for rel in $FILES; do
        safe_path "/$rel"
        safe_path "$S/backup/files/$rel"
        presence="$(awk -v p="$rel" '$2==p {print $1}' "$S/backup/present.txt")"
        digest="$(awk -v p="$rel" '$2==p {print $1}' "$S/backup/original.sha256")"
        case "$presence" in
            1) hex "$digest" && regular "$S/backup/files/$rel" &&
               [ "$(hash "$S/backup/files/$rel")" = "$digest" ] || fail "bad backup: $rel";;
            0) [ "$digest" = - ] && [ ! -e "$S/backup/files/$rel" ] || fail "bad absent record: $rel";;
            *) fail "bad backup record: $rel";;
        esac
    done
}
verify_candidate() {
    expected="$1"; hex "$expected" || fail 'invalid pinned bundle SHA256'
    safe_path "$S/stage/candidate.tar"
    safe_path "$S/stage/unpacked/manifest.txt"
    [ "$(cat "$S/stage/archive.sha256")" = "$expected" ] &&
    [ "$(hash "$S/stage/candidate.tar")" = "$expected" ] || fail 'bundle digest mismatch'
    manifest="$S/stage/unpacked/manifest.txt"
    [ "$(wc -l < "$manifest")" = 10 ] || fail 'bad manifest length'
    [ "$(tar -xOf "$S/stage/candidate.tar" manifest.txt | sha256sum | awk '{print $1}')" = "$(hash "$manifest")" ] || fail 'manifest differs from pinned archive'
    for rel in $FILES; do
        safe_path "$S/stage/unpacked/payload/$rel"
        src="$S/stage/unpacked/payload/$rel"
        regular "$src" || fail "missing candidate: $rel"
        digest="$(awk -v p="$rel" '$3==p {print $1}' "$manifest")"
        mode="$(awk -v p="$rel" '$3==p {print $2}' "$manifest")"
        hex "$digest" && [ "$(hash "$src")" = "$digest" ] || fail "candidate digest: $rel"
        case "$mode" in 0644|0755);; *) fail 'invalid candidate mode';; esac
        case "$rel" in *.sh|usr/sbin/owrt-remote|etc/init.d/*|etc/hotplug.d/*|www/cgi-bin/*) sh -n "$src" || fail "syntax: $rel";; esac
    done
}
verify_originals() {
    for rel in $FILES; do
        if [ "$(awk -v p="$rel" '$2==p {print $1}' "$S/backup/present.txt")" = 1 ]; then
            regular "/$rel" && cmp -s "/$rel" "$S/backup/files/$rel" || fail "original changed: $rel"
        else
            [ ! -e "/$rel" ] && [ ! -L "/$rel" ] || fail "original appeared: $rel"
        fi
    done
}
replace_file() {
    src="$1"; dst="$2"; mode="$3"
    mkdir -p "$(dirname "$dst")"
    tmp="$(mktemp "$(dirname "$dst")/.ro-client.XXXXXX")"
    cp -p "$src" "$tmp" || { rm -f "$tmp"; return 1; }
    if [ "$mode" != preserve ]; then chmod "$mode" "$tmp" || { rm -f "$tmp"; return 1; }; fi
    mv -f "$tmp" "$dst" || { rm -f "$tmp"; return 1; }
}
restore() {
    [ -f "$S/client.pending" ] || return 0
    verify_backup
    for rel in $FILES; do
        if [ "$(awk -v p="$rel" '$2==p {print $1}' "$S/backup/present.txt")" = 1 ]; then
            replace_file "$S/backup/files/$rel" "/$rel" preserve || fail "restore: $rel"
        else
            rm -f "/$rel" || fail "remove: $rel"
        fi
    done
    verify_originals
    sync
    mark client.result ROLLED_BACK
    rm -f "$S/client.pending" "$S/client.armed"
    sync
    say 'ROLLED BACK: original client files restored; no service restart'
}
ready() {
    for worker in primary secondary; do
        pid="$(cat "$S/client.$worker.ready" 2>/dev/null || true)"
        number "$pid" && kill -0 "$pid" 2>/dev/null || return 1
        tr '\000' '\n' < "/proc/$pid/cmdline" | grep -Fxq "$SELF" || return 1
        tr '\000' '\n' < "/proc/$pid/cmdline" | grep -Fxq "$worker" || return 1
    done
    [ "$(cat "$S/client.primary.ready")" != "$(cat "$S/client.secondary.ready")" ]
}
supervised() {
    a="$(cat "$S/client.primary.ready" 2>/dev/null || true)"
    b="$(cat "$S/client.secondary.ready" 2>/dev/null || true)"
    number "$a" && number "$b" || return 1
    ubus -t 3 call service list '{"name":"ro-client-recovery"}' |
        jq -e --arg a "$a" --arg b "$b" '
          .["ro-client-recovery"].instances as $i
          | $i.primary.running == true and $i.secondary.running == true
          and ($i.primary.pid|tostring) == $a
          and ($i.secondary.pid|tostring) == $b
        ' >/dev/null
}
valid_window() {
    [ "$(cat "$S/client.boot")" = "$(boot_id)" ] || return 1
    deadline="$(cat "$S/client.deadline")"; current="$(now)"
    number "$deadline" && number "$current" && [ "$current" -lt "$deadline" ]
}
install_recovery() {
    [ ! -e "$INIT" ] && [ ! -L "$INIT" ] && [ ! -e "$BOOT" ] && [ ! -L "$BOOT" ] || fail 'recovery hook already exists'
    regular "$0" || fail 'script must be a regular file'
    [ "$0" = "$SELF" ] || fail 'install script in persistent state before arm'
    tmp="$(mktemp /etc/init.d/.ro-client-recovery.XXXXXX)"
    cat > "$tmp" <<'EOF'
#!/bin/sh /etc/rc.common
USE_PROCD=1
START=08
STOP=99
boot() {
    /bin/sh /etc/router-orchestrator/upgrade/client-transaction.sh boot-recover
}
start_service() {
    for role in primary secondary; do
        procd_open_instance "$role"
        procd_set_param command /bin/sh /etc/router-orchestrator/upgrade/client-transaction.sh worker "$role"
        procd_set_param respawn 3600 2 0
        procd_set_param stdout 1
        procd_set_param stderr 1
        procd_close_instance
    done
}
EOF
    chmod 700 "$tmp"
    mv "$tmp" "$INIT"
    "$INIT" enable
    [ -L "$BOOT" ] || fail 'early boot hook not enabled'
    sync
}
case "${1:-}" in
arm)
    [ "$#" = 4 ] || fail 'usage: arm ROUTER_ID SHA256 TIMEOUT'
    [ ! -e "$S/client.pending" ] && [ ! -e "$S/client.armed" ] && [ ! -e "$S/client.result" ] || fail 'existing transaction; reset rolled-back transaction first'
    [ "$(uci -q get owrtremote.main.router_id)" = "$2" ] || fail 'wrong router'
    transport="$(uci -q get owrtremote.main.vps_transport || true)"
    transport="${transport:-tcp}"
    [ "$(cat "$S/stage/target")" = "$(printf '%s\n' "$2" "$transport" "$(uci -q get owrtremote.main.vps_port)")" ] || fail 'target changed'
    number "$4" && [ "$4" -ge 180 ] && [ "$4" -le 900 ] || fail 'timeout 180..900 required'
    verify_backup; verify_candidate "$3"; verify_originals; critical
    identity="$(xray_identity)"; [ -n "$identity" ] || fail 'reverse Xray not found'
    install_recovery
    mark client.sha "$3"
    mark client.xray "$identity"
    mark client.boot "$(boot_id)"
    mark client.nonce "$(cat /proc/sys/kernel/random/uuid)"
    mark client.deadline "$(($(now) + $4))"
    mark client.armed yes
    say 'ARMED: start recovery service, verify both workers, then apply'
    ;;
apply)
    [ "$#" = 2 ] && [ -f "$S/client.armed" ] && [ ! -e "$S/client.pending" ] || fail 'not armed'
    [ "$2" = "$(cat "$S/client.sha")" ] || fail 'wrong digest'
    [ -L "$BOOT" ] && ready && supervised && valid_window || fail 'recovery not ready or deadline expired'
    verify_backup; verify_candidate "$2"; verify_originals; critical
    [ "$(xray_identity)" = "$(cat "$S/client.xray")" ] || fail 'Xray identity changed'
    # All recovery metadata and pending marker reach disk before first replacement.
    mark client.pending yes
    for rel in $FILES; do
        mode="$(awk -v p="$rel" '$3==p {print $2}' "$S/stage/unpacked/manifest.txt")"
        replace_file "$S/stage/unpacked/payload/$rel" "/$rel" "$mode" || { restore; fail "apply: $rel"; }
    done
    sync
    # Critical failures leave a durable pending marker for the independent monitors.
    critical
    [ "$(xray_identity)" = "$(cat "$S/client.xray")" ] || { restore; fail 'Xray changed during apply'; }
    valid_window || { restore; fail 'deadline elapsed'; }
    say 'APPLIED ON DISK; no existing service restarted'
    say "CONFIRM NONCE: $(cat "$S/client.nonce")"
    ;;
worker)
    [ "$#" = 2 ] || fail 'worker role required'
    case "$2" in primary|secondary);; *) fail 'unknown worker';; esac
    role="$2"
    # Release the kernel lock between probes so apply/confirm/rollback can run.
    printf '%s\n' "$$" > "$S/client.$role.ready"
    flock -u 9
    while :; do
        flock -x 9
        if [ -f "$S/client.pending" ] && ! valid_window; then restore; exit 0; fi
        if [ ! -f "$S/client.armed" ] && [ ! -f "$S/client.pending" ]; then exit 0; fi
        flock -u 9
        sleep 2
    done
    ;;
confirm)
    [ "$#" = 2 ] && [ -f "$S/client.pending" ] || fail 'nothing to confirm'
    [ "$2" = "$(cat "$S/client.nonce")" ] || fail 'wrong nonce'
    valid_window || { restore; fail 'deadline expired'; }
    critical; verify_candidate "$(cat "$S/client.sha")"
    [ "$(xray_identity)" = "$(cat "$S/client.xray")" ] || fail 'Xray changed'
    for rel in $FILES; do
        [ "$(hash "/$rel")" = "$(hash "$S/stage/unpacked/payload/$rel")" ] || fail "installed digest: $rel"
    done
    # Pending wins on boot even if interrupted after writing the result.
    mark client.result CONFIRMED_ON_DISK
    rm -f "$S/client.pending"
    sync
    rm -f "$S/client.armed"
    sync
    say 'CONFIRMED ON DISK; runtime activation is separate'
    ;;
rollback|boot-recover)
    restore
    ;;
reset-rolled-back)
    [ ! -e "$S/client.pending" ] && [ ! -e "$S/client.armed" ] || fail 'transaction still active'
    [ "$(cat "$S/client.result" 2>/dev/null || true)" = ROLLED_BACK ] || fail 'only rolled-back transactions may be reset'
    if ready; then fail 'stop recovery service before reset'; fi
    verify_backup; verify_originals; critical
    [ -L "$BOOT" ] && regular "$INIT" || fail 'recovery hook missing'
    "$INIT" disable
    [ ! -e "$BOOT" ] && [ ! -L "$BOOT" ] || fail 'boot hook still present'
    rm -f "$INIT" "$S/client.result" "$S/client.sha" "$S/client.xray" "$S/client.boot" "$S/client.nonce" "$S/client.deadline" "$S/client.primary.ready" "$S/client.secondary.ready"
    sync
    say 'RESET: original staged bundle and recovery snapshot retained'
    ;;
status)
    if [ -f "$S/client.pending" ]; then say PENDING
    elif [ -f "$S/client.result" ]; then say "$(cat "$S/client.result")"
    elif [ -f "$S/client.armed" ]; then say ARMED
    else say PREPARED; fi
    if ready; then say 'TWO WORKERS READY'; fi
    if supervised; then say 'PROCD SUPERVISION: PASS'; fi
    ;;
*) fail 'arm | apply | confirm | rollback | reset-rolled-back | status | boot-recover | worker';;
esac
