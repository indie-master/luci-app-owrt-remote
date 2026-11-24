#!/bin/sh
# Opt-in, single-device reboot trial for a confirmed on-disk agent update.
# Original agent snapshot is retained by router_client_transaction.sh.
# Does not touch Podkop, DNSMasq, LAN, WAN UCI or the server.
set -eu
umask 077
export PATH=/bin:/sbin:/usr/bin:/usr/sbin
S=/etc/router-orchestrator/upgrade
SELF="$S/client-boot-trial.sh"
TX="$S/client-transaction.sh"
INIT=/etc/init.d/ro-client-boot-trial
BOOT=/etc/rc.d/S09ro-client-boot-trial
UCI=/etc/config/owrtremote
XRAY=/etc/xray/owrt-remote-client.json
fail() { echo "BOOT-TRIAL: BLOCKED - $*" >&2; exit 1; }
say() { echo "BOOT-TRIAL: $*"; }
[ "$(id -u)" = 0 ] && [ -r /etc/openwrt_release ] || fail 'OpenWrt root required'
[ -d "$S" ] && [ ! -L "$S" ] && [ -f "$SELF" ] && [ ! -L "$SELF" ] || fail 'missing persistent runner'
[ ! -L "$S/boot-trial.lock" ] || fail 'unsafe lock'
exec 9>"$S/boot-trial.lock"
flock -x 9 || fail 'lock unavailable'
hash() { sha256sum "$1" | awk '{print $1}'; }
boot_id() { cat /proc/sys/kernel/random/boot_id; }
now() { awk 'NR==1 {print int($1)}' /proc/uptime; }
phase() { cat "$S/boot-trial.phase" 2>/dev/null || true; }
mark() { printf '%s\n' "$2" > "$S/$1.new"; mv -f "$S/$1.new" "$S/$1"; sync; }
number() { case "$1" in ''|*[!0-9]*) return 1;; esac; [ "${#1}" -le 12 ]; }
critical_backup() {
    for name in uci xray; do
        case "$name" in uci) dest="$UCI";; xray) dest="$XRAY";; esac
        original="$S/backup/files$dest"
        [ -f "$original" ] && [ ! -L "$original" ] &&
        [ ! -L "$dest" ] && { [ ! -e "$dest" ] || [ -f "$dest" ]; } &&
        [ "$(hash "$original")" = "$(cat "$S/stage/$name.sha256")" ] ||
            fail "invalid $name recovery copy"
    done
}
verify_installed() {
    [ "$(hash "$S/stage/candidate.tar")" = "$(cat "$S/stage/archive.sha256")" ] || fail 'candidate archive changed'
    manifest="$S/stage/unpacked/manifest.txt"
    [ "$(tar -xOf "$S/stage/candidate.tar" manifest.txt | sha256sum | awk '{print $1}')" = "$(hash "$manifest")" ] || fail 'manifest changed'
    [ "$(wc -l < "$manifest")" = 10 ] || fail 'manifest count'
    while read -r digest mode rel; do
        case "$rel" in usr/sbin/owrt-remote|usr/lib/owrt-remote/wan.sh|usr/lib/owrt-remote/logs.sh|etc/init.d/owrt-remote-logs|etc/init.d/owrt-remote|etc/hotplug.d/iface/99-owrt-remote|www/cgi-bin/owrt-remote|usr/share/luci/menu.d/luci-app-owrt-remote.json|usr/share/rpcd/acl.d/luci-app-owrt-remote.json|www/luci-static/resources/view/owrt_remote.js) ;; *) fail 'unexpected manifest path';; esac
        [ -f "/$rel" ] && [ ! -L "/$rel" ] && [ "$(hash "/$rel")" = "$digest" ] || fail "installed file changed: $rel"
    done < "$manifest"
}
# The next bundle may differ from the confirmed bundle ONLY in logs.sh.
# Retain the original stage and snapshot; do not change the live lock
# protocol until the early boot hook, before any agent writer starts.
verify_next() {
    next_sha="$1"
    case "$next_sha" in ''|*[!0-9a-f]*) fail 'invalid next digest';; esac
    [ "${#next_sha}" = 64 ] || fail 'invalid next digest length'
    next="$S/next-client.tar"
    [ -f "$next" ] && [ ! -L "$next" ] &&
    [ "$(hash "$next")" = "$next_sha" ] || fail 'next bundle digest mismatch'
    old_manifest="$S/stage/unpacked/manifest.txt"
    [ "$(tar -xOf "$S/stage/candidate.tar" manifest.txt | sha256sum | awk '{print $1}')" = "$(hash "$old_manifest")" ] &&
    [ "$(hash "$S/stage/candidate.tar")" = "$(cat "$S/stage/archive.sha256")" ] || fail 'old manifest changed'
    next_manifest="$(tar -xOf "$next" manifest.txt)" || fail 'next manifest missing'
    [ "$(printf '%s\n' "$next_manifest" | wc -l)" = 10 ] || fail 'next manifest count'
    [ "$(printf '%s\n' "$next_manifest" | awk '$3!="usr/lib/owrt-remote/logs.sh"')" = "$(awk '$3!="usr/lib/owrt-remote/logs.sh"' "$old_manifest")" ] || fail 'next bundle changes more than journal helper'
    next_log_hash="$(printf '%s\n' "$next_manifest" | awk '$2=="0644" && $3=="usr/lib/owrt-remote/logs.sh" {print $1}')"
    case "$next_log_hash" in ''|*[!0-9a-f]*) fail 'bad journal digest';; esac
    [ "${#next_log_hash}" = 64 ] &&
    [ "$(tar -xOf "$next" payload/usr/lib/owrt-remote/logs.sh | sha256sum | awk '{print $1}')" = "$next_log_hash" ] || fail 'journal payload mismatch'
}
switch_journal() (
    verify_installed
    verify_next "$(cat "$S/boot-trial.next.sha")"
    dst=/usr/lib/owrt-remote/logs.sh
    tmp="$(mktemp "$(dirname "$dst")/.ro-journal.XXXXXX")" || exit 1
    trap 'rm -f "$tmp"' EXIT
    tar -xOf "$next" payload/usr/lib/owrt-remote/logs.sh >"$tmp" &&
    [ "$(hash "$tmp")" = "$next_log_hash" ] &&
    sh -n "$tmp" && chmod 0644 "$tmp" && mv -f "$tmp" "$dst" || exit 1
    sync
)
verify_trial_installed() {
    if [ ! -f "$S/boot-trial.next.sha" ]; then verify_installed; return; fi
    verify_next "$(cat "$S/boot-trial.next.sha")"
    while read -r digest mode rel; do
        [ -f "/$rel" ] && [ ! -L "/$rel" ] &&
        [ "$(hash "/$rel")" = "$digest" ] || fail "trial installed digest: $rel"
    done <<EOF
$next_manifest
EOF
}
verify_old_files() {
    [ "$(wc -l < "$S/backup/present.txt")" = 10 ] &&
    [ "$(wc -l < "$S/backup/original.sha256")" = 10 ] || fail 'invalid original inventory'
    while read -r digest mode rel; do
        presence="$(awk -v p="$rel" '$2==p {print $1}' "$S/backup/present.txt")"
        old_hash="$(awk -v p="$rel" '$2==p {print $1}' "$S/backup/original.sha256")"
        old="$S/backup/files/$rel"
        case "$presence" in
            1) [ -f "$old" ] && [ ! -L "$old" ] &&
               [ "$(hash "$old")" = "$old_hash" ] || fail "original backup invalid: $rel";;
            0) [ "$old_hash" = - ] && [ ! -e "$old" ] && [ ! -L "$old" ] || fail "invalid absent record: $rel";;
            *) fail "invalid original record: $rel";;
        esac
    done < "$S/stage/unpacked/manifest.txt"
}
restore_critical() {
    # Arm explicitly reserves owrtremote UCI and its own Xray JSON for recovery.
    # No other configuration is restored, and no UCI edit is allowed during trial.
    critical_backup
    for dest in "$UCI" "$XRAY"; do
        tmp="$(mktemp "$(dirname "$dest")/.ro-boot-restore.XXXXXX")"
        cp -p "$S/backup/files$dest" "$tmp" && mv -f "$tmp" "$dest" || { rm -f "$tmp"; fail 'critical restore failed'; }
    done
    # Clear only this package's volatile UCI delta, if any.
    if [ -n "$(uci -q changes owrtremote)" ]; then uci revert owrtremote; fi
    sync
}
restore_trial() {
    mark boot-trial.phase RESTORING
    restore_critical
    # Existing runner restores only its sealed ten-file snapshot, including
    # originally absent files. Its own lock and pending recovery are retained.
    mark client.pending yes
    sh "$TX" boot-recover
    [ "$(cat "$S/client.result")" = ROLLED_BACK ] && [ ! -e "$S/client.pending" ] || fail 'client restore not confirmed'
    mark boot-trial.phase ROLLED_BACK
    say 'original agent, owrtremote UCI and reverse Xray JSON restored'
}
valid_window() {
    [ "$(phase)" = TRIAL ] || return 1
    [ "$(cat "$S/boot-trial.boot")" = "$(boot_id)" ] || return 1
    deadline="$(cat "$S/boot-trial.deadline")"; current="$(now)"
    number "$deadline" && number "$current" && [ "$current" -lt "$deadline" ]
}
workers_ready() {
    a="$(cat "$S/boot-trial.primary.pid" 2>/dev/null || true)"
    b="$(cat "$S/boot-trial.secondary.pid" 2>/dev/null || true)"
    number "$a" && number "$b" && [ "$a" != "$b" ] || return 1
    ubus -t 3 call service list '{"name":"ro-client-boot-trial"}' |
      jq -e --arg a "$a" --arg b "$b" '
       .["ro-client-boot-trial"].instances as $i |
       $i.primary.running == true and $i.secondary.running == true
       and ($i.primary.pid|tostring)==$a and ($i.secondary.pid|tostring)==$b
      ' >/dev/null
}
runtime_ok() {
    ubus -t 3 call service list '{"name":"owrt-remote"}' |
      jq -e '.["owrt-remote"].instances | .xray.running == true and .heartbeat.running == true' >/dev/null || return 1
    ubus -t 3 call service list '{"name":"owrt-remote-logs"}' |
      jq -e '.["owrt-remote-logs"].instances | .journal.running == true and .archive.running == true' >/dev/null || return 1
    # The procd xray instance must actually have exec'd Xray, not a retry shell.
    pid="$(ubus -t 3 call service list '{"name":"owrt-remote"}' | jq -er '.["owrt-remote"].instances.xray.pid')"
    number "$pid" && [ -r "/proc/$pid/cmdline" ] || return 1
    tr '\000' '\n' <"/proc/$pid/cmdline" | grep -Fxq -e "$XRAY" -e "-config=$XRAY" -e "--config=$XRAY" -e "-c=$XRAY" || return 1
    case "$(readlink "/proc/$pid/exe")" in */xray|*/xray-core|*/xray_softfloat) ;; *) return 1;; esac
    # Match both reverse links against the known-good transport, credential,
    # reverse identity and routing targets; resolved WAN IP may change.
    jq -e -s '
      def reverse: [.outbounds[] | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")]|sort_by(.tag);
      def identity: {tag,protocol,settings:(.settings|{port,id,reverse}),stream:(.streamSettings|del(.sockopt))};
      (.[0]|reverse) as $old | (.[1]|reverse) as $new |
      ($old|length)==2 and ($new|length)==2 and
      ([$old[]|identity] == [$new[]|identity]) and
      (.[0].routing.rules == .[1].routing.rules) and
      ([.[0].outbounds[]|select(.tag=="router-admin" or .tag=="router-ssh")|.settings.redirect] ==
       [.[1].outbounds[]|select(.tag=="router-admin" or .tag=="router-ssh")|.settings.redirect])
    ' "$S/backup/files$XRAY" "$XRAY" >/dev/null
}
request_reboot() { /sbin/reboot; }
case "${1:-}" in
arm)
    [ "$#" = 3 ] || [ "$#" = 4 ] || fail 'arm ROUTER_ID BUNDLE_SHA256 [NEXT_SHA256]'
    [ -z "$(phase)" ] && [ ! -e "$INIT" ] && [ ! -L "$INIT" ] && [ ! -e "$BOOT" ] && [ ! -L "$BOOT" ] || fail 'trial already exists'
    [ -f "$TX" ] && [ ! -L "$TX" ] && sh -n "$TX" || fail 'disk recovery runner unavailable'
    [ "$(cat "$S/client.result")" = CONFIRMED_ON_DISK ] && [ ! -e "$S/client.pending" ] && [ ! -e "$S/client.armed" ] || fail 'client transaction not confirmed'
    [ "$(uci -q get owrtremote.main.router_id)" = "$2" ] && [ "$(cat "$S/stage/archive.sha256")" = "$3" ] || fail 'target or digest mismatch'
    [ "$(cat "$S/client.boot")" = "$(boot_id)" ] || fail 'client already rebooted'
    [ -z "$(uci -q changes owrtremote)" ] || fail 'uncommitted UCI changes'
    critical_backup; verify_installed; verify_old_files
    if [ "$#" = 4 ]; then verify_next "$4"; fi
    [ "$(hash "$UCI")" = "$(cat "$S/stage/uci.sha256")" ] && [ "$(hash "$XRAY")" = "$(cat "$S/stage/xray.sha256")" ] || fail 'critical configuration changed since staging'
    [ -x /etc/init.d/owrt-remote ] && /etc/init.d/owrt-remote enabled || fail 'agent not enabled at boot'
    # Require a persistent executable or a verified compressed boot copy
    # of the configured executable; no network download is assumed.
    bin="$(uci -q get owrtremote.main.xray_bin || true)"
    [ -n "$bin" ] || bin=/usr/bin/xray
    [ -x "$bin" ] || fail 'configured Xray binary absent'
    resolved="$(readlink -f "$bin")"
    case "$resolved" in /tmp/*|/var/*)
        digest="$(hash "$resolved")"; found=0
        for cache in /overlay/owrt-remote/xray-cache/*."$digest".gz /etc/owrt-remote/xray-cache/*."$digest".gz; do
            [ -f "$cache" ] && [ ! -L "$cache" ] || continue
            gzip -t "$cache" && [ "$(gzip -dc "$cache" | sha256sum | awk '{print $1}')" = "$digest" ] && found=1
        done
        [ "$found" = 1 ] || fail 'RAM Xray has no verified persistent cache; prepare boot copy first'
        ;;
    esac
    tmp="$(mktemp /etc/init.d/.ro-boot-trial.XXXXXX)"
    cat >"$tmp" <<'EOF'
#!/bin/sh /etc/rc.common
USE_PROCD=1
START=09
STOP=99
boot() {
    local runner=/etc/router-orchestrator/upgrade/client-boot-trial.sh
    /bin/sh "$runner" boot-enter || return 1
    [ "$(cat /etc/router-orchestrator/upgrade/boot-trial.phase 2>/dev/null)" = TRIAL ] || return 0
    start
    for i in 1 2 3 4 5 6 7 8 9 10; do
        /bin/sh "$runner" ready && return 0
        sleep 1
    done
    /bin/sh "$runner" recover-before-start
}
start_service() {
    [ "$(cat /etc/router-orchestrator/upgrade/boot-trial.phase)" = TRIAL ] || return 0
    for role in primary secondary; do
        procd_open_instance "$role"
        procd_set_param command /bin/sh /etc/router-orchestrator/upgrade/client-boot-trial.sh worker "$role"
        procd_set_param respawn 3600 2 0
        procd_set_param stdout 1
        procd_set_param stderr 1
        procd_close_instance
    done
}
EOF
    chmod 700 "$tmp"; mv "$tmp" "$INIT"; "$INIT" enable
    [ -L "$BOOT" ] || fail 'boot hook not enabled'
    if [ "$#" = 4 ]; then mark boot-trial.next.sha "$4"; fi
    mark boot-trial.before "$(boot_id)"
    mark boot-trial.phase ARMED
    say 'ARMED: reboot deliberately to start 300-second trial'
    ;;
boot-enter)
    case "$(phase)" in
        ARMED)
            [ "$(cat "$S/boot-trial.before")" != "$(boot_id)" ] || fail 'not a new boot'
            if [ -f "$S/boot-trial.next.sha" ]; then
                mark boot-trial.phase SWITCHING
                if ! switch_journal; then restore_trial; return_code=1; exit "$return_code"; fi
            fi
            mark boot-trial.boot "$(boot_id)"
            mark boot-trial.deadline "$(($(now) + 300))"
            mark boot-trial.phase TRIAL
            ;;
        SWITCHING|TRIAL|RESTORING) restore_trial;;
    esac
    ;;
ready)
    workers_ready
    ;;
recover-before-start)
    [ "$(phase)" = TRIAL ] || fail 'not in trial'
    restore_trial
    ;;
worker)
    case "${2:-}" in primary|secondary) role="$2";; *) fail 'worker role';; esac
    printf '%s\n' "$$" >"$S/boot-trial.$role.pid"
    while :; do
        if [ "$(phase)" = TRIAL ] && ! valid_window || [ "$(phase)" = RESTORING ]; then
            # Restore only on the next early boot, before any journal writer
            # starts. Never switch lock protocols underneath live processes.
            mark boot-trial.phase RESTORING
            request_reboot
        fi
        flock -u 9
        sleep 2
        flock -x 9
    done
    ;;
confirm)
    valid_window && workers_ready || fail 'trial expired or monitors unavailable'
    verify_trial_installed
    [ "$(hash "$UCI")" = "$(cat "$S/stage/uci.sha256")" ] || fail 'UCI changed; manual review before acceptance'
    runtime_ok || fail 'runtime or reverse parity failed'
    valid_window || fail 'deadline elapsed during verification'
    if [ -f "$S/boot-trial.next.sha" ]; then
        mark boot-trial.accepted.sha "$(cat "$S/boot-trial.next.sha")"
    else
        mark boot-trial.accepted.sha "$(cat "$S/stage/archive.sha256")"
    fi
    mark boot-trial.phase ACCEPTED
    say 'ACCEPTED: new boot and agent processes verified; caller must verify reverse access independently'
    ;;
cancel)
    [ "$(phase)" = ARMED ] && [ "$(cat "$S/boot-trial.before")" = "$(boot_id)" ] || fail 'only an unstarted trial may be cancelled'
    "$INIT" disable
    [ ! -e "$BOOT" ] && [ ! -L "$BOOT" ] || fail 'boot hook still enabled'
    rm -f "$INIT" "$S/boot-trial.phase" "$S/boot-trial.before" "$S/boot-trial.next.sha"
    sync
    say 'unstarted trial cancelled; confirmed disk update retained'
    ;;
status)
    say "PHASE=$(phase)"
    if workers_ready; then say 'WATCHDOGS: READY'; fi
    if runtime_ok; then say 'RUNTIME: PASS'; fi
    ;;
*) fail 'arm | boot-enter | worker | ready | recover-before-start | confirm | cancel | status';;
esac
