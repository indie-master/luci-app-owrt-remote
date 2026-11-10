#!/bin/sh
# Read-only preflight for the intentionally selected TCP canary router.
# This script does not install, restart, reconfigure or disclose credentials.
set -eu
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"

die() { printf 'PREFLIGHT: BLOCKED - %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }
get() { uci -q get "owrtremote.main.$1" 2>/dev/null || true; }

command -v uci >/dev/null 2>&1 || die 'uci is unavailable'
command -v jq >/dev/null 2>&1 || die 'jq is required to validate existing Xray JSON'
command -v sha256sum >/dev/null 2>&1 || die 'sha256sum is required'

info '===== ROUTER IDENTITY ====='
hostname 2>/dev/null || true
id="$(get router_id)"
name="$(get router_name)"
[ -n "$id" ] || die 'router_id missing; cannot correlate with the HUB'
# The router_id is not a credential, but keep the output concise.
printf 'Router ID: %s\nRouter label: %s\n' "$id" "$name"
info 'Confirm manually that this is the HUB router labelled main before continuing.'

info '===== TRANSPORT ====='
transport="$(get vps_transport)"
[ -n "$transport" ] || transport=tcp
port="$(get vps_port)"
printf 'UCI transport: %s\nUCI port: %s\n' "$transport" "${port:-unknown}"
[ "$transport" = tcp ] || die 'this pilot only allows existing TCP transport'
[ "$port" = 18443 ] || die 'unexpected legacy VPS port; investigate before upgrade'

config="$(get xray_config)"
[ -n "$config" ] || config=/etc/xray/owrt-remote-client.json
[ -s "$config" ] || die 'Xray client configuration is missing'
jq -e '
  [.outbounds[]? | select(.tag == "vps-interconn" or .tag == "vps-ssh-interconn")
    | .streamSettings] as $links |
  ($links | length == 2) and
  all($links[]; ((.network // "tcp") == "tcp") and ((.security // "none") == "none"))
' "$config" >/dev/null 2>&1 ||
  die 'existing reverse outbounds are not exactly two TCP none links'
info 'Xray reverse JSON: 2 x TCP none confirmed (UUIDs not displayed).'

info '===== INSTALLED FILES ====='
for file in /etc/config/owrtremote /usr/sbin/owrt-remote /etc/init.d/owrt-remote "$config"; do
  [ -s "$file" ] || die "missing expected file: $file"
  sha256sum "$file"
done
xraybin="$(get xray_bin)"
[ -n "$xraybin" ] || xraybin=/usr/bin/xray
[ -x "$xraybin" ] || die 'selected Xray binary not executable'
"$xraybin" version 2>/dev/null | head -n 1 || true

info '===== READ-ONLY SERVICES ====='
for service in owrt-remote rpcd uhttpd; do
  if [ -x "/etc/init.d/$service" ]; then
    printf '%s: ' "$service"
    "/etc/init.d/$service" status 2>&1 | head -n 2 || true
  fi
done
info '===== STORAGE ====='
df -h /overlay /tmp 2>/dev/null || true
info '===== INSTALLED UI ====='
if [ -r /www/cgi-bin/owrt-remote ]; then
  grep -m1 '^OWRT_REMOTE_UI_VERSION=' /www/cgi-bin/owrt-remote || true
fi
info 'PREFLIGHT: READ-ONLY PASS. No upgrade was executed.'
