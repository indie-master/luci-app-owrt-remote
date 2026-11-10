#!/bin/sh
# Explicitly version-pinned fresh-router bootstrap. NOT for upgrades.
# --check is read-only; --install is for a clean OpenWrt device with local
# recovery access. Existing agents must use the separately guarded updater.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
fatal() { printf 'FRESH-INSTALL: BLOCKED - %s\n' "$*" >&2; exit 1; }
info() { printf 'FRESH-INSTALL: %s\n' "$*"; }
mode="${1:-}"
case "$mode" in --check|--install) ;; *) fatal 'usage: --check [40-hex-revision] | --install 40-hex-revision' ;; esac
rev="${2:-}"
if [ -n "$rev" ]; then
    case "$rev" in ''|*[!0-9a-f]*) fatal 'revision must be 40 lowercase hex characters' ;; esac
    [ "${#rev}" -eq 40 ] || fatal 'revision must be a full 40-hex Git commit'
fi
[ "$mode" != --install ] || [ -n "$rev" ] || fatal 'an immutable revision is required to install'
[ "$#" -le 2 ] || fatal 'unexpected arguments'

test_mode="${RO_FRESH_TEST_MODE:-0}"
if [ "$test_mode" = 1 ]; then
    ROOT="${RO_TEST_ROOT:-}"
    case "$ROOT" in
        /tmp/ro-fresh-install-test-*/router) ;;
        *) fatal 'unsafe test root' ;;
    esac
    [ -d "$ROOT" ] && [ ! -L "$ROOT" ] ||
        fatal 'synthetic root missing or symlink'
    [ "$(cd -P "$ROOT" && pwd -P)" = "$ROOT" ] ||
        fatal 'synthetic root not canonical'
    [ -f "$ROOT/.ro-fresh-install-test-root" ] &&
    [ ! -L "$ROOT/.ro-fresh-install-test-root" ] ||
        fatal 'synthetic root marker missing'
else
    [ -z "${RO_TEST_ROOT:-}" ] || fatal 'test root cannot be used in live mode'
    ROOT=''
    [ "$(id -u)" -eq 0 ] || fatal 'root privileges required'
    [ -f /etc/openwrt_release ] || fatal 'not an OpenWrt system'
fi

path() { printf '%s/%s' "$ROOT" "$1"; }
safe_path() {
    rel="$1"; prefix="$ROOT"; rest="$rel"
    while [ -n "$rest" ]; do
        case "$rest" in
            */*) part="${rest%%/*}"; rest="${rest#*/}" ;;
            *) part="$rest"; rest='' ;;
        esac
        [ -n "$part" ] && [ "$part" != . ] && [ "$part" != .. ] ||
            fatal 'unsafe relative path'
        prefix="$prefix/$part"
        [ ! -L "$prefix" ] || fatal "symlink in target path: $rel"
    done
}
# No overwrite or adoption: a partially installed router must be recovered
# manually and cannot be mistaken for a fresh enrollment.
for rel in etc/config/owrtremote usr/sbin/owrt-remote \
           etc/init.d/owrt-remote etc/xray/owrt-remote-client.json \
           etc/owrt-remote/web.key etc/router-orchestrator/upgrade/pending; do
    safe_path "$rel"
    [ ! -e "$(path "$rel")" ] ||
        fatal "existing state at $rel; use guarded upgrade/recovery instead"
done
# --check must validate all directories the installer may touch, not just
# the six enrollment sentinels.
for rel in usr/lib/owrt-remote/wan.sh usr/lib/owrt-remote/logs.sh \
           etc/init.d/owrt-remote-logs etc/hotplug.d/iface/99-owrt-remote \
           www/cgi-bin/owrt-remote www/luci-static/resources/view/owrt_remote.js \
           usr/share/luci/menu.d/luci-app-owrt-remote.json \
           usr/share/rpcd/acl.d/luci-app-owrt-remote.json \
           etc/init.d/rpcd etc/init.d/uhttpd etc/rc.d \
           usr/lib/lua/luci/controller/owrt_remote.lua tmp; do
    safe_path "$rel"
done
if [ "$test_mode" != 1 ]; then
    command -v ubus >/dev/null 2>&1 ||
        fatal 'missing prerequisite: ubus'
    service_json="$(ubus call service list '{"name":"owrt-remote"}' 2>/dev/null)" ||
        fatal 'cannot inspect existing procd services'
    [ -z "$(printf '%s\n' "$service_json" | grep -F '"owrt-remote"' || true)" ] ||
        fatal 'existing procd owrt-remote service detected'
    for cmd in wget sha256sum jq tar cp mv chmod dd hexdump; do
        command -v "$cmd" >/dev/null 2>&1 || fatal "missing prerequisite: $cmd"
    done
    available="$(df -Pk /overlay | awk 'END{print $4}')"
    case "$available" in ''|*[!0-9]*) fatal 'cannot inspect overlay' ;; esac
    [ "$available" -ge 16384 ] || fatal 'less than 16 MiB free on overlay'
fi
[ "$mode" != --check ] || {
    info 'PASS - no existing Remote HUB installation or unsafe target paths'
    info 'Check mode changed no files or services.'
    exit 0
}

src_base="https://raw.githubusercontent.com/indie-master/luci-app-owrt-remote/$rev"
stage="$(mktemp -d /tmp/ro-fresh-bootstrap.XXXXXX)" ||
    fatal 'cannot prepare temporary download directory'
case "$stage" in /tmp/ro-fresh-bootstrap.*) ;; *) fatal 'unexpected temporary directory' ;; esac
cleanup() {
    case "$stage" in /tmp/ro-fresh-bootstrap.*)
        [ -d "$stage" ] && [ ! -L "$stage" ] && rm -rf "$stage" ;;
    esac
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

files='usr/sbin/owrt-remote
usr/lib/owrt-remote/wan.sh
usr/lib/owrt-remote/logs.sh
etc/init.d/owrt-remote-logs
etc/init.d/owrt-remote
etc/hotplug.d/iface/99-owrt-remote
etc/config/owrtremote
www/cgi-bin/owrt-remote
usr/share/luci/menu.d/luci-app-owrt-remote.json
usr/share/rpcd/acl.d/luci-app-owrt-remote.json
www/luci-static/resources/view/owrt_remote.js'
# The original installer also touches CGI/LuCI caches and may reload
# existing rpcd/uhttpd; never traverse a nested synthetic-root symlink.
for rel in $files \
           etc/init.d/rpcd etc/init.d/uhttpd etc/rc.d \
           usr/lib/lua/luci/controller/owrt_remote.lua tmp; do
    safe_path "$rel"
done
for rel in install.sh $files; do
    case "$rel" in
        install.sh) dst="$stage/install.sh" ;;
        *) dst="$stage/files/$rel" ;;
    esac
    mkdir -p "$(dirname "$dst")"
    if [ "$test_mode" = 1 ]; then
        source_dir="${RO_FRESH_TEST_SOURCE:-}"
        [ -d "$source_dir" ] || fatal 'test source unavailable'
        case "$rel" in
            install.sh) input="$source_dir/install.sh" ;;
            *) input="$source_dir/files/$rel" ;;
        esac
        cp "$input" "$dst" || fatal "missing test payload: $rel"
    else
        case "$rel" in
            install.sh) url="$src_base/install.sh" ;;
            *) url="$src_base/files/$rel" ;;
        esac
        wget -T 15 -O "$dst" "$url" >/dev/null 2>&1 ||
            fatal "download failed before installation: $rel"
    fi
    [ -s "$dst" ] && [ ! -L "$dst" ] ||
        fatal "empty/unsafe staged file: $rel"
    # The Git commit is immutable and every file is downloaded before install.
    sha256sum "$dst" >> "$stage/staged.sha256" ||
        fatal "could not hash: $rel"
done
sh -n "$stage/install.sh" || fatal 'installer shell syntax invalid'
sh -n "$stage/files/usr/sbin/owrt-remote" || fatal 'agent shell syntax invalid'

# Make absolutely sure no other process enrolled the router during download.
for rel in etc/config/owrtremote usr/sbin/owrt-remote \
           etc/init.d/owrt-remote etc/xray/owrt-remote-client.json; do
    safe_path "$rel"
    [ ! -e "$(path "$rel")" ] ||
        fatal "new conflicting installation appeared: $rel"
done

info "All 12 installation files staged from pinned revision $rev"
info 'New-router install requires local recovery; existing installations remain blocked.'
if ROOT="$ROOT" RAW_URL="$src_base" \
   OWRT_REMOTE_SKIP_WOL_PACKAGES=1 \
   sh "$stage/install.sh" > "$stage/install.log" 2>&1; then
    info 'INSTALLED - initial files written; configure UCI/Hub and verify Xray before declaring operational'
    info 'The web key is stored locally in /etc/owrt-remote/web.key; do not publish it.'
else
    # Never print the original installer stdout (it may contain web.key).
    fatal 'installer failed; examine the device locally before retrying; no automatic rollback for fresh install'
fi
