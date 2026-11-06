#!/bin/sh
# Synthetic procd instance worker. Never touches UCI, Xray or live services.
# Valid only under a named /tmp fixture created by the matching probe.
set -eu
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
umask 077

die() { printf 'DUAL-WORKER: FAIL - %s\n' "$*" >&2; exit 1; }
mode="${1:-}"
dir="${2:-}"
case "$dir" in
  /tmp/ro-procd-dual.*) ;;
  *) die 'not a dedicated temporary fixture' ;;
esac
[ -d "$dir" ] && [ ! -L "$dir" ] || die 'fixture absent or symlink'
real="$(cd -P "$dir" && pwd -P)" || die 'cannot resolve fixture'
[ "$real" = "$dir" ] || die 'non-canonical fixture path'
[ -f "$dir/.dual-probe-marker" ] && [ ! -L "$dir/.dual-probe-marker" ] ||
  die 'fixture marker absent'

uptime_seconds() {
  [ -r /proc/uptime ] || return 1
  v="$(awk 'NR==1{print int($1)}' /proc/uptime)" || return 1
  case "$v" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$v"
}
case "$mode" in
  primary)
    printf '%s\n' "$$" >> "$dir/primary-starts"
    exec sleep 120
    ;;
  secondary)
    printf '%s\n' "$$" >> "$dir/secondary-starts"
    printf 'SECONDARY_START pid=%s uptime=%s pinned=%s\n' \
      "$$" "$(uptime_seconds)" "$(cat "$dir/deadline")" >> "$dir/secondary-events"
    while [ -f "$dir/armed" ]; do
      [ -f "$dir/pending" ] || { sleep 1; continue; }
      IFS= read -r deadline < "$dir/deadline" ||
        die 'missing synthetic deadline'
      case "$deadline" in ''|*[!0-9]*) die 'invalid synthetic deadline' ;; esac
      now="$(uptime_seconds)" || die 'uptime not available'
      if [ "$now" -ge "$deadline" ]; then
        printf 'SECONDARY_EXPIRE pid=%s uptime=%s pinned=%s\n' \
          "$$" "$now" "$deadline" >> "$dir/secondary-events"
        : > "$dir/synthetic-rollback"
        rm -f "$dir/pending" "$dir/armed"
        break
      fi
      sleep 1
    done
    # Stay running until service deletion so an intentional successful
    # expiry doesn't cause an unnecessary unlimited-respawn loop.
    exec sleep 120
    ;;
  safety-cleanup)
    name="${3:-}"
    case "$name" in ro-procd-dual-[0-9a-f]*) ;; *) die 'unsafe service name' ;; esac
    # Give the parent enough time to observe original deadline + grace
    # even on a slow device; delete independently if SSH disconnects.
    sleep 75
    # Remove the entire isolated service if the parent shell disconnected.
    # Do NOT remove the temporary fixture: /tmp is cleared on reboot.
    ubus call service delete "{\"name\":\"$name\"}" >/dev/null 2>&1 || true
    ;;
  *)
    die 'allowed modes: primary | secondary | safety-cleanup'
    ;;
esac
