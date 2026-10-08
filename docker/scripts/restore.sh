#!/bin/sh
set -eu
[ "$#" -eq 1 ] || { echo "Usage: $0 backup.tar.gz" >&2; exit 2; }
archive="$(realpath "$1")"
cd "$(dirname "$0")/../.."
[ -f "$archive" ] || { echo "Archive not found" >&2; exit 1; }
for svc in hub xray; do
  if docker compose ps --status running --services | grep -qx "$svc"; then
    echo "Stop services first: docker compose stop hub xray" >&2
    exit 1
  fi
done
mkdir -p data
[ ! -e data/state ] && [ ! -e data/xray ] || {
  echo "Refusing to overwrite data/state or data/xray. Restore to an empty project only." >&2; exit 1;
}
tmp="$(mktemp -d data/.restore.XXXXXXXX)"
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
python3 docker/scripts/verify_backup.py "$archive" "$tmp"
mv "$tmp/state" data/state
mv "$tmp/xray" data/xray
chmod 700 data/state data/xray
echo "Restored offline state and Xray config. Verify ports before docker compose up -d."
