#!/bin/sh
# Import a CONSISTENT offline snapshot from an existing host installation.
# Usage: sh docker/scripts/import-legacy.sh /path/to/legacy-snapshot
# Source directory must contain state/ and xray/owrt-remote.json.
set -eu
[ "$#" -eq 1 ] || { echo "Usage: sh $0 /path/to/snapshot" >&2; exit 2; }
source_dir="$(realpath "$1")"
cd "$(dirname "$0")/../.."
test -f "$source_dir/state/hub.db" || { echo "Missing state/hub.db" >&2; exit 1; }
test -f "$source_dir/xray/owrt-remote.json" || { echo "Missing xray/owrt-remote.json" >&2; exit 1; }
if docker compose ps --status running --services | grep -Eq '^(hub|xray)$'; then
  echo "Stop Docker edition before importing a legacy backup" >&2; exit 1
fi
if [ -e data/state ] || [ -e data/xray ]; then
  echo "Destination data/state or data/xray already exists; refusing overwrite" >&2; exit 1
fi
umask 077
mkdir -p data/state data/xray data/control
cp -a "$source_dir/state/." data/state/
cp -a "$source_dir/xray/." data/xray/
chmod 700 data/state data/xray data/control
echo "Import complete (no containers started). Review config/port and secrets locally before enabling."
