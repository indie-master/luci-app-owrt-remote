#!/bin/sh
# Cold import from an existing, stopped host-systemd installation.
set -eu
cd "$(dirname "$0")/../.."
state="${OWRT_OLD_STATE_DIR:-/var/lib/owrt-remote}"
xray="${OWRT_OLD_XRAY_CONFIG:-/etc/xray/owrt-remote.json}"
if systemctl is-active --quiet owrt-remote || systemctl is-active --quiet owrt-remote-xray; then
  echo "Original OWRT systemd services are active; stop them for a consistent import." >&2
  exit 1
fi
if [ ! -d "$state" ] || [ ! -f "$xray" ]; then
  echo "Source state or Xray config not found." >&2; exit 1
fi
if [ -e data/state ] || [ -e data/xray ]; then
  echo "Destination already exists; refusing overwrite." >&2; exit 1
fi
umask 077
mkdir -p data/state data/xray data/control
cp -a "$state/." data/state/
cp -a "$xray" data/xray/owrt-remote.json
chmod 700 data/state data/xray data/control
chmod 600 data/xray/owrt-remote.json
echo "Imported offline state. No systemd services or Nginx were started/stopped."
echo "Check data/xray/owrt-remote.json listener port and .env before Docker deployment."
