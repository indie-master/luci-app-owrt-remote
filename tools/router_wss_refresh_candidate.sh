#!/bin/sh
# Refresh a staged WSS candidate ONLY; never touch active OpenWrt service or UCI.
set -eu
umask 077
ST=/etc/router-orchestrator/wss-pilot
CANDIDATE=/tmp/ro-wss-agent-pilot-next
OUT=/tmp/ro-wss-refresh.$$
die() { printf 'WSS-REFRESH: BLOCKED - %s\n' "$*" >&2; exit 1; }
cleanup() { rm -rf "$OUT"; }
trap cleanup EXIT HUP INT TERM
[ "$#" -eq 2 ] && [ "$1" = --candidate-only ] || die 'usage: script --candidate-only WSS_HOST'
HOST="$2"
case "$HOST" in ''|*[!a-zA-Z0-9.-]*|.*|*..*|*.) die 'invalid WSS hostname' ;; esac
command -v uci >/dev/null 2>&1 || die 'uci missing'
command -v jq >/dev/null 2>&1 || die 'jq missing'
[ ! -e "$ST/pending" ] && [ ! -e "$ST/armed" ] || die 'migration still armed'
[ -d "$ST" ] && [ ! -L "$ST" ] || die 'staging missing'
for f in original-uci original-xray.json candidate-uci candidate-xray.json SHA256SUMS original-agent candidate-agent original-agent.sha256 candidate-agent.sha256 router-id; do
  [ -f "$ST/$f" ] && [ ! -L "$ST/$f" ] || die "missing $f"
done
[ -x "$CANDIDATE" ] && [ -f "$CANDIDATE" ] && [ ! -L "$CANDIDATE" ] || die 'new candidate agent missing'
[ -f /etc/config/owrtremote ] && [ ! -L /etc/config/owrtremote ] || die 'unsafe live UCI'
[ -f /etc/xray/owrt-remote-client.json ] && [ ! -L /etc/xray/owrt-remote-client.json ] || die 'unsafe live Xray'
[ -f /usr/sbin/owrt-remote ] || die 'live agent missing'
cmp -s /etc/config/owrtremote "$ST/original-uci" || die 'live UCI no longer matches original'
cmp -s /etc/xray/owrt-remote-client.json "$ST/original-xray.json" || die 'live Xray no longer matches original'
cmp -s /usr/sbin/owrt-remote "$ST/original-agent" || die 'live agent no longer matches original'
(cd "$ST" && sha256sum -c SHA256SUMS >/dev/null) || die 'staging checksums mismatch'
[ "$(sha256sum "$ST/original-agent" | awk '{print $1}')" = "$(cat "$ST/original-agent.sha256")" ] || die 'agent backup mismatch'
[ "$(sha256sum "$ST/candidate-agent" | awk '{print $1}')" = "$(cat "$ST/candidate-agent.sha256")" ] || die 'current candidate agent mismatch'
[ "$(uci -q get owrtremote.main.router_id 2>/dev/null || true)" = "$(cat "$ST/router-id")" ] || die 'router identity mismatch'
mkdir -p "$OUT/uci"
cp -p "$ST/candidate-uci" "$OUT/uci/owrtremote"
uci -c "$OUT/uci" set owrtremote.main.vps_host="$HOST"
uci -c "$OUT/uci" set owrtremote.main.vps_port=443
uci -c "$OUT/uci" set owrtremote.main.vps_transport=ws
uci -c "$OUT/uci" set owrtremote.main.vps_sni="$HOST"
uci -c "$OUT/uci" set owrtremote.main.vps_ws_path=/owrt-ws/
uci -c "$OUT/uci" commit owrtremote
OWRT_REMOTE_UCI_CONFIG_DIR="$OUT/uci" "$CANDIDATE" render-client --stdout > "$OUT/new-xray.json" 2>"$OUT/render-error" ||
  die 'candidate rendering failed'
jq -e --arg host "$HOST" '[.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")] as $v |
  ($v|length)==2 and ([$v[].tag]|unique|length)==2 and
  all($v[]; .settings.port==443 and .settings.address==$host and .streamSettings.network=="ws" and
    .streamSettings.security=="tls" and .streamSettings.tlsSettings.allowInsecure==false and
    .streamSettings.tlsSettings.serverName==$host and .streamSettings.wsSettings.host==$host and
    .streamSettings.wsSettings.path=="/owrt-ws/")' "$OUT/new-xray.json" >/dev/null ||
  die 'invalid WSS candidate'
jq -e '[.outbounds[]? | select(.tag=="router-admin" or .tag=="router-ssh")] |
    length==2 and all(.[]; (.settings | has("finalRules") | not))' "$OUT/new-xray.json" >/dev/null ||
  die 'old core candidate still has finalRules'
jq -n --slurpfile old "$ST/original-xray.json" --slurpfile new "$OUT/new-xray.json" '
  def links($x): [$x.outbounds[] | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn") |
    {tag,id:.settings.id,encryption:.settings.encryption,flow:.settings.flow,reverse:.settings.reverse}] | sort_by(.tag);
  (links($old[0])|length)==2 and links($old[0])==links($new[0])
' | grep -qx true || die 'UUID or reverse tags differ'
X="$(uci -q get owrtremote.main.xray_bin 2>/dev/null || true)"
[ -n "$X" ] || X=/usr/bin/xray
[ -x "$X" ] || die 'Xray executable missing'
"$X" -test -config "$OUT/new-xray.json" >/dev/null 2>"$OUT/xray-test" || die 'Xray rejected candidate'
cp -p "$CANDIDATE" "$OUT/agent"
cp -p "$OUT/uci/owrtremote" "$ST/candidate-uci.next"
cp -p "$OUT/agent" "$ST/candidate-agent.next"
cp -p "$OUT/new-xray.json" "$ST/candidate-xray.json.next"
mv -f "$ST/candidate-uci.next" "$ST/candidate-uci"
mv -f "$ST/candidate-agent.next" "$ST/candidate-agent"
mv -f "$ST/candidate-xray.json.next" "$ST/candidate-xray.json"
sha256sum "$ST/candidate-agent" | awk '{print $1}' > "$ST/candidate-agent.sha256"
(cd "$ST" && sha256sum original-uci original-xray.json candidate-uci candidate-xray.json > SHA256SUMS.next && mv -f SHA256SUMS.next SHA256SUMS)
sync
printf 'WSS-REFRESH: UPDATED WSS HOST + CANDIDATE ONLY - live TCP untouched, Xray syntax PASS\n'
