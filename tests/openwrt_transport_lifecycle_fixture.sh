#!/bin/sh
# Exercise the installed agent on a disposable VM attached to a fixture HUB.
set -eu
umask 077

fail() { printf '%s\n' "OpenWrt transport fixture: $*" >&2; exit 1; }
[ "${1:-}" = --disposable-openwrt ] || fail 'explicit disposable fixture required'
[ "$(cat /run/ro-transport-fixture 2>/dev/null)" = 'router-orchestrator-virtual-fixture' ] ||
	fail 'virtual fixture marker missing'
slot="${2:-}"
case "$slot" in 0|1|2) ;; *) fail 'expected assigned revision slot 0, 1 or 2' ;; esac
case "${3:-}" in wss|xhttp|grpc|reality-tcp) ;; *) fail 'expected transport name' ;; esac
[ -s /etc/router-orchestrator/transport/identity ] ||
	[ -n "$(uci -q get owrtremote.main.transport_identity)" ] || fail 'fixture identity missing'
[ "$(uci -q get owrtremote.main.transport_fallback_enabled || printf '0')" = 0 ] ||
	fail 'disable autonomous fallback during the deterministic activation scenario'
uci -q get owrtremote.main.hub_url | grep -Eq '^https://[A-Za-z0-9.-]+[.]test(:[0-9]{1,5})?/?$' ||
	fail 'expected an isolated .test HTTPS HUB'

agent=/usr/sbin/owrt-remote
active=/etc/router-orchestrator/transport/active.json
xray_config="$(uci -q get owrtremote.main.xray_config || printf '/etc/xray/owrt-remote-client.json')"

transport_network() {
	jsonfilter -i "$xray_config" -e '@.outbounds[2].streamSettings.network'
}

primary_pid() {
	for cmdline in /proc/[0-9]*/cmdline; do
		[ -r "$cmdline" ] || continue
		if tr '\000' '\n' <"$cmdline" 2>/dev/null | grep -Fxq -- "$xray_config"; then
			pid="${cmdline%/cmdline}"
			printf '%s\n' "${pid##*/}"
			return 0
		fi
	done
	return 1
}

case "${3:-}" in wss) expected=ws ;; reality-tcp) expected=tcp ;; *) expected="$3" ;; esac
"$agent" heartbeat >/dev/null
"$agent" transport-stage enrollment
"$agent" transport-probe >/dev/null
before="$(primary_pid)" || fail 'enrollment Xray is missing'
"$agent" transport-stage "$slot"
"$agent" transport-probe >/dev/null
[ "$(primary_pid)" = "$before" ] || fail 'stage or probe replaced enrollment'
"$agent" transport-activate
[ "$(primary_pid)" != "$before" ] || fail 'activation did not replace the owned Xray'
[ "$(transport_network)" = "$expected" ] || fail 'running configuration selected the wrong transport'
if [ "$3" = reality-tcp ]; then
	[ "$(jsonfilter -i "$xray_config" -e '@.outbounds[2].streamSettings.security')" = reality ] ||
		fail 'REALITY security missing'
fi
committed="$(jsonfilter -i "$active" -e '@.remarks')"
case "$committed" in ro-profile:*) ;; *) fail 'profile was not persisted' ;; esac
"$agent" transport-probe >/dev/null
"$agent" transport-rollback
"$agent" transport-stage enrollment
"$agent" transport-probe >/dev/null
case "$(jsonfilter -i "$active" -e '@.remarks')" in
	'OpenWrt Remote client for '*) ;;
	*) fail 'rollback did not persist enrollment' ;;
esac
printf '%s\n' "OpenWrt transport lifecycle passed: $3"
