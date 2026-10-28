#!/bin/sh
# Experimental, isolated, opt-in pilot transaction for OpenWrt router "main".
# Depends on verified stage + immutable backup from router_bundle_guard.sh.
# No Xray/reverse/service/network restart. No production use until hardware review.
set -eu
export PATH="/bin:/sbin:/usr/bin:/usr/sbin:${PATH:-}"
umask 077

fail() { printf 'PILOT: BLOCKED - %s\n' "$*" >&2; exit 1; }
say() { printf 'PILOT: %s\n' "$*"; }
case "${RO_TEST_ROOT:-/}" in
  /) ROOT= ;;
  /*) [ "${RO_TEST_MODE:-0}" = 1 ] || fail 'test root needs test mode'; ROOT="${RO_TEST_ROOT%/}" ;;
  *) fail 'invalid root' ;;
esac
[ "${RO_TEST_MODE:-0}" != 1 ] || [ -n "$ROOT" ] || fail 'test mode cannot target real root'
# PRODUCTION DISABLED: only synthetic-root tests are permitted in this branch.
# The live enablement is a separately reviewed release after hardware validation.
[ "${RO_TEST_MODE:-0}" = 1 ] || fail 'live transactions disabled: use isolated synthetic-root compatibility tests only'
STATE="$ROOT/etc/router-orchestrator/upgrade"
HOOK="$ROOT/etc/init.d/ro-pilot-recovery"
BOOT="$ROOT/etc/rc.d/S08ro-pilot-recovery"
FILES='usr/sbin/owrt-remote
usr/lib/owrt-remote/wan.sh
usr/lib/owrt-remote/logs.sh
etc/init.d/owrt-remote-logs
etc/init.d/owrt-remote
etc/hotplug.d/iface/99-owrt-remote
www/cgi-bin/owrt-remote
usr/share/luci/menu.d/luci-app-owrt-remote.json
usr/share/rpcd/acl.d/luci-app-owrt-remote.json
www/luci-static/resources/view/owrt_remote.js'

at() { printf '%s/%s' "$ROOT" "$1"; }
sha() { sha256sum "$1" | awk '{print $1}'; }
hex64() { case "$1" in ''|*[!0-9a-f]*) return 1 ;; esac; [ "${#1}" -eq 64 ]; }
present() { [ -f "$1" ] && [ ! -L "$1" ]; }

secure_nonce() {
  # Exactly 16 unpredictable bytes, hexadecimal-encoded. Never use timestamps,
  # shell PIDs or weak PRNG values for confirmation tokens.
  [ -r /dev/urandom ] || return 1
  nonce_hex=''
  if command -v dd >/dev/null 2>&1 && command -v hexdump >/dev/null 2>&1; then
    nonce_hex="$(dd if=/dev/urandom bs=16 count=1 2>/dev/null | hexdump -v -e '16/1 "%02x"' 2>/dev/null)" || nonce_hex=''
  fi
  if [ "${#nonce_hex}" -ne 32 ] && command -v od >/dev/null 2>&1; then
    nonce_hex="$(od -An -tx1 -N 16 /dev/urandom 2>/dev/null | tr -d ' \n')" || nonce_hex=''
  fi
  if [ "${#nonce_hex}" -ne 32 ] && command -v openssl >/dev/null 2>&1; then
    nonce_hex="$(openssl rand -hex 16 2>/dev/null)" || nonce_hex=''
  fi
  case "$nonce_hex" in ''|*[!0-9a-f]*) return 1 ;; esac
  [ "${#nonce_hex}" -eq 32 ] || return 1
  printf '%s\n' "$nonce_hex"
}

# Single-operation directory lock. Boot recovery clears orphaned locks from
# a prior boot; watchdog can reclaim a lock only when the owner is gone.
unlock() { rm -f "$STATE/lock/pid"; rmdir "$STATE/lock" 2>/dev/null || true; }
lock() {
  i=0
  while [ "$i" -lt 5 ]; do
    if mkdir "$STATE/lock" 2>/dev/null; then
      printf '%s\n' "$$" > "$STATE/lock/pid"
      trap 'unlock' EXIT HUP INT TERM
      return
    fi
    if [ -r "$STATE/lock/pid" ]; then
      oldpid="$(cat "$STATE/lock/pid")"
      case "$oldpid" in ''|*[!0-9]*) fail 'untrusted lock PID' ;; esac
      if ! kill -0 "$oldpid" 2>/dev/null; then
        # Recheck immediately before stale-lock removal.
        [ "$(cat "$STATE/lock/pid")" = "$oldpid" ] || fail 'lock changed'
        rm -f "$STATE/lock/pid"
        rmdir "$STATE/lock" 2>/dev/null || true
      fi
    fi
    i=$((i+1))
    sleep 1
  done
  fail 'another update operation is active'
}

identity() {
  if [ "${RO_TEST_MODE:-0}" = 1 ]; then
    [ "${RO_TEST_ROUTER_ID:-}" = main ] || fail 'wrong test router'
    [ "${RO_TEST_TRANSPORT:-}" = tcp ] || fail 'not tcp'
    [ "${RO_TEST_PORT:-}" = 18443 ] || fail 'wrong port'
    return
  fi
  command -v uci >/dev/null 2>&1 || fail 'uci missing'
  command -v jq >/dev/null 2>&1 || fail 'jq missing'
  [ "$(uci -q get owrtremote.main.router_id)" = main ] || fail 'pilot limited to main'
  transport="$(uci -q get owrtremote.main.vps_transport || true)"
  [ "${transport:-tcp}" = tcp ] || fail 'expected TCP'
  [ "$(uci -q get owrtremote.main.vps_port)" = 18443 ] || fail 'expected TCP 18443'
  cfg="$(uci -q get owrtremote.main.xray_config || true)"
  cfg="${cfg:-/etc/xray/owrt-remote-client.json}"
  [ "$cfg" = /etc/xray/owrt-remote-client.json ] || fail 'nondefault config needs separate review'
  jq -e '[.outbounds[]? | select(.tag=="vps-interconn" or .tag=="vps-ssh-interconn") | .streamSettings] as $l | ($l|length==2) and all($l[]; ((.network//"tcp")=="tcp") and ((.security//"none")=="none"))' "$cfg" >/dev/null || fail 'reverse changed'
}

critical() {
  # Do NOT restore these private configs in a blind rollback; this updater
  # never writes them. If they changed concurrently, stop for manual review.
  [ -s "$STATE/backup/critical.sha256" ] || fail 'critical digest snapshot missing'
  read -r expected_uci < "$STATE/backup/critical.sha256"
  expected_xray="$(sed -n '2p' "$STATE/backup/critical.sha256")"
  hex64 "$expected_uci" || fail 'bad UCI backup hash'
  hex64 "$expected_xray" || fail 'bad Xray backup hash'
  [ "$(sha "$STATE/backup/files/etc/config/owrtremote")" = "$expected_uci" ] || fail 'UCI recovery copy corrupted'
  [ "$(sha "$STATE/backup/files/etc/xray/owrt-remote-client.json")" = "$expected_xray" ] || fail 'Xray recovery copy corrupted'
  [ "$(sha "$(at etc/config/owrtremote)")" = "$expected_uci" ] || fail 'UCI changed during pilot'
  [ "$(sha "$(at etc/xray/owrt-remote-client.json)")" = "$expected_xray" ] || fail 'Xray JSON changed during pilot'
}

verify_backup() {
  present "$STATE/backup/present.txt" || fail 'backup inventory missing'
  [ "$(wc -l < "$STATE/backup/present.txt")" -eq 10 ] || fail 'invalid backup inventory'
  present "$STATE/backup/original.sha256" || fail 'original digest inventory missing'
  [ "$(wc -l < "$STATE/backup/original.sha256")" -eq 10 ] || fail 'original digest inventory invalid'
  for rel in $FILES; do
    record="$(awk -v name="$rel" '$2==name{print $0}' "$STATE/backup/present.txt" || true)"
    seal="$(awk -v name="$rel" '$2==name{print $1}' "$STATE/backup/original.sha256" || true)"
    case "$record" in
      "1 $rel")
        present "$STATE/backup/files/$rel" || fail "backup missing $rel"
        hex64 "$seal" || fail "original digest missing for $rel"
        [ "$(sha "$STATE/backup/files/$rel")" = "$seal" ] || fail "backup corrupted for $rel"
        ;;
      "0 $rel")
        [ "$seal" = '-' ] || fail "absent-file seal invalid for $rel"
        [ ! -e "$STATE/backup/files/$rel" ] || fail "backup conflict $rel"
        ;;
      *) fail "backup inventory invalid for $rel" ;;
    esac
  done
  critical
}

verify_originals() {
  # Between snapshot and apply no original client file may change unnoticed.
  for rel in $FILES; do
    record="$(awk -v name="$rel" '$2==name{print $0}' "$STATE/backup/present.txt")"
    live="$(at "$rel")"
    if [ "$record" = "1 $rel" ]; then
      present "$live" || fail "original client file disappeared: $rel"
      cmp -s "$live" "$STATE/backup/files/$rel" || fail "original client changed after snapshot: $rel"
    else
      [ ! -e "$live" ] && [ ! -L "$live" ] || fail "previously absent path now exists: $rel"
    fi
  done
}

verify_resources() {
  if [ "${RO_TEST_MODE:-0}" != 1 ]; then
    [ "$(id -u)" = 0 ] || fail 'apply requires root'
    available="$(awk '$1=="MemAvailable:"{print $2}' /proc/meminfo)"
    case "$available" in ''|*[!0-9]*) fail 'cannot read MemAvailable' ;; esac
    [ "$available" -ge 20480 ] || fail 'less than 20 MiB available RAM'
  fi
  free_kb="$(df -Pk "$(at etc)" | awk 'END{print $4}')"
  case "$free_kb" in ''|*[!0-9]*) fail 'cannot read overlay free space' ;; esac
  [ "$free_kb" -ge 16384 ] || fail 'less than 16 MiB persistent storage reserve'
}

verify_candidate() {
  present "$STATE/stage/archive.sha256" || fail 'staged archive hash missing'
  pinned="$(cat "$STATE/stage/archive.sha256")"
  hex64 "$pinned" || fail 'bad staged SHA-256'
  [ "$pinned" = "$1" ] || fail 'requested SHA-256 differs from staged bundle'
  [ "$(sha "$STATE/stage/candidate.tar")" = "$pinned" ] || fail 'staged tar changed'
  manifest="$STATE/stage/unpacked/manifest.txt"
  present "$manifest" || fail 'staged manifest missing'
  [ "$(wc -l < "$manifest")" -eq 10 ] || fail 'manifest length wrong'
  for rel in $FILES; do
    data="$STATE/stage/unpacked/payload/$rel"
    present "$data" || fail "candidate missing $rel"
    line="$(awk -v n="$rel" '$3==n{print $0}' "$manifest")"
    [ "$(printf '%s\n' "$line" | wc -l)" -eq 1 ] || fail 'duplicate manifest entry'
    set -- $line
    [ "$#" -eq 3 ] && [ "$3" = "$rel" ] && hex64 "$1" || fail "manifest invalid $rel"
    case "$2" in 0644|0755) ;; *) fail "invalid candidate mode $rel" ;; esac
    [ "$(sha "$data")" = "$1" ] || fail "candidate hash mismatch $rel"
  done
}

# The installer itself remains on overlay and is invoked by an early-boot
# service; it can recover without SSH, Docker or the HUB being online.
install_boot_recovery() {
  [ ! -e "$HOOK" ] && [ ! -L "$HOOK" ] || fail 'recovery init name already exists'
  [ ! -e "$BOOT" ] && [ ! -L "$BOOT" ] || fail 'recovery boot link already exists'
  present "$0" || fail 'run from a regular checked-out script'
  cp "$0" "$STATE/transaction.sh.new"
  chmod 700 "$STATE/transaction.sh.new"
  mv "$STATE/transaction.sh.new" "$STATE/transaction.sh"
  mkdir -p "$(dirname "$HOOK")" "$(dirname "$BOOT")"
  cat > "$HOOK.new" <<'EOF'
#!/bin/sh /etc/rc.common
START=08
STOP=99
start() {
  [ -f /etc/router-orchestrator/upgrade/pending ] || return 0
  /bin/sh /etc/router-orchestrator/upgrade/transaction.sh boot-recover
}
EOF
  chmod 700 "$HOOK.new"
  mv "$HOOK.new" "$HOOK"
  ln -s '../init.d/ro-pilot-recovery' "$BOOT"
}

# Restore all ten original paths, including original absent files. A partially
# failed restore leaves "pending", so a reboot will retry; never auto-confirm.
restore() {
  [ -f "$STATE/pending" ] || fail 'no pending update to restore'
  verify_backup
  for rel in $FILES; do
    dst="$(at "$rel")"
    record="$(awk -v name="$rel" '$2==name{print $0}' "$STATE/backup/present.txt")"
    if [ "$record" = "0 $rel" ]; then
      rm -f "$dst" || fail "could not remove new $rel"
    else
      backup="$STATE/backup/files/$rel"
      mkdir -p "$(dirname "$dst")"
      cp -p "$backup" "$dst.ro-restore-$$" || fail "restore failed for $rel"
      mv -f "$dst.ro-restore-$$" "$dst" || fail "restore rename failed for $rel"
    fi
  done
  : > "$STATE/rolled-back"
  rm -f "$STATE/pending" "$STATE/armed"
  # Deliberately do not restart Xray, WAN, procd or original agents.
  say 'ROLLED BACK: on-disk original files restored; check existing LuCI/SSH and service state.'
}

apply() {
  [ "$#" -eq 1 ] || fail 'usage: apply EXACT_SHA256'
  hex64 "$1" || fail 'expected SHA-256 required'
  [ ! -e "$STATE/pending" ] || fail 'pending update exists'
  [ ! -e "$STATE/armed" ] || fail 'watchdog already armed'
  [ ! -e "$STATE/transaction.sh" ] || fail 'transaction already installed; investigate'
  identity
  verify_resources
  verify_backup
  verify_originals
  verify_candidate "$1"
  lock
  # Check preexisting state after obtaining the exclusive lock.
  [ ! -e "$STATE/pending" ] && [ ! -e "$STATE/armed" ] || fail 'update already started'
  # Validate crypto availability before adding any recovery hooks or files.
  # Missing od alone must not block devices with BusyBox hexdump.
  nonce="$(secure_nonce)" || fail 'no supported secure nonce generator (hexdump, od or openssl)'
  install_boot_recovery
  printf '%s\n' "$nonce" > "$STATE/confirm-nonce"
  duration="${RO_PILOT_TIMEOUT:-300}"
  case "$duration" in ''|*[!0-9]*) fail 'invalid timeout' ;; esac
  if [ "${RO_TEST_MODE:-0}" = 1 ]; then
    [ "$duration" -ge 2 ] && [ "$duration" -le 30 ] || fail 'test watchdog timeout invalid'
  else
    [ "$duration" -ge 180 ] && [ "$duration" -le 900 ] || fail 'production timeout must be 180..900 seconds'
  fi
  printf '%s\n' "$duration" > "$STATE/duration"
  : > "$STATE/armed"
  # Start watchdog independently of the SSH session. Arming happens BEFORE
  # the first mutation. Persisted boot recovery handles power loss.
  if [ "${RO_TEST_MODE:-0}" = 1 ]; then
    RO_TEST_ROOT="$ROOT" RO_TEST_MODE=1 RO_TEST_ROUTER_ID=main RO_TEST_TRANSPORT=tcp RO_TEST_PORT=18443 RO_PILOT_TIMEOUT="$duration" \
      nohup sh "$STATE/transaction.sh" watchdog > "$STATE/watchdog.log" 2>&1 < /dev/null &
  else
    nohup sh "$STATE/transaction.sh" watchdog > "$STATE/watchdog.log" 2>&1 < /dev/null &
  fi
  watcher="$!"
  i=0
  while [ "$i" -lt 5 ] && [ ! -e "$STATE/watchdog-ready" ]; do
    kill -0 "$watcher" 2>/dev/null || fail 'watchdog did not start'
    sleep 1
    i=$((i+1))
  done
  [ -e "$STATE/watchdog-ready" ] || fail 'watchdog not ready: no live file changed'
  # The persistent marker is created before live file changes.
  : > "$STATE/pending"
  for rel in $FILES; do
    source="$STATE/stage/unpacked/payload/$rel"
    dest="$(at "$rel")"
    mode="$(awk -v n="$rel" '$3==n{print $2}' "$STATE/stage/unpacked/manifest.txt")"
    mkdir -p "$(dirname "$dest")"
    cp "$source" "$dest.ro-new-$$" || { restore; fail "copy failed $rel"; }
    chmod "$mode" "$dest.ro-new-$$" || { restore; fail "chmod failed $rel"; }
    mv -f "$dest.ro-new-$$" "$dest" || { restore; fail "rename failed $rel"; }
  done
  critical || { restore; fail 'critical configuration unexpectedly changed'; }
  say "APPLIED ON DISK. NO SERVICES OR XRAY RESTARTED. Confirm from Belt after LuCI+SSH probes."
  say "ONE-TIME CONFIRM NONCE: $nonce"
  say "TIMEOUT: $duration seconds; missing confirmation triggers rollback."
}

watchdog() {
  [ -f "$STATE/armed" ] || exit 0
  : > "$STATE/watchdog-ready"
  duration="$(cat "$STATE/duration")"
  case "$duration" in ''|*[!0-9]*) fail 'invalid watchdog duration' ;; esac
  # Countdown uses bounded sleeps, independent of wall-clock/NTP adjustments.
  i=0
  while [ "$i" -lt "$duration" ]; do
    [ -f "$STATE/armed" ] || exit 0
    sleep 1
    i=$((i+1))
  done
  [ -f "$STATE/pending" ] || exit 0
  lock
  [ -f "$STATE/pending" ] && restore
}

confirm() {
  [ "$#" -eq 1 ] || fail 'usage: confirm ONE_TIME_NONCE'
  [ -f "$STATE/pending" ] || fail 'no pending update'
  lock
  [ -f "$STATE/pending" ] || fail 'no pending update'
  [ -r "$STATE/confirm-nonce" ] || fail 'nonce missing'
  [ "$1" = "$(cat "$STATE/confirm-nonce")" ] || fail 'invalid nonce'
  identity
  critical
  for rel in $FILES; do
    expected="$(awk -v n="$rel" '$3==n{print $1}' "$STATE/stage/unpacked/manifest.txt")"
    hex64 "$expected" || fail 'manifest changed'
    [ "$(sha "$(at "$rel")")" = "$expected" ] || fail "installed file mismatch $rel"
  done
  rm -f "$STATE/pending" "$STATE/armed" "$STATE/confirm-nonce"
  : > "$STATE/confirmed"
  say 'CONFIRMED: on-disk files verified. Keep recovery snapshot for manual inspection.'
}

boot_recover() {
  # Called by the OpenWrt boot hook before owrt-remote (START=99).
  [ -f "$STATE/pending" ] || exit 0
  # On a fresh boot all prior owner PIDs are invalid; remove any orphaned lock.
  rm -f "$STATE/lock/pid"
  rmdir "$STATE/lock" 2>/dev/null || true
  lock
  [ -f "$STATE/pending" ] && restore
}

case "${1:-}" in
  apply) shift; apply "$@" ;;
  confirm) shift; confirm "$@" ;;
  rollback) shift; [ "$#" -eq 0 ] || fail 'rollback accepts no args'; lock; restore ;;
  watchdog) shift; [ "$#" -eq 0 ] || fail 'watchdog accepts no args'; watchdog ;;
  boot-recover) shift; [ "$#" -eq 0 ] || fail 'boot-recover accepts no args'; boot_recover ;;
  *) fail 'usage: apply SHA256 | confirm NONCE | rollback | boot-recover (internal)' ;;
esac
