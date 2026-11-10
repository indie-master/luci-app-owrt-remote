#!/usr/bin/env bash
# Belt-side WSS enrollment preflight. No daemon restart, no credential output.
# Default: read-only. --install modifies the config on disk only, with backup.
set -Eeuo pipefail
umask 077
export SYSTEMD_PAGER=cat
CONFIG=/etc/xray/owrt-remote.json
XRAY=/usr/local/bin/xray
DRY_RUN=1
usage() {
  echo "Usage: $0 --router-id ROUTER_ID [--config PATH] [--xray PATH] [--install]" >&2
}
die() { printf 'WSS-ENROLL: BLOCKED: %s\n' "$*" >&2; exit 1; }
router=''
while (($#)); do
  case "$1" in
    --router-id) (($# >= 2)) || die 'missing router id'; router="$2"; shift 2 ;;
    --config) (($# >= 2)) || die 'missing config path'; CONFIG="$2"; shift 2 ;;
    --xray) (($# >= 2)) || die 'missing xray path'; XRAY="$2"; shift 2 ;;
    --install) DRY_RUN=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage; die "unknown argument $1" ;;
  esac
done
[[ "$router" =~ ^[A-Za-z0-9_.-]+$ ]] || die 'invalid router ID'
[[ -f "$CONFIG" && ! -L "$CONFIG" ]] || die 'invalid config file'
[[ -x "$XRAY" ]] || die 'xray binary unavailable'
command -v jq >/dev/null || die 'jq required'
command -v sha256sum >/dev/null || die 'sha256sum required'
command -v cmp >/dev/null || die 'cmp required'
if ((DRY_RUN == 0)); then
  [[ $(id -u) -eq 0 ]] || die 'install requires root'
fi
source_digest=$(sha256sum "$CONFIG" | awk '{print $1}')
directory=$(dirname -- "$CONFIG")
tmp=$(mktemp "$directory/.owrt-wss-enroll.XXXXXXXX") || die 'cannot create temporary file'
normalized=$(mktemp "$directory/.owrt-wss-normalized.XXXXXXXX") || die 'cannot create temporary file'
cleanup() { rm -f -- "$tmp" "$normalized"; }
trap cleanup EXIT
# Pass router ID via jq --arg; do not interpolate it into jq source.
jq -e --arg router "$router" '
  def client_list($tag):
    [.inbounds[] | select(.tag == $tag) | .settings.clients[]?];
  (client_list("owrt-remote-vless")) as $tcp |
  (client_list("owrt-remote-wss")) as $wss |
  [$tcp[] | select(.email == ($router + "@owrt-remote") or
                   .email == ($router + "-ssh@owrt-remote"))] as $selected |
  if ([.inbounds[] | select(.tag=="owrt-remote-vless")]|length) != 1 or
     ([.inbounds[] | select(.tag=="owrt-remote-wss")]|length) != 1 then
    error("expected exactly one TCP and WSS inbound")
  elif ($selected|length) != 2 or ([ $selected[].id ]|unique|length) != 2 or
       ([ $selected[].email ]|unique|length) != 2 then
    error("expected exactly two unique TCP reverse credentials for this router")
  elif any($selected[]; (.id|type)!="string" or (.id|length)==0) then
    error("empty/invalid UUID")
  elif any($selected[]; .id as $id |
       any($wss[]; .id==$id and
           (.email != ($router+"@owrt-remote") and
            .email != ($router+"-ssh@owrt-remote")))) then
    error("UUID assigned to different WSS identity")
  elif any($wss[]; (.email == ($router+"@owrt-remote") or
                      .email == ($router+"-ssh@owrt-remote")) as $matching |
                    $matching and
                    (.id as $id | all($selected[]; .id != $id))) then
    error("WSS identity already exists with different UUID")
  else
    .inbounds |= map(
      if .tag == "owrt-remote-wss" then
        .settings.clients +=
          [$selected[] as $s | select(all($wss[]; .id != $s.id))]
      else . end)
  end
' "$CONFIG" > "$tmp" || die 'credential parity check failed'
"$XRAY" run -test -config "$tmp" >/dev/null 2>&1 || die 'candidate rejected by Xray'
jq -S . "$CONFIG" > "$normalized"
if cmp -s "$normalized" <(jq -S . "$tmp"); then
  printf 'WSS-ENROLL: ALREADY PRESENT — %s, no changes needed\n' "$router"
  exit 0
fi
printf 'WSS-ENROLL: VALIDATED — %s; two reverse credentials from TCP; Xray syntax PASS\n' "$router"
if ((DRY_RUN)); then
  echo 'WSS-ENROLL: DRY RUN — no files changed, use --install after review'
  exit 0
fi
# Serialize concurrent enrollments; check for source drift before installing.
command -v flock >/dev/null || die 'flock required for installation'
exec 9>"$directory/.owrt-wss-enroll.lock"
flock -n 9 || die 'another enrollment is running'
# The prepared candidate must still match the current source config, except
# for adding these two identities. Regenerate under the lock if config drifted.
[[ "$(sha256sum "$CONFIG" | awk '{print $1}')" == "$source_digest" ]] || die 'config changed since preflight; retry'
# Backup cannot contain secrets in its filename; preserve original permissions.
backup="$CONFIG.bak.wss-$(date -u +%Y%m%dT%H%M%SZ)-$$"
cp -p -- "$CONFIG" "$backup" || die 'backup failed'
chmod 600 "$backup"
# Fail closed if a different process changes config before install.
[[ "$(sha256sum "$CONFIG" | awk '{print $1}')" == "$source_digest" ]] || die 'config changed during install'
chown --reference="$CONFIG" "$tmp"
chmod --reference="$CONFIG" "$tmp"
mv -f -- "$tmp" "$CONFIG"
echo "WSS-ENROLL: INSTALLED ON DISK — $router"
echo "WSS-ENROLL: BACKUP — $backup"
echo 'WSS-ENROLL: RUNNING XRAY UNCHANGED — controlled restart still required'
