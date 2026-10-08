#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
echo "===== COMPOSE ====="
docker compose config --quiet
echo "===== FORBIDDEN TRACKED RUNTIME FILES ====="
if git ls-files | grep -E '^(data/|backups/|\.env$)|\.(pem|key|token|db)(-|$)'; then
  echo "ERROR: runtime secrets appear in Git index" >&2; exit 1
fi
echo "===== PORTS (will NOT change listeners) ====="
for p in 8088 18443; do
  ss -lnt 2>/dev/null | grep -E ":$p[[:space:]]" || true
done
echo "===== SUCCESS: inspection only ====="
