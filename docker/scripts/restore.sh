#!/bin/sh
set -eu
[ "$#" -eq 1 ] || { echo "Usage: $0 backup.tar.gz" >&2; exit 2; }
backup="$(realpath "$1")"
cd "$(dirname "$0")/../.."
if docker compose ps --status running --services | grep -Eq '^(hub|xray)$'; then
  echo "Stop both services before restore: docker compose down" >&2
  exit 1
fi
mkdir -p data
[ ! -e data/state ] && [ ! -e data/xray ] || { echo "data/state or data/xray exists; restore only into an empty directory" >&2; exit 1; }
tar -tzf "$backup" | grep -Eq '^state/' || { echo "Missing state/" >&2; exit 1; }
tar -tzf "$backup" | grep -Eq '^xray/' || { echo "Missing xray/" >&2; exit 1; }
if tar -tzf "$backup" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then echo "Unsafe archive paths" >&2; exit 1; fi
tar -xzf "$backup" -C data --no-same-owner --no-same-permissions
chmod 700 data/state data/xray
echo "Restore complete. Inspect settings before docker compose up -d"
