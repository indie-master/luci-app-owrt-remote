#!/bin/sh
# Cudy main live release inventory. READ-ONLY. Never signals services.
set -eu
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
[ "${1:-}" = --inspect ] && [ "$#" -eq 1 ] ||
  { echo 'PILOT-INVENTORY: usage: --inspect' >&2; exit 2; }
say() { printf 'PILOT-INVENTORY: %s\n' "$*"; }
die() { say "BLOCKED - $*"; exit 1; }
for cmd in ubus uci jq sha256sum awk df; do
  command -v "$cmd" >/dev/null 2>&1 || die "missing $cmd"
done
[ -r /etc/openwrt_release ] && [ "$(id -u)" -eq 0 ] ||
  die 'OpenWrt root required'

router_id="$(uci -q get owrtremote.main.router_id 2>/dev/null)" ||
  die 'router ID unavailable'
[ "$router_id" = main ] || die "expected main, got $router_id"
transport="$(uci -q get owrtremote.main.vps_transport 2>/dev/null || true)"
[ -n "$transport" ] || transport=tcp
port="$(uci -q get owrtremote.main.vps_port 2>/dev/null || true)"
[ "$transport" = tcp ] && [ "$port" = 18443 ] ||
  die "unexpected transport/port $transport/$port"
config="$(uci -q get owrtremote.main.xray_config 2>/dev/null || true)"
[ -n "$config" ] || config=/etc/xray/owrt-remote-client.json
[ "$config" = /etc/xray/owrt-remote-client.json ] ||
  die 'nondefault Xray config path'
[ -f "$config" ] && [ ! -L "$config" ] ||
  die 'Xray config missing or a symlink'
jq -e '
  [.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")
   | .streamSettings] as $links |
  ($links|length==2) and all($links[];
      ((.network//"tcp")=="tcp") and ((.security//"none")=="none"))
' "$config" >/dev/null || die 'reverse must be 2 TCP none links'
say 'PASS - main TCP/18443, reverse contract (UUIDs never printed)'

status="$(ubus call service list '{"name":"owrt-remote","verbose":true}')" ||
  die 'cannot query procd'
pid_for() {
  printf '%s\n' "$status" | jq -er --arg i "$1" '
    .["owrt-remote"].instances[$i] | select(.running==true)
    | .pid | select(type=="number" and . > 1) | tostring'
}
xray_pid="$(pid_for xray)" || die 'Xray instance not running'
hb_pid="$(pid_for heartbeat)" || die 'heartbeat instance not running'
[ "$xray_pid" != "$hb_pid" ] &&
[ -d "/proc/$xray_pid" ] && [ -d "/proc/$hb_pid" ] ||
  die 'invalid/missing managed processes'
say "PASS - procd xray-instance PID=$xray_pid; heartbeat PID=$hb_pid"
say 'NOTE - the xray-instance is a launcher; Belt must verify both reverse connections'

[ ! -e /etc/router-orchestrator/upgrade ] &&
[ ! -L /etc/router-orchestrator/upgrade ] ||
  die 'an existing persistent upgrade state must be investigated'
for f in /etc/init.d/ro-pilot-recovery /etc/rc.d/S08ro-pilot-recovery; do
  [ ! -e "$f" ] && [ ! -L "$f" ] ||
    die "recovery boot hook conflict: $f"
done
all="$(ubus call service list '{}')" || die 'cannot inspect procd'
leftovers="$(printf '%s\n' "$all" | jq -r 'keys[]' |
  awk '/^ro-(pilot-supervised|runtime-test|procd-(probe|dual))-/')"
[ -z "$leftovers" ] || die "stale test service: $leftovers"
say 'PASS - no transaction/recovery collisions or temporary procd instances'
say '===== CRITICAL CONFIG DIGESTS (HASH ONLY, NEVER FILE CONTENT) ====='
[ -f /etc/config/owrtremote ] && [ ! -L /etc/config/owrtremote ] ||
  die 'UCI configuration must be a regular file'
sha256sum /etc/config/owrtremote /etc/xray/owrt-remote-client.json ||
  die 'cannot calculate critical config digests'

[ -f /etc/init.d/owrt-remote ] &&
[ ! -L /etc/init.d/owrt-remote ] || die 'main init script not regular'
startup="$(awk '/^[[:space:]]*START[[:space:]]*=/{
  gsub(/[^0-9]/,"",$0); print; exit
}' /etc/init.d/owrt-remote)"
case "$startup" in ''|*[!0-9]*) die 'unreadable main init priority' ;; esac
[ "$startup" -gt 8 ] || die 'early recovery cannot run before main service'
say "PASS - main START=$startup; early recovery boot ordering possible"

say '===== ORIGINAL TEN FILES (SHA256, NO CONTENT) ====='
files='usr/sbin/owrt-remote
usr/lib/owrt-remote/wan.sh
usr/lib/owrt-remote/logs.sh
etc/init.d/owrt-remote-logs
etc/init.d/owrt-remote
etc/hotplug.d/iface/99-owrt-remote
www/cgi-bin/owrt-remote
usr/share/luci/menu.d/luci-app-owrt-remote.json
usr/share/rpcd/acl.d/luci-app-owrt-remote.json
www/luci-static/resources/view/owrt_remote.js'
present=0
absent=0
for rel in $files; do
  piece="/"; rest="$rel"
  while [ -n "$rest" ]; do
    case "$rest" in
      */*) part="${rest%%/*}"; rest="${rest#*/}" ;;
      *) part="$rest"; rest='' ;;
    esac
    piece="$piece$part"
    [ ! -L "$piece" ] || die "symlink in target path: $rel"
    [ -z "$rest" ] || piece="$piece/"
  done
  if [ -f "/$rel" ]; then
    sha256sum "/$rel" || die "unreadable original: $rel"
    present=$((present+1))
  elif [ ! -e "/$rel" ]; then
    printf 'ABSENT /%s\n' "$rel"
    absent=$((absent+1))
  else
    die "unexpected installed file type: $rel"
  fi
done
[ $((present+absent)) -eq 10 ] || die 'bad inventory'
say "PASS - 10 paths inventoried: $present files, $absent absent"

mem="$(awk '$1=="MemAvailable:" {print $2}' /proc/meminfo)"
disk="$(df -Pk /overlay | awk 'END{print $4}')"
case "$mem:$disk" in *[!0-9:]*|:*|*:) die 'resource readings invalid' ;; esac
[ "$mem" -ge 20480 ] && [ "$disk" -ge 16384 ] ||
  die 'insufficient RAM (20MiB) or overlay reserve (16MiB)'
say "PASS - free RAM ${mem} KiB, free overlay ${disk} KiB"
say 'READ-ONLY BASELINE PASS - no live file, service, network or UCI changed'
say 'Live update remains disabled until boot recovery and Belt approval are ready'
