#!/bin/sh
# BusyBox/OpenWrt-safe, read-only bundle verifier and persistent snapshot preparer.
# Deliberately NO apply/start/stop/reboot operations in this first implementation.
# The verified bundle is data, never executed. Preparatory artifacts live on overlay.
set -eu
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
umask 077

abort() { printf 'GUARD: BLOCKED - %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }
case "${RO_TEST_ROOT:-/}" in
    /) ROOT= ;;
    /*) [ "${RO_TEST_MODE:-0}" = 1 ] || abort 'test root requires explicit test mode'; ROOT="${RO_TEST_ROOT%/}" ;;
    *) abort 'invalid test root' ;;
esac
[ "$ROOT" != '' ] || [ "${RO_TEST_MODE:-0}" != 1 ] || abort 'test mode cannot operate on the real root'
STATE="$ROOT/etc/router-orchestrator/upgrade"
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

path() { printf '%s/%s' "$ROOT" "$1"; }
sha_file() { sha256sum "$1" | awk '{print $1}'; }
hex64() { case "$1" in *[!0-9a-f]*|'') return 1 ;; esac; [ "${#1}" -eq 64 ]; }
valid_mode() { [ "$1" = 0644 ] || [ "$1" = 0755 ]; }

check_tar_names() {
    # No arbitrary tar path can be extracted, even when a caller provides its hash.
    tar -tf "$1" > "$2" || abort 'invalid tar archive'
    [ "$(wc -l < "$2")" -eq 11 ] || abort 'unexpected tar entry count'
    [ "$(sort -u "$2" | wc -l)" -eq 11 ] || abort 'duplicate tar entry'
    grep -qx 'manifest.txt' "$2" || abort 'manifest missing'
    for rel in $FILES; do
        grep -Fqx "payload/$rel" "$2" || abort "missing or unexpected entry: $rel"
    done
    # The exact count above plus unique names means no additional entries exist.
}

check_payload() {
    dir="$1"
    manifest="$dir/manifest.txt"
    [ -f "$manifest" ] && [ ! -L "$manifest" ] || abort 'manifest not a regular file'
    [ "$(wc -l < "$manifest")" -eq 10 ] || abort 'unexpected manifest line count'
    while read -r digest mode rel extra; do
        [ -z "${extra:-}" ] || abort 'invalid manifest columns'
        hex64 "$digest" || abort 'invalid checksum in manifest'
        valid_mode "$mode" || abort 'invalid permission mode'
        found=0
        for allowed in $FILES; do
            [ "$rel" = "$allowed" ] && found=1
        done
        [ "$found" = 1 ] || abort 'unexpected manifest path'
        file="$dir/payload/$rel"
        [ -f "$file" ] && [ ! -L "$file" ] || abort 'unsafe payload file'
        [ "$(sha_file "$file")" = "$digest" ] || abort "payload hash mismatch: $rel"
    done < "$manifest"
    # Ensure one and only one line for each expected file.
    for rel in $FILES; do
        [ "$(awk -v name="$rel" '$3 == name {n++} END {print n+0}' "$manifest")" -eq 1 ] || abort "manifest mismatch: $rel"
    done
}

preflight_live() {
    if [ "${RO_TEST_MODE:-0}" = 1 ]; then
        [ "${RO_TEST_ROUTER_ID:-}" = main ] || abort 'test identity does not match main'
        [ "${RO_TEST_TRANSPORT:-}" = tcp ] || abort 'test transport not TCP'
        [ "${RO_TEST_PORT:-}" = 18443 ] || abort 'test port mismatch'
        return
    fi
    command -v uci >/dev/null || abort 'uci missing'
    command -v jq >/dev/null || abort 'jq missing'
    [ "$(uci -q get owrtremote.main.router_id)" = main ] || abort 'this pilot is for router main only'
    tr="$(uci -q get owrtremote.main.vps_transport || true)"
    [ "${tr:-tcp}" = tcp ] || abort 'not TCP; no upgrade allowed'
    [ "$(uci -q get owrtremote.main.vps_port)" = 18443 ] || abort 'port not 18443'
    config="$(uci -q get owrtremote.main.xray_config || true)"
    config="${config:-/etc/xray/owrt-remote-client.json}"
    [ -s "$config" ] || abort 'current Xray client config missing'
    jq -e '[.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn") | .streamSettings] as $x | ($x|length==2) and all($x[]; (.network // "tcp")=="tcp" and (.security // "none")=="none")' "$config" >/dev/null || abort 'reverse not exactly 2 x TCP none'
}

stage() {
    [ "$#" -eq 2 ] || abort 'usage: stage /path/bundle.tar 64-character-sha256'
    src="$1"; expected="$2"
    hex64 "$expected" || abort 'expected SHA-256 required'
    [ -f "$src" ] && [ ! -L "$src" ] || abort 'bundle missing or symlink'
    [ "$(wc -c < "$src")" -le 6291456 ] || abort 'candidate too large for constrained OpenWrt routers'
    [ "$(sha_file "$src")" = "$expected" ] || abort 'bundle SHA-256 mismatch'
    preflight_live
    # Staging requires an overlay reserve; never unpack on tmpfs just because /tmp is large.
    available_kb="$(df -Pk "$(path etc)" | awk 'END {print $4}')"
    case "$available_kb" in ''|*[!0-9]*) abort 'cannot measure persistent overlay free space' ;; esac
    [ "$available_kb" -ge 24576 ] || abort 'less than 24 MiB of persistent overlay free space'
    [ ! -e "$STATE/pending" ] || abort 'unfinished update; do not overwrite snapshot'
    [ ! -e "$STATE/backup" ] || abort 'previous backup exists; no overwriting old recovery material'
    [ ! -e "$STATE/stage" ] || abort 'previous stage exists; explicit cleanup required'
    mkdir -p "$STATE" || abort 'cannot use persistent overlay state'
    chmod 700 "$STATE"
    mkdir "$STATE/stage" || abort 'stage conflict'
    trap 'rm -rf "$STATE/stage"' EXIT HUP INT TERM
    cp "$src" "$STATE/stage/candidate.tar"
    [ "$(sha_file "$STATE/stage/candidate.tar")" = "$expected" ] || abort 'copy checksum mismatch'
    check_tar_names "$STATE/stage/candidate.tar" "$STATE/stage/names"
    mkdir "$STATE/stage/unpacked"
    # Stream ONLY named entries into new files. Never extract tar links or paths directly.
    tar -xOf "$STATE/stage/candidate.tar" manifest.txt > "$STATE/stage/unpacked/manifest.txt" || abort 'manifest stream failed'
    for rel in $FILES; do
        dst="$STATE/stage/unpacked/payload/$rel"
        mkdir -p "$(dirname "$dst")"
        tar -xOf "$STATE/stage/candidate.tar" "payload/$rel" > "$dst" || abort "payload stream failed: $rel"
    done
    check_payload "$STATE/stage/unpacked"
    printf '%s\n' "$expected" > "$STATE/stage/archive.sha256"
    trap - EXIT HUP INT TERM
    info 'GUARD: STAGED - archive and file hashes verified. Router software unchanged.'
}

snapshot() {
    # Only prepares a recovery snapshot. It does not stop services or update files.
    [ "$#" -eq 0 ] || abort 'usage: snapshot'
    preflight_live
    [ -s "$STATE/stage/archive.sha256" ] || abort 'verified stage missing'
    [ ! -e "$STATE/backup" ] || abort 'backup already exists; never overwrite'
    [ ! -e "$STATE/pending" ] || abort 'pending transaction exists'
    [ "$(sha_file "$STATE/stage/candidate.tar")" = "$(cat "$STATE/stage/archive.sha256")" ] || abort 'stage hash changed'
    check_payload "$STATE/stage/unpacked"
    mkdir "$STATE/backup" || abort 'cannot create persistent backup'
    trap 'rm -rf "$STATE/backup"' EXIT HUP INT TERM
    mkdir "$STATE/backup/files"
    : > "$STATE/backup/present.txt"
    : > "$STATE/backup/original.sha256"
    for rel in $FILES; do
        old="$(path "$rel")"
        if [ -e "$old" ]; then
            [ -f "$old" ] && [ ! -L "$old" ] || abort "unsafe installed path: $rel"
            mkdir -p "$STATE/backup/files/$(dirname "$rel")"
            cp -p "$old" "$STATE/backup/files/$rel" || abort "could not back up: $rel"
            printf '1 %s\n' "$rel" >> "$STATE/backup/present.txt"
            printf '%s %s\n' "$(sha_file "$old")" "$rel" >> "$STATE/backup/original.sha256"
        else
            printf '0 %s\n' "$rel" >> "$STATE/backup/present.txt"
            printf '%s %s\n' '-' "$rel" >> "$STATE/backup/original.sha256"
        fi
    done
    for rel in etc/config/owrtremote etc/xray/owrt-remote-client.json; do
        old="$(path "$rel")"
        [ -f "$old" ] && [ ! -L "$old" ] || abort "required original missing: $rel"
        mkdir -p "$STATE/backup/files/$(dirname "$rel")"
        cp -p "$old" "$STATE/backup/files/$rel"
        sha_file "$old" >> "$STATE/backup/critical.sha256"
    done
    trap - EXIT HUP INT TERM
    info 'GUARD: SNAPSHOT READY - original files backed up on persistent overlay; no installation performed.'
}

case "${1:-}" in
    stage) shift; stage "$@" ;;
    snapshot) shift; snapshot "$@" ;;
    *) abort 'supported now: stage bundle.tar SHA256 | snapshot. Apply/confirm/watchdog not yet enabled.' ;;
esac
