#!/bin/sh
# OpenWrt guarded TCP -> WSS pilot; explicitly targeted WR3000S_2.
# Independent procd watchdog and early boot recovery; NO fleet rollout.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
ST=/etc/router-orchestrator/wss-pilot
UCI=/etc/config/owrtremote
JSON=/etc/xray/owrt-remote-client.json
AGENT=/usr/sbin/owrt-remote
INIT=/etc/init.d/ro-wss-recovery
SELF=/usr/libexec/ro-wss-transaction
LOCK="$ST/transaction-lock"
say() { printf 'WSS-TXN: %s\n' "$*"; }
fail() { say "BLOCKED - $*" >&2; exit 1; }
regular() { [ -f "$1" ] && [ ! -L "$1" ]; }
digest() { sha256sum "$1" | awk '{print $1}'; }
uptime_sec() { awk 'NR==1 {print int($1)}' /proc/uptime; }
require_id() {
  [ "$(uci -q get owrtremote.main.router_id 2>/dev/null || true)" = WR3000S_2 ] || fail 'wrong router; only WR3000S_2'
}
lock() {
  mkdir "$LOCK" 2>/dev/null || fail 'transaction already running or stale lock'
  trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT
}
unlock() { rmdir "$LOCK" 2>/dev/null || true; trap - EXIT; }
verify_stage() {
  [ -d "$ST" ] && [ ! -L "$ST" ] || fail 'staging directory missing/unsafe'
  for name in original-uci original-xray.json candidate-uci candidate-xray.json; do
    regular "$ST/$name" || fail "stage missing $name"
  done
  (cd "$ST" && sha256sum -c SHA256SUMS >/dev/null) || fail 'stage checksum mismatch'
  [ "$(uci -q get owrtremote.main.router_id 2>/dev/null || true)" = WR3000S_2 ] || fail 'router identity mismatch'
}
verify_assets() {
  verify_stage
  for name in original-agent candidate-agent; do regular "$ST/$name" || fail "missing $name"; done
  [ "$(digest "$ST/original-agent")" = "$(cat "$ST/original-agent.sha256")" ] || fail 'agent backup checksum mismatch'
  [ "$(digest "$ST/candidate-agent")" = "$(cat "$ST/candidate-agent.sha256")" ] || fail 'candidate agent checksum mismatch'
  regular "$SELF" && regular "$INIT" || fail 'recovery components missing'
  [ -L /etc/rc.d/S08ro-wss-recovery ] || fail 'early boot recovery not enabled'
}
verify_baseline() {
  verify_stage
  cmp -s "$UCI" "$ST/original-uci" || fail 'live UCI differs from TCP backup'
  cmp -s "$JSON" "$ST/original-xray.json" || fail 'live Xray JSON differs from TCP backup'
  [ "$(uci -q get owrtremote.main.vps_port 2>/dev/null || true)" = 18443 ] || fail 'unexpected original port'
  [ "$(digest "$AGENT")" = "$(cat "$ST/original-agent.sha256")" ] || fail 'installed agent changed'
}
atomic_copy() {
  src="$1"; dst="$2"
  regular "$src" || fail 'source not a regular file'
  regular "$dst" || fail 'destination not a regular file'
  cp -p "$src" "$dst.ro-wss-new" || fail 'copy failed'
  mv -f "$dst.ro-wss-new" "$dst" || fail 'atomic rename failed'
}
restore_files() {
  # Do not require current UCI/JSON match: a partially written candidate is expected.
  verify_assets
  atomic_copy "$ST/original-agent" "$AGENT"
  atomic_copy "$ST/original-xray.json" "$JSON"
  atomic_copy "$ST/original-uci" "$UCI"
  [ "$(digest "$AGENT")" = "$(cat "$ST/original-agent.sha256")" ] || fail 'agent recovery checksum mismatch'
  cmp -s "$UCI" "$ST/original-uci" || fail 'UCI recovery mismatch'
  cmp -s "$JSON" "$ST/original-xray.json" || fail 'Xray recovery mismatch'
  : > "$ST/rolled-back"
  rm -f "$ST/confirmed" "$ST/pending" "$ST/armed" "$ST/ready"
}
rollback() {
  lock
  if [ ! -e "$ST/pending" ]; then say 'no pending WSS switch'; unlock; return; fi
  restore_files
  unlock
  say 'TCP originals restored; restarting router Remote HUB only'
  /etc/init.d/owrt-remote restart || fail 'TCP restart failed; local intervention needed'
}
boot_recover() {
  # Called before the ordinary OpenWrt Remote service starts (S08 < S99).
  [ -e "$ST/pending" ] || { rm -f "$ST/armed"; return 0; }
  [ ! -e "$LOCK" ] || rm -rf "$LOCK"
  lock
  restore_files
  unlock
  say 'unfinished switch reverted before normal boot of TCP service'
}
prepare() {
  [ "$#" -eq 1 ] || fail 'prepare requires downloaded candidate agent file'
  candidate="$1"
  require_id
  [ ! -e "$ST/armed" ] && [ ! -e "$ST/pending" ] || fail 'existing transaction'
  [ ! -e "$INIT" ] && [ ! -e "$SELF" ] || fail 'recovery service already exists'
  verify_stage
  regular "$AGENT" && regular "$JSON" && regular "$UCI" || fail 'unsafe live paths'
  regular "$candidate" || fail 'candidate agent missing'
  [ -x "$candidate" ] || fail 'candidate agent not executable'
  command -v ubus >/dev/null || fail 'procd ubus required'
  [ -x /etc/init.d/owrt-remote ] || fail 'existing service missing'
  [ -r /proc/uptime ] || fail 'monotonic clock unavailable'
  [ "$(df -Pk /overlay | awk 'END{print $4}')" -ge 16384 ] || fail 'overlay reserve insufficient'
  grep -Fq 'OWRT_REMOTE_UCI_CONFIG_DIR' "$candidate" || fail 'candidate agent does not support WSS staging'
  [ "$(id -u)" -eq 0 ] || fail 'root required'
  [ -r /dev/urandom ] || fail 'system randomness required'
  mkdir -p /usr/libexec
  cp -p "$AGENT" "$ST/original-agent"
  cp -p "$candidate" "$ST/candidate-agent"
  digest "$ST/original-agent" > "$ST/original-agent.sha256"
  digest "$ST/candidate-agent" > "$ST/candidate-agent.sha256"
  # These scripts must already have been downloaded and syntax checked in /tmp.
  regular /tmp/ro-wss-transaction.sh || fail 'transaction source missing'
  regular /tmp/ro-wss-recovery.init || fail 'init source missing'
  cp /tmp/ro-wss-transaction.sh "$SELF"
  cp /tmp/ro-wss-recovery.init "$INIT"
  chmod 700 "$SELF" "$INIT"
  "$INIT" enable || fail 'could not enable early-boot recovery'
  [ -L /etc/rc.d/S08ro-wss-recovery ] || fail 'early boot recovery link absent'
  say 'PREPARED: backup + boot recovery installed, TCP still running'
}
apply() {
  require_id
  verify_assets
  [ ! -e "$ST/armed" ] && [ ! -e "$ST/pending" ] || fail 'transaction already active'
  lock
  verify_baseline
  X="$(uci -q get owrtremote.main.xray_bin 2>/dev/null || true)"
  [ -n "$X" ] || X=/usr/bin/xray
  [ -x "$X" ] || fail 'configured Xray unavailable'
  "$X" -test -config "$ST/candidate-xray.json" >/dev/null 2>&1 || fail 'WSS config rejected'
  # Authenticated operator must explicitly confirm after Belt-side probes.
  nonce="$(dd if=/dev/urandom bs=16 count=1 2>/dev/null | hexdump -v -e '16/1 "%02x"')" || fail 'nonce generation failed'
  [ "${#nonce}" -eq 32 ] || fail 'nonce length invalid'
  now="$(uptime_sec)"
  case "$now" in ''|*[!0-9]*) fail 'monotonic clock invalid';; esac
  echo "$((now + 300))" > "$ST/deadline"
  printf '%s\n' "$nonce" > "$ST/nonce"
  rm -f "$ST/ready" "$ST/rolled-back" "$ST/confirmed"
  : > "$ST/armed"
  # No Xray changes until independent procd worker reports readiness.
  /etc/init.d/ro-wss-recovery start || fail 'watchdog registration failed'
  i=0
  while [ "$i" -lt 8 ] && [ ! -f "$ST/ready" ]; do
    sleep 1
    i=$((i+1))
  done
  [ -f "$ST/ready" ] || fail 'watchdog never became ready; NO live files changed'
  [ "$(uptime_sec)" -lt "$(cat "$ST/deadline")" ] || fail 'deadline expired before apply'
  : > "$ST/pending"
  # All three files are restored if any intermediate operation fails.
  atomic_copy "$ST/candidate-agent" "$AGENT"
  atomic_copy "$ST/candidate-xray.json" "$JSON"
  atomic_copy "$ST/candidate-uci" "$UCI"
  unlock
  say 'WSS candidate installed; now restarting only OpenWrt Remote (SSH session may disconnect)'
  say "Confirmation nonce (KEEP PRIVATE): $nonce"
  /etc/init.d/owrt-remote restart || { say 'restart failed; attempting rollback'; rollback; exit 1; }
  say 'Verify LuCI + SSH externally from Belt, then confirm before 300 seconds'
}
watchdog() {
  [ -e "$ST/armed" ] || exit 0
  : > "$ST/ready"
  while [ -e "$ST/armed" ]; do
    deadline="$(cat "$ST/deadline" 2>/dev/null || true)"
    now="$(uptime_sec)"
    case "$deadline:$now" in *[!0-9:]*|:*|*:) rollback; exit 0;; esac
    if [ "$now" -ge "$deadline" ]; then
      [ -e "$ST/pending" ] && rollback
      rm -f "$ST/armed"
      exit 0
    fi
    sleep 1
  done
}
confirm() {
  [ "$#" -eq 1 ] || fail 'confirm requires private one-time nonce'
  require_id
  lock
  [ -e "$ST/pending" ] || fail 'no pending switch'
  [ "$1" = "$(cat "$ST/nonce")" ] || fail 'invalid nonce'
  now="$(uptime_sec)"
  [ "$now" -lt "$(cat "$ST/deadline")" ] || fail 'deadline expired'
  verify_assets
  cmp -s "$UCI" "$ST/candidate-uci" || fail 'UCI mismatch'
  cmp -s "$JSON" "$ST/candidate-xray.json" || fail 'Xray JSON mismatch'
  cmp -s "$AGENT" "$ST/candidate-agent" || fail 'agent mismatch'
  : > "$ST/confirmed"
  rm -f "$ST/pending" "$ST/armed" "$ST/nonce" "$ST/ready"
  unlock
  say 'CONFIRMED: WSS adopted, automatic rollback disarmed'
}
[ "$#" -ge 1 ] || fail 'usage: prepare AGENT | apply | confirm NONCE | rollback | watchdog | boot-recover'
action="$1"; shift
case "$action" in
  prepare) prepare "$@" ;;
  apply) [ "$#" -eq 0 ] || fail 'apply takes no arguments'; apply ;;
  confirm) confirm "$@" ;;
  rollback) [ "$#" -eq 0 ] || fail 'rollback takes no arguments'; rollback ;;
  watchdog) [ "$#" -eq 0 ] || fail 'watchdog takes no arguments'; watchdog ;;
  boot-recover) [ "$#" -eq 0 ] || fail 'boot-recover takes no arguments'; boot_recover ;;
  *) fail 'unknown command' ;;
esac
