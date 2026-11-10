#!/bin/sh
# OpenWrt Router Orchestrator guarded transport migration.
# Generic per-device TCP -> WSS(443) with rollback to each device's original port.
# BusyBox ash; persistent recovery via procd/early boot, no external tooling.
set -eu
umask 077
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
ST=/etc/router-orchestrator/wss-pilot
UCI=/etc/config/owrtremote
JSON=/etc/xray/owrt-remote-client.json
AGENT=/usr/sbin/owrt-remote
TX=/usr/libexec/ro-wss-transaction
INIT=/etc/init.d/ro-wss-recovery
LOCK="$ST/transaction-lock"
log() { printf 'WSS-TXN: %s\n' "$*" >&2; }
die() { log "BLOCKED: $*"; exit 1; }
file() { [ -f "$1" ] && [ ! -L "$1" ]; }
hash() { sha256sum "$1" | awk '{print $1}'; }
uptime() { awk 'NR==1 {printf "%d\n", $1}' /proc/uptime; }
is_uint() { case "$1" in ''|*[!0-9]*) return 1;; esac; return 0; }
safe_id() { case "$1" in ''|*[!a-zA-Z0-9_.-]*) return 1;; esac; }
# Handles interrupted runs. Boot-time recovery always discards a previous boot's lock.
lock() {
  n=0
  while ! mkdir "$LOCK" 2>/dev/null; do
    [ -f "$LOCK/pid" ] || { [ "$n" -ge 10 ] && die 'lock busy'; sleep 1; n=$((n+1)); continue; }
    owner="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if is_uint "$owner" && ! kill -0 "$owner" 2>/dev/null; then
      rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null || true
      continue
    fi
    [ "$n" -lt 15 ] || die 'lock held by another process'
    sleep 1; n=$((n+1))
  done
  printf '%s\n' "$$" > "$LOCK/pid"
  trap 'rm -f "$LOCK/pid"; rmdir "$LOCK" 2>/dev/null || true' EXIT
}
unlock() {
  rm -f "$LOCK/pid"
  rmdir "$LOCK" 2>/dev/null || true
  trap - EXIT
}
check_stage() {
  [ -d "$ST" ] && [ ! -L "$ST" ] || die 'staging missing'
  for name in original-uci original-xray.json candidate-uci candidate-xray.json SHA256SUMS; do
    file "$ST/$name" || die "stage missing $name"
  done
  (cd "$ST" && sha256sum -c SHA256SUMS >/dev/null) || die 'staged backup/candidate checksum mismatch'
  command -v jq >/dev/null 2>&1 || die 'jq required for credential parity'
  # Preserve BOTH VLESS identities and reverse tags. Only their transport is migrated.
  original_links="$(jq -c '[.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn") |
    {tag, id:.settings.id, encryption:.settings.encryption, flow:.settings.flow, reverse:.settings.reverse}] |
    sort_by(.tag)' "$ST/original-xray.json")" || die 'invalid original JSON'
  candidate_links="$(jq -c '[.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn") |
    {tag, id:.settings.id, encryption:.settings.encryption, flow:.settings.flow, reverse:.settings.reverse}] |
    sort_by(.tag)' "$ST/candidate-xray.json")" || die 'invalid candidate JSON'
  [ "$original_links" = "$candidate_links" ] || die 'reverse credentials or tags changed'
  jq -e '[.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn")] as $v |
    ($v | length)==2 and
    all($v[]; .streamSettings.network=="ws" and .streamSettings.security=="tls" and
      .streamSettings.tlsSettings.allowInsecure==false and .settings.port==443)' \
      "$ST/candidate-xray.json" >/dev/null || die 'WSS transport candidate invalid'
}
check_identity() {
  file "$ST/router-id" || die 'staged router-id missing'
  expected="$(cat "$ST/router-id")"
  safe_id "$expected" || die 'invalid staged ID'
  actual="$(uci -q get owrtremote.main.router_id 2>/dev/null || true)"
  [ "$actual" = "$expected" ] || die 'router identity mismatch'
}
check_prepared() {
  check_stage
  for name in original-agent candidate-agent original-agent.sha256 candidate-agent.sha256 router-id original-port; do
    file "$ST/$name" || die "missing protected $name"
  done
  [ "$(hash "$ST/original-agent")" = "$(cat "$ST/original-agent.sha256")" ] || die 'agent backup damaged'
  [ "$(hash "$ST/candidate-agent")" = "$(cat "$ST/candidate-agent.sha256")" ] || die 'candidate agent damaged'
  file "$TX" && file "$INIT" || die 'recovery program missing'
  [ -L /etc/rc.d/S08ro-wss-recovery ] || die 'early boot recovery not enabled'
}
copy_atomic() {
  from="$1"; dest="$2"
  file "$from" || die "invalid source: $from"
  file "$dest" || die "unsafe destination: $dest"
  cp -p "$from" "$dest.ro-wss-tmp" || die "cannot write $dest"
  mv -f "$dest.ro-wss-tmp" "$dest" || die "cannot replace $dest"
}
recover_locked() {
  # NEVER check current identity here: an interrupted UCI write is expected.
  # Revalidate immutable snapshots independently of candidate state.
  file "$ST/original-uci" && file "$ST/original-xray.json" &&
    file "$ST/original-agent" || die 'recovery originals missing'
  (cd "$ST" && sha256sum -c SHA256SUMS >/dev/null) ||
    die 'recovery checksum verification failed'
  [ "$(hash "$ST/original-agent")" = "$(cat "$ST/original-agent.sha256")" ] ||
    die 'recovery agent checksum failed'
  copy_atomic "$ST/original-agent" "$AGENT"
  copy_atomic "$ST/original-uci" "$UCI"
  copy_atomic "$ST/original-xray.json" "$JSON"
  cmp -s "$ST/original-uci" "$UCI" || die 'restored UCI differs'
  cmp -s "$ST/original-xray.json" "$JSON" || die 'restored Xray differs'
  cmp -s "$ST/original-agent" "$AGENT" || die 'restored agent differs'
  : > "$ST/rolled-back"
  rm -f "$ST/pending" "$ST/armed" "$ST/ready"
}
boot_recover() {
  [ -e "$ST/pending" ] || { rm -f "$ST/armed" "$ST/ready"; return 0; }
  # A lock from the preceding boot is never valid.
  rm -f "$LOCK/pid"
  rmdir "$LOCK" 2>/dev/null || true
  lock
  recover_locked
  unlock
  log 'recovered TCP originals before OpenWrt Remote starts'
}
rollback() {
  [ -e "$ST/pending" ] || { log 'nothing pending'; return 0; }
  lock
  [ -e "$ST/pending" ] || { unlock; return 0; }
  recover_locked
  unlock
  log 'TCP originals restored; restarting owrt-remote'
  /etc/init.d/owrt-remote restart || die 'TCP restart failed'
}
watchdog() {
  [ -f "$ST/armed" ] || exit 0
  : > "$ST/ready"
  while [ -f "$ST/armed" ]; do
    deadline="$(cat "$ST/deadline" 2>/dev/null || true)"
    now="$(uptime)"
    if ! is_uint "$deadline" || ! is_uint "$now"; then
      [ ! -e "$ST/pending" ] || rollback
      rm -f "$ST/armed"
      exit 1
    fi
    if [ "$now" -ge "$deadline" ]; then
      if [ -e "$ST/pending" ]; then rollback; fi
      rm -f "$ST/armed"
      exit 0
    fi
    sleep 2
  done
}
prepare() {
  [ "$#" -eq 1 ] || die 'prepare needs downloaded new agent'
  candidate="$1"
  [ "$(id -u)" -eq 0 ] || die 'root required'
  check_stage
  [ ! -e "$ST/pending" ] && [ ! -e "$ST/armed" ] || die 'existing transaction'
  for p in "$UCI" "$JSON" "$AGENT" "$candidate" /tmp/ro-wss-transaction.sh /tmp/ro-wss-recovery.init; do
    file "$p" || die "missing regular input $p"
  done
  for command in uci sha256sum cmp awk cp mv mkdir ubus; do
    command -v "$command" >/dev/null 2>&1 || die "missing $command"
  done
  [ -x /etc/init.d/owrt-remote ] || die 'OpenWrt Remote service missing'
  [ -x "$candidate" ] || die 'candidate agent is not executable'
  [ -r /proc/uptime ] || die 'no monotonic clock'
  available="$(df -Pk /overlay | awk 'END {print $4}')"
  is_uint "$available" && [ "$available" -ge 16384 ] || die 'overlay storage below 16MB'
  [ ! -e "$TX" ] && [ ! -e "$INIT" ] || die 'recovery components already installed; manual review'
  cmp -s "$UCI" "$ST/original-uci" || die 'UCI changed since stage'
  cmp -s "$JSON" "$ST/original-xray.json" || die 'Xray changed since stage'
  id="$(uci -q get owrtremote.main.router_id 2>/dev/null || true)"
  safe_id "$id" || die 'router identity missing'
  port="$(uci -q get owrtremote.main.vps_port 2>/dev/null || true)"
  is_uint "$port" && [ "$port" -ge 1 ] && [ "$port" -le 65535 ] ||
    die 'original port missing/invalid'
  current="$(uci -q get owrtremote.main.vps_transport 2>/dev/null || true)"
  case "$current" in ''|tcp) ;; *) die 'original transport not TCP' ;; esac
  grep -Fq 'OWRT_REMOTE_UCI_CONFIG_DIR' "$candidate" ||
    die 'candidate agent lacks shadow-UCI support'
  printf '%s\n' "$id" > "$ST/router-id"
  printf '%s\n' "$port" > "$ST/original-port"
  cp -p "$AGENT" "$ST/original-agent"
  cp -p "$candidate" "$ST/candidate-agent"
  hash "$ST/original-agent" > "$ST/original-agent.sha256"
  hash "$ST/candidate-agent" > "$ST/candidate-agent.sha256"
  mkdir -p /usr/libexec
  cp /tmp/ro-wss-transaction.sh "$TX"
  cp /tmp/ro-wss-recovery.init "$INIT"
  chmod 700 "$TX" "$INIT"
  "$INIT" enable || die 'could not enable early-boot recovery'
  [ -L /etc/rc.d/S08ro-wss-recovery ] || die 'boot recovery link missing'
  log "PREPARED $id TCP:$port; no live connection changed"
}
apply() {
  check_prepared
  check_identity
  [ ! -e "$ST/armed" ] && [ ! -e "$ST/pending" ] || die 'already switching'
  lock
  cmp -s "$UCI" "$ST/original-uci" || die 'live UCI differs from original'
  cmp -s "$JSON" "$ST/original-xray.json" || die 'live Xray differs from original'
  cmp -s "$AGENT" "$ST/original-agent" || die 'live agent differs from original'
  X="$(uci -q get owrtremote.main.xray_bin 2>/dev/null || true)"
  [ -n "$X" ] || X=/usr/bin/xray
  [ -x "$X" ] || die 'Xray executable missing'
  "$X" -test -config "$ST/candidate-xray.json" >/dev/null 2>&1 ||
    die 'candidate Xray syntax invalid'
  now="$(uptime)"
  is_uint "$now" || die 'clock unavailable'
  printf '%s\n' "$((now+300))" > "$ST/deadline"
  rm -f "$ST/ready" "$ST/rolled-back" "$ST/confirmed"
  : > "$ST/armed"
  /etc/init.d/ro-wss-recovery start || die 'watchdog could not start'
  i=0
  while [ ! -f "$ST/ready" ] && [ "$i" -lt 12 ]; do
    sleep 1
    i=$((i+1))
  done
  if [ ! -f "$ST/ready" ]; then
    rm -f "$ST/armed"
    die 'watchdog did not report readiness; no live files modified'
  fi
  # Persistent marker BEFORE any live mutation: reboot recovers partial installs.
  : > "$ST/pending"
  copy_atomic "$ST/candidate-agent" "$AGENT"
  copy_atomic "$ST/candidate-uci" "$UCI"
  copy_atomic "$ST/candidate-xray.json" "$JSON"
  unlock
  log 'WSS installed; restarting owrt-remote only. Automatic rollback in <= 300 seconds.'
  /etc/init.d/owrt-remote restart || {
    log 'restart failed; restoring TCP'
    rollback
    exit 1
  }
  log 'Check SSH and LuCI from Belt; confirm explicitly to keep WSS'
}
confirm() {
  [ "$#" -eq 1 ] || die 'confirm EXPECTED_ROUTER_ID'
  check_prepared
  check_identity
  [ "$1" = "$(cat "$ST/router-id")" ] || die 'confirmation ID mismatch'
  lock
  [ -e "$ST/pending" ] || die 'no pending WSS migration'
  now="$(uptime)"
  is_uint "$now" || die 'clock invalid'
  [ "$now" -lt "$(cat "$ST/deadline")" ] || die 'deadline expired'
  cmp -s "$UCI" "$ST/candidate-uci" || die 'UCI mismatch after apply'
  cmp -s "$JSON" "$ST/candidate-xray.json" || die 'Xray mismatch after apply'
  cmp -s "$AGENT" "$ST/candidate-agent" || die 'agent mismatch after apply'
  : > "$ST/confirmed"
  rm -f "$ST/pending" "$ST/armed" "$ST/ready"
  unlock
  /etc/init.d/ro-wss-recovery stop >/dev/null 2>&1 || true
  log 'CONFIRMED: WSS kept on 443; rollback disarmed'
}
[ "$#" -ge 1 ] || die 'prepare AGENT | apply | confirm ID | rollback | watchdog | boot-recover | status'
action="$1"; shift
case "$action" in
  prepare) prepare "$@" ;;
  apply) [ "$#" -eq 0 ] || die 'apply has no args'; apply ;;
  confirm) confirm "$@" ;;
  rollback) [ "$#" -eq 0 ] || die 'rollback has no args'; rollback ;;
  watchdog) [ "$#" -eq 0 ] || die 'watchdog has no args'; watchdog ;;
  boot-recover) [ "$#" -eq 0 ] || die 'boot-recover has no args'; boot_recover ;;
  status)
    if [ -f "$ST/pending" ]; then log "PENDING until $(cat "$ST/deadline") uptime seconds"
    elif [ -f "$ST/confirmed" ]; then log 'CONFIRMED WSS'
    elif [ -f "$ST/rolled-back" ]; then log 'ROLLED BACK TCP'
    else log 'NO ACTIVE TRANSACTION'; fi ;;
  *) die 'unknown action' ;;
esac
