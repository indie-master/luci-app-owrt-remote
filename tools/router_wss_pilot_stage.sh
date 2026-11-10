#!/bin/sh
# Stage a WSS candidate for an explicitly selected OpenWrt router.
# This does not constrain the model, router_id, or original reverse port.
# No live UCI writes, no service restarts, no Xray changes.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
log() { printf 'WSS-STAGE: %s\n' "$*"; }
fail() { log "BLOCKED - $*"; exit 1; }
[ "$#" -eq 4 ] && [ "$1" = --stage ] || fail 'usage: sh script --stage EXPECTED_ROUTER_ID CANDIDATE_AGENT_PATH WSS_HOST'
expected_id="$2"
agent="$3"
wss_host="$4"
case "$wss_host" in ''|*[!a-zA-Z0-9.-]*|.*|*..*|*.) fail 'invalid WSS hostname' ;; esac
case "$agent" in /tmp/ro-wss-agent-*) ;; *) fail 'candidate agent must be under /tmp/ro-wss-agent-*' ;; esac
[ -f "$agent" ] && [ ! -L "$agent" ] && [ -x "$agent" ] || fail 'candidate agent must be an executable regular file'
# BusyBox builds may not include stat(1). ls -ldn is standard on OpenWrt.
# Parse only fixed metadata fields; never print candidate contents.
agent_meta="$(LC_ALL=C ls -ldn "$agent" 2>/dev/null)" || fail 'cannot inspect candidate agent'
agent_permissions="$(printf '%s\n' "$agent_meta" | awk '{print $1}')"
agent_owner="$(printf '%s\n' "$agent_meta" | awk '{print $3}')"
[ "$agent_owner" = 0 ] || fail 'candidate agent must be root-owned'
[ "$agent_permissions" = '-rwx------' ] || fail 'candidate agent must have mode 700'
case "$expected_id" in ''|*[!a-zA-Z0-9_.-]*) fail 'invalid expected router ID' ;; esac
[ -f /etc/openwrt_release ] || fail 'OpenWrt required'
[ "$(id -u)" -eq 0 ] || fail 'root required'
for c in uci jq mktemp sha256sum cmp; do command -v "$c" >/dev/null 2>&1 || fail "missing $c"; done
[ "$(uci -q get owrtremote.main.router_id || true)" = "$expected_id" ] || fail 'router identity differs from the selected pilot'
transport="$(uci -q get owrtremote.main.vps_transport || true)"
[ -z "$transport" ] || [ "$transport" = tcp ] || fail 'expected original TCP UCI'
original_port="$(uci -q get owrtremote.main.vps_port || true)"
case "$original_port" in ''|*[!0-9]*) fail 'original port missing/invalid' ;; esac
[ "$original_port" -ge 1 ] && [ "$original_port" -le 65535 ] || fail 'original port out of range'
case "$(uci -q get owrtremote.main.wan_direct || echo 0)" in
  1|yes|true|on|enabled) fail 'WAN-direct config needs separate staged review' ;;
esac
[ -x /usr/sbin/owrt-remote ] || fail 'installed agent missing'
grep -Fq 'if [ "$vps_transport" = ws ]; then' "$agent" || fail 'candidate agent does not support WSS'
grep -Fq 'OWRT_REMOTE_UCI_CONFIG_DIR' "$agent" || fail 'candidate agent missing explicit shadow-UCI support'
xray_config="$(uci -q get owrtremote.main.xray_config || true)"
[ -n "$xray_config" ] || xray_config=/etc/xray/owrt-remote-client.json
[ "$xray_config" = /etc/xray/owrt-remote-client.json ] || fail 'custom Xray config needs review'
[ -f "$xray_config" ] && [ ! -L "$xray_config" ] || fail 'unsafe Xray config'
[ -f /etc/config/owrtremote ] && [ ! -L /etc/config/owrtremote ] || fail 'unsafe UCI config'
jq -e '
 [.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")] as $l |
 ($l|length)==2 and ([$l[].tag]|unique|length)==2 and
 all($l[]; (.streamSettings.network // "tcp")=="tcp" and
           (.streamSettings.security // "none")=="none")
' "$xray_config" >/dev/null || fail 'live reverse JSON is not both TCP/none'
[ ! -e /etc/router-orchestrator/wss-pilot ] || fail 'previous stage exists: manual review required'
work="$(mktemp -d /tmp/ro-wss-stage.XXXXXX)" || fail 'mktemp failed'
trap 'rm -rf "$work"' EXIT HUP INT TERM
mkdir -p "$work/config" "$work/uci"
cp -p /etc/config/owrtremote "$work/config/owrtremote"
cp -p /etc/config/owrtremote "$work/uci/owrtremote"
uci -c "$work/uci" set owrtremote.main.vps_transport=ws
uci -c "$work/uci" set owrtremote.main.vps_host="$wss_host"
uci -c "$work/uci" set owrtremote.main.vps_port=443
uci -c "$work/uci" set owrtremote.main.vps_sni="$wss_host"
uci -c "$work/uci" set owrtremote.main.vps_ws_path=/owrt-ws/
uci -c "$work/uci" commit owrtremote
OWRT_REMOTE_UCI_CONFIG_DIR="$work/uci" "$agent" render-client --stdout > "$work/candidate.json" 2>"$work/render.err" ||
    fail 'installed agent could not render WSS candidate'
jq -e --arg sni "$wss_host" '
  [.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")] as $l |
  ($l|length)==2 and ([$l[].tag]|unique|length)==2 and
  all($l[]; .streamSettings.network=="ws" and
       .streamSettings.security=="tls" and
       .streamSettings.tlsSettings.serverName==$sni and
       .settings.address==$sni and .streamSettings.wsSettings.host==$sni and
       .streamSettings.tlsSettings.allowInsecure==false and
       (.streamSettings.tlsSettings.alpn | index("http/1.1") != null) and
       .streamSettings.wsSettings.path=="/owrt-ws/" and
       .settings.port==443)
' "$work/candidate.json" >/dev/null || fail 'WSS candidate failed transport validation'
# The generator must not alter live inputs.
cmp -s "$work/config/owrtremote" /etc/config/owrtremote || fail 'live UCI changed during stage'
# Stage on persistent overlay. Fail closed when storage budget is insufficient.
free_kb="$(df -Pk /overlay | awk 'END{print $4}')"
case "$free_kb" in ''|*[!0-9]*) fail 'invalid overlay free space';; esac
[ "$free_kb" -ge 24576 ] || fail 'less than 24 MiB overlay available'
mkdir -p /etc/router-orchestrator
stage=/etc/router-orchestrator/wss-pilot
mkdir "$stage" || fail 'cannot create persistent stage'
cp -p "$work/config/owrtremote" "$stage/original-uci"
cp -p "$xray_config" "$stage/original-xray.json"
cp -p "$work/uci/owrtremote" "$stage/candidate-uci"
cp -p "$work/candidate.json" "$stage/candidate-xray.json"
(
 cd "$stage"
 sha256sum original-uci original-xray.json candidate-uci candidate-xray.json > SHA256SUMS
)
chmod 700 "$stage"
chmod 600 "$stage"/*
printf '%s\n' "$expected_id" > "$stage/router-id"
printf '%s\n' "$original_port" > "$stage/original-port"
chmod 600 "$stage/router-id" "$stage/original-port"
log "PILOT: $expected_id; original TCP port: $original_port"
log 'STAGED ONLY — installed agent, running TCP and live configuration unchanged'
log 'No apply/confirm is implemented by this staging script'
log "Stored protected originals and candidate in $stage"
log 'Run independent pinned Xray syntax test and guarded watchdog before switching'
