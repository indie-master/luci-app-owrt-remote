#!/bin/sh
# Router Orchestrator: isolated OpenWrt compatibility probe.
# This intentionally NEVER invokes the updater, UCI writes, service controls,
# Xray or network changes. All writes stay in one mktemp directory under /tmp.
set -eu
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
umask 077

fail() { printf 'PROBE: FAIL - %s\n' "$*" >&2; exit 1; }
pass() { printf 'PROBE: PASS - %s\n' "$*"; }

printf '%s\n' '===== ISOLATED OPENWRT COMPATIBILITY PROBE ====='
printf '%s\n' 'No production files, services, UCI or tunnels are changed.'

for app in mktemp mkdir rm cat cp mv chmod cmp tar sha256sum awk grep df od tr nohup sleep sh wc; do
  command -v "$app" >/dev/null 2>&1 || fail "missing applet: $app"
done
pass 'required command applets are present'

# /tmp must exist; the probe never mounts anything or touches /etc.
[ -d /tmp ] && [ ! -L /tmp ] || fail '/tmp is missing or a symlink'
dir="$(mktemp -d /tmp/ro-hardware-probe.XXXXXX)" || fail 'cannot create private /tmp directory'
# Fail-safe cleanup: never pass an unvalidated value to rm -rf.
case "$dir" in
  /tmp/ro-hardware-probe.*) ;;
  *) fail 'mktemp returned a path outside the designated /tmp prefix' ;;
esac
[ -d "$dir" ] && [ ! -L "$dir" ] || fail 'temporary directory is unsafe'
cleanup() {
  case "$dir" in
    /tmp/ro-hardware-probe.*)
      if [ -d "$dir" ] && [ ! -L "$dir" ]; then
        rm -rf "$dir"
      fi
      ;;
  esac
}
trap 'cleanup' EXIT
trap 'exit 130' HUP INT TERM

mkdir -p "$dir/source/payload/usr/sbin" "$dir/restore"
printf '%s\n' 'OPENWRT-PROBE-FIXTURE' > "$dir/source/payload/usr/sbin/owrt-remote"
printf '%s\n' 'fixture manifest' > "$dir/source/manifest.txt"
tar -cf "$dir/fixture.tar" -C "$dir/source" manifest.txt payload/usr/sbin/owrt-remote ||
  fail 'tar cannot create an archive'
tar -tf "$dir/fixture.tar" | grep -Fxq 'payload/usr/sbin/owrt-remote' ||
  fail 'tar -tf cannot enumerate expected member'
tar -xOf "$dir/fixture.tar" 'payload/usr/sbin/owrt-remote' > "$dir/extracted" ||
  fail 'BusyBox tar -xOf cannot stream a selected member'
cmp -s "$dir/source/payload/usr/sbin/owrt-remote" "$dir/extracted" ||
  fail 'streamed tar member differs from original'
pass 'tar -cf, tar -tf, tar -xOf and cmp -s'

cp -p "$dir/extracted" "$dir/restore/current" || fail 'cp -p failed'
chmod 600 "$dir/restore/current" || fail 'chmod failed'
cp -p "$dir/restore/current" "$dir/restore/current.new" || fail 'atomic replacement staging failed'
mv -f "$dir/restore/current.new" "$dir/restore/current" || fail 'same-directory rename failed'
cmp -s "$dir/restore/current" "$dir/extracted" || fail 'renamed file differs'
pass 'cp -p, chmod, same-directory rename'

a="$(sha256sum "$dir/extracted" | awk '{print $1}')" || fail 'sha256sum not usable'
b="$(sha256sum "$dir/restore/current" | awk '{print $1}')" || fail 'second sha256sum not usable'
[ "${#a}" -eq 64 ] && [ "$a" = "$b" ] || fail 'SHA-256 mismatch'
pass 'SHA-256 checks'

nonce="$(od -An -tx1 -N 16 /dev/urandom | tr -d ' \n')" || fail 'od random bytes failure'
[ "${#nonce}" -eq 32 ] || fail 'random nonce is not 16 bytes'
pass 'od and nonce generation'

# Must work on the actual OpenWrt BusyBox, not just a desktop's coreutils.
# /overlay exists on OpenWrt; use /tmp on generic CI hosts without overlayfs.
storage_path=/overlay
if [ ! -d "$storage_path" ]; then
  storage_path=/tmp
  printf '%s\n' 'PROBE: INFO - no /overlay on this test host; measuring /tmp instead'
fi
overlay="$(df -Pk "$storage_path" 2>/dev/null | awk 'END{print $4}')"
case "$overlay" in
  ''|*[!0-9]*) fail "df -Pk $storage_path unavailable" ;;
esac
pass "df -Pk $storage_path"

available="$(awk '$1=="MemAvailable:"{print $2}' /proc/meminfo)"
case "$available" in
  ''|*[!0-9]*) fail 'MemAvailable unavailable' ;;
esac
pass 'MemAvailable parsing'

printf '%s\n' '===== NON-SENSITIVE DEVICE DETAILS ====='
if command -v busybox >/dev/null 2>&1; then
  busybox 2>&1 | head -n 1 || true
fi
printf 'Filesystem available (%s): %s KiB\n' "$storage_path" "$overlay"
printf 'MemAvailable: %s KiB\n' "$available"
printf '%s\n' 'PROBE: PASS - isolated temporary filesystem checks complete.'
printf '%s\n' 'No client upgrade was performed.'
