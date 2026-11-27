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
# Synthetic-root mode must never be usable to point at an arbitrary live
# path. Verify both the path shape and the canonical path, plus a marker
# created by the disposable fixture before any router code-file writes.
if [ "${RO_TEST_MODE:-0}" = 1 ]; then
    case "$ROOT" in
        /tmp/ro-transaction-test-*/router|/tmp/ro-guard-test-*/router|/tmp/ro-pilot-fixture.*/router) ;;
        *) abort 'unapproved synthetic-root location' ;;
    esac
    [ -d "$ROOT" ] && [ ! -L "$ROOT" ] || abort 'synthetic root missing or symlink'
    canonical="$(cd -P "$ROOT" && pwd -P)" || abort 'cannot resolve synthetic root'
    [ "$canonical" = "$ROOT" ] || abort 'synthetic root includes symlink or traversal'
    [ -f "$ROOT/.ro-pilot-synthetic-root" ] &&
    [ ! -L "$ROOT/.ro-pilot-synthetic-root" ] || abort 'synthetic-root marker missing'
fi

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

# Do not follow nested links inside a marked synthetic root. A safe
# top-level /tmp router fixture is not enough if e.g. its 'etc' child
# points to the device's real /etc. Validate every prefix before writes.
check_path_no_symlinks() {
    relative="$1"
    prefix="$ROOT"
    rest="$relative"
    while [ -n "$rest" ]; do
        case "$rest" in
            */*) part="${rest%%/*}"; rest="${rest#*/}" ;;
            *) part="$rest"; rest='' ;;
        esac
        [ -n "$part" ] && [ "$part" != . ] && [ "$part" != .. ] ||
            abort "unsafe path component: $relative"
        prefix="$prefix/$part"
        [ ! -L "$prefix" ] || abort "symlink in router path: $relative"
    done
}
check_router_tree() {
    for rel in $FILES; do
        check_path_no_symlinks "$rel"
    done
    for rel in etc/router-orchestrator/upgrade \
               etc/config/owrtremote etc/xray/owrt-remote-client.json \
               etc/init.d/ro-pilot-recovery etc/rc.d/S08ro-pilot-recovery; do
        check_path_no_symlinks "$rel"
    done
}

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
    [ "$(id -u)" = 0 ] || abort 'root required for persistent snapshot'
    [ -r /etc/openwrt_release ] || abort 'not an OpenWrt device'
    command -v uci >/dev/null || abort 'uci missing'
    command -v jq >/dev/null || abort 'jq missing'
    # The caller must pin the selected enrolled router and its CURRENT
    # transport. Preparing an update must not silently migrate the tunnel.
    rid="$(uci -q get owrtremote.main.router_id || true)"
    case "$rid" in ''|*[!a-zA-Z0-9_.-]*) abort 'invalid router identity' ;; esac
    [ "$rid" = "${RO_EXPECT_ROUTER_ID:-}" ] || abort 'router identity mismatch'
    tr="$(uci -q get owrtremote.main.vps_transport || true)"
    tr="${tr:-tcp}"
    case "$tr" in tcp|ws) ;; *) abort 'only existing TCP/WSS may be prepared' ;; esac
    [ "$tr" = "${RO_EXPECT_TRANSPORT:-}" ] || abort 'existing transport mismatch'
    port="$(uci -q get owrtremote.main.vps_port || true)"
    case "$port" in ''|*[!0-9]*) abort 'invalid current port' ;; esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || abort 'port out of range'
    [ "$port" = "${RO_EXPECT_PORT:-}" ] || abort 'current port mismatch'
    config="$(uci -q get owrtremote.main.xray_config || true)"
    config="${config:-/etc/xray/owrt-remote-client.json}"
    # The existing snapshot/rollback contract only covers this path.
    [ "$config" = /etc/xray/owrt-remote-client.json ] || abort 'custom Xray path requires separate review'
    [ -f "$config" ] && [ ! -L "$config" ] && [ -s "$config" ] || abort 'current Xray config missing or unsafe'
    sni="$(uci -q get owrtremote.main.vps_sni || true)"
    [ -n "$sni" ] || sni="$(uci -q get owrtremote.main.vps_host || true)"
    ws_path="$(uci -q get owrtremote.main.vps_ws_path || true)"
    ws_path="${ws_path:-/owrt-ws/}"
    jq -e --arg tr "$tr" --arg port "$port" --arg sni "$sni" --arg path "$ws_path" '
      [.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")] as $links
      | ($links|length==2) and ([$links[].tag]|unique|length==2)
      and all($links[];
        .protocol=="vless"
        and (.settings.port == ($port|tonumber))
        and (if $tr=="tcp" then
          ((.streamSettings.network // "tcp")=="tcp")
          and ((.streamSettings.security // "none")=="none")
        else
          $sni!="" and (.streamSettings.network=="ws")
          and (.streamSettings.security=="tls")
          and (.streamSettings.tlsSettings.serverName==$sni)
          and (.streamSettings.tlsSettings.allowInsecure==false)
          and (.streamSettings.tlsSettings.alpn|index("http/1.1")!=null)
          and (.streamSettings.wsSettings.path==$path)
          and (.streamSettings.wsSettings.host==$sni)
        end))
    ' "$config" >/dev/null || abort 'existing reverse links disagree with pinned transport/port'
}

stage() {
    [ "$#" -eq 2 ] || abort 'usage: stage /path/bundle.tar 64-character-sha256'
    src="$1"; expected="$2"
    hex64 "$expected" || abort 'expected SHA-256 required'
    [ -f "$src" ] && [ ! -L "$src" ] || abort 'bundle missing or symlink'
    [ "$(wc -c < "$src")" -le 6291456 ] || abort 'candidate too large for constrained OpenWrt routers'
    [ "$(sha_file "$src")" = "$expected" ] || abort 'bundle SHA-256 mismatch'
    preflight_live
    check_router_tree
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
    if [ "${RO_TEST_MODE:-0}" != 1 ]; then
        printf '%s\n' "$rid" "$tr" "$port" > "$STATE/stage/target"
        sha_file /etc/config/owrtremote > "$STATE/stage/uci.sha256"
        sha_file "$config" > "$STATE/stage/xray.sha256"
    fi
    trap - EXIT HUP INT TERM
    info 'GUARD: STAGED - archive and file hashes verified. Router software unchanged.'
}

snapshot() {
    # Only prepares a recovery snapshot. It does not stop services or update files.
    [ "$#" -eq 0 ] || abort 'usage: snapshot'
    preflight_live
    check_router_tree
    [ -s "$STATE/stage/archive.sha256" ] || abort 'verified stage missing'
    if [ "${RO_TEST_MODE:-0}" != 1 ]; then
        [ "$(cat "$STATE/stage/target")" = "$(printf '%s\n' "$rid" "$tr" "$port")" ] || abort 'stage belongs to another target'
        [ "$(sha_file /etc/config/owrtremote)" = "$(cat "$STATE/stage/uci.sha256")" ] || abort 'UCI changed after stage'
        [ "$(sha_file "$config")" = "$(cat "$STATE/stage/xray.sha256")" ] || abort 'Xray JSON changed after stage'
    fi
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
    if [ "${RO_TEST_MODE:-0}" != 1 ]; then
        [ "$(sha_file /etc/config/owrtremote)" = "$(cat "$STATE/stage/uci.sha256")" ] || abort 'UCI changed during snapshot'
        [ "$(sha_file "$config")" = "$(cat "$STATE/stage/xray.sha256")" ] || abort 'Xray JSON changed during snapshot'
        [ "$(sha_file "$STATE/backup/files/etc/config/owrtremote")" = "$(cat "$STATE/stage/uci.sha256")" ] || abort 'UCI backup digest mismatch'
        [ "$(sha_file "$STATE/backup/files/etc/xray/owrt-remote-client.json")" = "$(cat "$STATE/stage/xray.sha256")" ] || abort 'Xray backup digest mismatch'
        sync
    fi
    trap - EXIT HUP INT TERM
    info 'GUARD: SNAPSHOT READY - original files backed up on persistent overlay; no installation performed.'
}


# No persistent writes. This checks real-device prerequisites before stage.
inspect() {
    [ "$#" -eq 0 ] || abort 'usage: inspect'
    [ "${RO_TEST_MODE:-0}" != 1 ] || abort 'inspect requires real OpenWrt'
    preflight_live
    check_router_tree
    [ -z "$(uci -q changes owrtremote)" ] || abort 'uncommitted UCI changes'
    for tool in tar sha256sum awk sed cmp df sync ubus; do
        command -v "$tool" >/dev/null 2>&1 || abort "missing tool: $tool"
    done
    [ -x /sbin/procd ] || abort 'procd unavailable'
    ubus -t 3 list service | grep -qx service || abort 'procd service API unavailable'
    free_kb="$(df -Pk /etc | awk 'END {print $4}')"
    ram_kb="$(awk '$1=="MemAvailable:" {print $2}' /proc/meminfo)"
    case "$free_kb" in ''|*[!0-9]*) abort 'overlay space unavailable' ;; esac
    case "$ram_kb" in ''|*[!0-9]*) abort 'available RAM unknown' ;; esac
    info "GUARD: TARGET $rid transport=$tr port=$port"
    info "GUARD: FREE OVERLAY KiB=$free_kb; AVAILABLE RAM KiB=$ram_kb"
    [ "$free_kb" -ge 24576 ] || abort 'less than 24 MiB persistent overlay free'
    [ "$ram_kb" -ge 20480 ] || abort 'less than 20 MiB available RAM'
    for entry in pending armed stage backup transaction.sh; do
        [ ! -e "$STATE/$entry" ] && [ ! -L "$STATE/$entry" ] ||
            abort "existing transaction artifact: $entry; inspect manually"
    done
    present_count=0
    for rel in $FILES; do
        if [ -f "$(path "$rel")" ]; then
            present_count=$((present_count + 1))
        elif [ -e "$(path "$rel")" ]; then
            abort "unexpected file type: $rel"
        fi
    done
    info "GUARD: EXISTING FILES=$present_count/10"
    info 'GUARD: INSPECT PASS - no persistent files or services changed.'
}

case "${1:-}" in
    inspect) shift; inspect "$@" ;;
    stage) shift; stage "$@" ;;
    snapshot) shift; snapshot "$@" ;;
    *) abort 'supported now: stage bundle.tar SHA256 | snapshot. Apply/confirm/watchdog not yet enabled.' ;;
esac
