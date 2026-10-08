#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
umask 077
mkdir -p backups data/state data/xray data/control
file="backups/owrt-remote-$(date -u +%Y%m%dT%H%M%SZ).tar.gz"
# Quiesce application for consistent SQLite backup; caller should schedule downtime.
if docker compose ps --status running --services | grep -qx hub; then
  echo "Stop Hub first for a consistent backup: docker compose stop hub" >&2
  exit 1
fi
tar -czf "$file" -C data state xray
echo "Created $file (contains sensitive secrets; never commit or share)"
