#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
umask 077
for svc in hub xray; do
  if docker compose ps --status running --services | grep -qx "$svc"; then
    echo "Stop services before a fully consistent backup: docker compose stop hub xray" >&2
    exit 1
  fi
done
[ -d data/state ] && [ -d data/xray ] || { echo "Missing data/state or data/xray" >&2; exit 1; }
mkdir -p backups
out="backups/owrt-remote-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
tar -czf "$out" -C data state xray
chmod 600 "$out"
echo "Created $out"
echo "SECRET ARCHIVE: contains tokens, UUIDs, users and session keys. Store offline; never upload to Git."
