#!/bin/sh
# Router Orchestrator: READ-ONLY transport parity check for ANY enrolled router.
# Works for legacy TCP and existing WSS. No install, UCI writes, Xray signal,
# port changes or implicit transport migration. Does not print credentials.
set -eu
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
[ "$#" -eq 1 ] && [ "${1:-}" = --inspect ] || {
    printf 'FLEET-CHECK: usage: sh script --inspect\n' >&2
    exit 2
}
log() { printf 'FLEET-CHECK: %s\n' "$*"; }
blocked() { log "BLOCKED - $*"; exit 1; }

command -v jq >/dev/null 2>&1 || blocked 'jq is required'
command -v uci >/dev/null 2>&1 || blocked 'uci is required'
[ -r /etc/openwrt_release ] || blocked 'not an OpenWrt device'
rid="$(uci -q get owrtremote.main.router_id 2>/dev/null || true)"
[ -n "$rid" ] || blocked 'router ID is missing'
case "$rid" in *[!a-zA-Z0-9_.-]*) blocked 'unsafe router ID characters' ;; esac
transport="$(uci -q get owrtremote.main.vps_transport 2>/dev/null || true)"
[ -n "$transport" ] || transport=tcp
case "$transport" in tcp|ws) ;; *) blocked "unsupported transport $transport" ;; esac

port="$(uci -q get owrtremote.main.vps_port 2>/dev/null || true)"
case "$port" in ''|*[!0-9]*) blocked 'VPS port missing/invalid' ;; esac
[ "$port" -ge 1 ] && [ "$port" -le 65535 ] ||
    blocked 'VPS port out of range'

cfg="$(uci -q get owrtremote.main.xray_config 2>/dev/null || true)"
[ -n "$cfg" ] || cfg=/etc/xray/owrt-remote-client.json
case "$cfg" in /*) ;; *) blocked 'Xray config path must be absolute' ;; esac
[ -f "$cfg" ] && [ ! -L "$cfg" ] ||
    blocked 'Xray config absent or a symlink'
[ -r "$cfg" ] || blocked 'Xray config unreadable'

# Validate BOTH reverse outbounds. Never display their UUIDs, endpoints,
# SNI, secrets or complete JSON. Do not treat a single working link as PASS.
if [ "$transport" = tcp ]; then
    jq -e '
      [.outbounds[]? |
        select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")
      ] as $links
      | ($links|length==2)
      and ([$links[].tag]|unique|length==2)
      and all($links[];
         ((.streamSettings.network // "tcp")=="tcp")
         and ((.streamSettings.security // "none")=="none")
      )
    ' "$cfg" >/dev/null ||
        blocked 'TCP UCI and reverse JSON disagree (2 TCP none links required)'
else
    sni="$(uci -q get owrtremote.main.vps_sni 2>/dev/null || true)"
    if [ -z "$sni" ]; then
        sni="$(uci -q get owrtremote.main.vps_host 2>/dev/null || true)"
    fi
    [ -n "$sni" ] || blocked 'WSS SNI unavailable from UCI'
    ws_path="$(uci -q get owrtremote.main.vps_ws_path 2>/dev/null || true)"
    [ -n "$ws_path" ] || ws_path=/owrt-ws/
    case "$ws_path" in /*) ;; *) blocked 'WSS path must begin with /' ;; esac
    jq -e --arg sni "$sni" --arg path "$ws_path" '
      [.outbounds[]? |
        select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")
      ] as $links
      | ($links|length==2)
      and ([$links[].tag]|unique|length==2)
      and all($links[];
         .streamSettings as $stream
         | $stream.network=="ws"
         and $stream.security=="tls"
         and $stream.tlsSettings.serverName==$sni
         and $stream.tlsSettings.allowInsecure==false
         and ($stream.tlsSettings.alpn | index("http/1.1")!=null)
         and $stream.wsSettings.path==$path
         and $stream.wsSettings.host==$sni
      )
    ' "$cfg" >/dev/null ||
        blocked 'WSS UCI and existing reverse JSON disagree (2 WSS TLS links required)'
fi
log "PASS - router=$rid, transport=$transport, existing port=$port"
log 'PASS - both reverse links match UCI. No transport or Xray core changes.'
log 'READ ONLY - no files, processes, routes or services modified.'
