#!/usr/bin/env bash
# Opt-in read-only adapter ONLY. Never runs systemd HUB/Xray restart/cutover.
set -Eeuo pipefail
umask 077

fail() { printf '[BLOCKED] %s\n' "$1" >&2; exit 2; }
[[ "$#" -ge 2 ]] || fail "Explicit --install-readonly and commit SHA required"
[[ "$1" == "--install-readonly" ]] ||
  fail "Explicit --install-readonly flag required"
EXPECTED="$2"
[[ "$EXPECTED" =~ ^[a-f0-9]{40}$ ]] || fail "Expected full reviewed commit SHA"
[[ "$EUID" -eq 0 ]] || fail "Run as root on Belt"

REPO=/opt/router-orchestrator/release
BIN_DIR=/opt/router-orchestrator/host-adapter
BIN="$BIN_DIR/host_adapter.py"
UNIT=/etc/systemd/system/router-orchestrator-host-adapter.service
SERVICE=router-orchestrator-host-adapter.service
XRAY=/etc/xray/owrt-remote.json

[[ -d "$REPO" && ! -L "$REPO" ]] || fail "Release directory missing/unsafe"
cd "$REPO"
[[ "$(git rev-parse HEAD)" == "$EXPECTED" ]] ||
  fail "Git commit differs from reviewed SHA"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] ||
  fail "Unreviewed files in release checkout"
[[ ! -e "$BIN_DIR" && ! -L "$BIN_DIR" ]] ||
  fail "Pinned adapter directory already exists; review before overwriting"
[[ ! -e "$UNIT" && ! -L "$UNIT" ]] ||
  fail "Adapter systemd unit already exists; refusing overwrite"
[[ -f "$XRAY" && ! -L "$XRAY" ]] ||
  fail "Host Xray config missing/unsafe"

python3 tools/orchestrator_host_adapter_readonly_acceptance.py \
  preflight --expect-commit "$EXPECTED"

BEFORE="$(sha256sum "$XRAY" | awk '{print $1}')"
echo "===== INSTALLING PINNED READ-ONLY ADAPTER ====="
install -d -o root -g root -m 0700 "$BIN_DIR"
install -o root -g root -m 0600 docker/host_adapter.py "$BIN"
install -o root -g root -m 0644 docker/host_adapter.service "$UNIT"

systemd-analyze verify "$UNIT"
systemctl daemon-reload

# A failed acceptance must never touch HUB or Xray. Stop only the newly
# introduced adapter service; leave installed files for operator inspection.
accepted=0
cleanup() {
    if [[ "$accepted" != 1 ]]; then
        systemctl stop "$SERVICE" >/dev/null 2>&1 || true
        echo "[BLOCKED] New adapter stopped; production HUB/Xray untouched" >&2
    fi
}
trap cleanup EXIT

systemctl start "$SERVICE"
python3 tools/orchestrator_host_adapter_readonly_acceptance.py \
  probe --expect-commit "$EXPECTED"

AFTER="$(sha256sum "$XRAY" | awk '{print $1}')"
[[ "$BEFORE" == "$AFTER" ]] ||
  fail "Xray config SHA changed unexpectedly; investigate independently"

accepted=1
echo "READONLY HOST ADAPTER: ACCEPTED"
echo "SYSTEMD: adapter started but NOT enabled at boot"
echo "WRITES: no Xray apply/restart capability; no production migration"
echo "TO STOP ONLY ADAPTER: systemctl stop $SERVICE"
