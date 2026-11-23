#!/usr/bin/env bash
# Supervised replacement of the *existing READ-ONLY* adapter code only.
# Does NOT touch the host Xray service, config, HUB, maintenance hold or routers.
set -Eeuo pipefail
umask 077
fail() { printf '[BLOCKED] %s\n' "$1" >&2; exit 2; }
[[ "$#" -eq 2 && "$1" == "--upgrade-readonly" ]] ||
  fail "Usage: bash .../orchestrator_host_adapter_readonly_upgrade.sh --upgrade-readonly <40-char SHA>"
EXPECTED="$2"
[[ "$EXPECTED" =~ ^[a-f0-9]{40}$ ]] || fail "Invalid Git SHA"
[[ "$EUID" -eq 0 ]] || fail "Requires root"

REPO=/opt/router-orchestrator/release
PINNED=/opt/router-orchestrator/host-adapter/host_adapter.py
UNIT=/etc/systemd/system/router-orchestrator-host-adapter.service
XRAY=/etc/xray/owrt-remote.json
ADAPTER=router-orchestrator-host-adapter.service
PREVIOUS=8a9e3a87c0915cf6d616370dd0942b1fa121f308

cd "$REPO" || fail "Release checkout missing"
[[ "$(git rev-parse HEAD)" == "$EXPECTED" ]] ||
  fail "Unexpected Git HEAD"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] ||
  fail "Git working tree not clean"
[[ -f "$PINNED" && ! -L "$PINNED" ]] ||
  fail "Existing adapter is missing or unsafe"
[[ -O "$PINNED" && "$(stat -c %a "$PINNED")" == "600" ]] ||
  fail "Existing adapter permissions do not match pinned installation"
[[ -f "$UNIT" && ! -L "$UNIT" ]] ||
  fail "Read-only adapter systemd unit missing/unsafe"
cmp -s "$UNIT" docker/host_adapter.service ||
  fail "Installed read-only unit differs from reviewed source"
[[ -f "$XRAY" && ! -L "$XRAY" ]] ||
  fail "Host Xray config missing/unsafe"
systemctl is-active --quiet "$ADAPTER" ||
  fail "Existing read-only adapter is not active"
systemctl is-active --quiet owrt-remote.service ||
  fail "Production HUB not active"
systemctl is-active --quiet owrt-remote-xray.service ||
  fail "Host Xray not active"

# Refuse upgrading an unexpected root executable.
git cat-file -e "$PREVIOUS:docker/host_adapter.py" ||
  fail "Reviewed previous adapter Git source is not available"
cmp -s "$PINNED" <(git show "$PREVIOUS:docker/host_adapter.py") ||
  fail "Installed adapter differs from approved previous release"

python3 -m py_compile docker/host_adapter.py
python3 tools/orchestrator_host_adapter_readonly_acceptance.py \
  preflight --expect-commit "$EXPECTED" >/dev/null 2>&1 && \
  fail "New source was already active; refuse ambiguous upgrade" || true

BEFORE="$(sha256sum "$XRAY" | awk '{print $1}')"
PARENT="$(dirname "$PINNED")"
BACKUP="$PARENT/host_adapter.py.before-preview-$PREVIOUS"
[[ ! -e "$BACKUP" && ! -L "$BACKUP" ]] ||
  fail "Adapter backup already exists; refuse overwrite"
install -o root -g root -m 0600 "$PINNED" "$BACKUP"
STAGED="$(mktemp "$PARENT/.new-adapter.XXXXXXXX")"
chmod 0600 "$STAGED"
install -o root -g root -m 0600 docker/host_adapter.py "$STAGED"

replaced=0
completed=0
cleanup() {
  status=$?
  trap - EXIT
  if [[ "$completed" != "1" && "$replaced" == "1" ]]; then
    echo "[RECOVERY] Restoring previous read-only adapter binary" >&2
    install -o root -g root -m 0600 "$BACKUP" "$PINNED"
    systemctl restart "$ADAPTER" ||
      echo "[CRITICAL] Read-only adapter failed to restart after rollback" >&2
  fi
  if [[ -e "$STAGED" ]]; then rm -f -- "$STAGED"; fi
  AFTER="$(sha256sum "$XRAY" | awk '{print $1}')"
  if [[ "$BEFORE" != "$AFTER" ]]; then
    echo "[CRITICAL] Host Xray config SHA unexpectedly changed" >&2
    status=2
  fi
  exit "$status"
}
trap cleanup EXIT

mv -f -- "$STAGED" "$PINNED"
replaced=1
systemctl restart "$ADAPTER"
python3 tools/orchestrator_host_adapter_readonly_acceptance.py \
  probe --expect-commit "$EXPECTED"
[[ "$BEFORE" == "$(sha256sum "$XRAY" | awk '{print $1}')" ]] ||
  fail "Host Xray config changed during adapter-only upgrade"

completed=1
echo "HOST ADAPTER READONLY UPGRADE: PASS"
echo "PREVIEW: xray.preview available; xray.apply/restart still disabled"
echo "XRAY: config unchanged; Xray and production HUB not restarted"
echo "BACKUP: previous adapter binary saved in pinned private directory"
