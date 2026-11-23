#!/usr/bin/env bash
# Independent Xray rescue: explicit install, NO services or live config changed.
set -Eeuo pipefail
umask 077

die() { printf '[BLOCKED] %s\n' "$1" >&2; exit 2; }
[[ "$#" -eq 2 && "$1" == "--install-rescue" ]] ||
  die "Usage: bash tools/orchestrator_wss_rescue_install.sh --install-rescue <reviewed 40-char SHA>"
EXPECTED="$2"
[[ "$EXPECTED" =~ ^[a-f0-9]{40}$ ]] || die "Full reviewed Git SHA required"
[[ "$EUID" -eq 0 ]] || die "Root required"

REPO="/opt/router-orchestrator/release"
SOURCE="$REPO/tools/orchestrator_wss_emergency_rescue.py"
TARGET="/usr/local/sbin/router-orchestrator-wss-rescue"
[[ -d "$REPO" && ! -L "$REPO" ]] || die "Release checkout missing/unsafe"
cd "$REPO"
[[ "$(git rev-parse HEAD)" == "$EXPECTED" ]] || die "Unexpected Git commit"
[[ -z "$(git status --porcelain --untracked-files=all)" ]] ||
  die "Unreviewed release files"
[[ -f "$SOURCE" && ! -L "$SOURCE" ]] || die "Reviewed rescue source missing"
[[ ! -e "$TARGET" && ! -L "$TARGET" ]] ||
  die "Existing rescue executable; do not overwrite without review"
[[ -d /usr/local/sbin && ! -L /usr/local/sbin ]] ||
  die "Root utility directory missing/unsafe"
[[ "$(stat -c %u /usr/local/sbin)" == "0" ]] ||
  die "Root utility directory not owned by root"
[[ "$(stat -c %a /usr/local/sbin)" =~ ^[0-7]+$ ]] ||
  die "Unexpected sbin permissions"
[[ ! "$(stat -c %A /usr/local/sbin)" =~ ^d....w|^d.......w ]] ||
  die "Root utility directory group/world writable"
python3 -m py_compile "$SOURCE"
SOURCE_SHA="$(sha256sum "$SOURCE" | awk '{print $1}')"
install -o root -g root -m 0700 "$SOURCE" "$TARGET"
[[ "$(stat -c %u:%g:%a "$TARGET")" == "0:0:700" ]] ||
  die "Installed rescue owner/permissions wrong"
[[ "$(sha256sum "$TARGET" | awk '{print $1}')" == "$SOURCE_SHA" ]] ||
  die "Installed rescue hash differs"
echo "WSS INDEPENDENT RESCUE: INSTALLED"
echo "SHA256: $SOURCE_SHA"
echo "STATUS ONLY: sudo /usr/local/sbin/router-orchestrator-wss-rescue status"
echo "NO CHANGE: Xray configuration, services, HUB, Docker and routers untouched"
