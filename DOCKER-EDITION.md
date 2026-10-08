# Docker Compose edition (experimental)

The upstream OpenWrt agent remains unchanged. This branch containerizes the **VPS side only**.
No live host or secrets were used in development.

## Architecture
- `hub`: upstream Python Hub, bind 127.0.0.1:8088, persistent SQLite and secrets under `data/state`.
- `xray`: Xray 26.3.27 and small supervisor, reverse endpoints on host networking, reads `data/xray/owrt-remote.json`.
- Control channel: Hub's restricted `systemctl restart owrt-remote-xray` adapter requests config verification/restart through a shared file; **no Docker socket**.
- Existing host Nginx reverse proxy is retained. Do not launch alongside host Hub/Xray on the same ports.

## Clean deployment (test VPS recommended)
```sh
cp .env.example .env
mkdir -p data/{state,xray,control}
chmod 700 data/state data/xray data/control
docker compose build
# Initialize DB and admin credentials interactively; do not paste credentials into shell history
docker compose run --rm hub python /opt/owrt-remote/owrt-remote-hub.py init
docker compose run --rm hub python /opt/owrt-remote/owrt-remote-hub.py set-login --help
# Set admin credentials using the supported CLI in an isolated session
docker compose run --rm hub python /opt/owrt-remote/owrt-remote-hub.py render-xray --out /etc/xray/owrt-remote.json
docker compose up -d
docker compose ps
```
Avoid publishing 8088; bind the host's Nginx to 127.0.0.1:8088. A host network service runs with host port visibility; audit UFW/iptables.

## Migration from legacy systemd service
1. Develop and test off production. Verify image builds and Xray config syntax.
2. Schedule maintenance; snapshot filesystem and collect `/var/lib/owrt-remote`, `/etc/xray/owrt-remote.json`, `/opt/owrt-remote` code/version, and service environment. Treat backups as secrets.
3. For consistent SQLite, stop legacy Hub (or use SQLite backup API), then create the final snapshot. Keep the old systemd units installed but disabled for the cutover.
4. Copy state files to `data/state/`, Xray config to `data/xray/owrt-remote.json`, restrict permissions, verify UUID/ports locally.
5. Run `docker compose run --rm xray /usr/local/bin/xray run -test -config /etc/xray/owrt-remote.json` (entrypoint must be overridden: `--entrypoint /usr/local/bin/xray`).
6. Stop legacy Hub/Xray; start containers; verify ports and `/health`, then test each router's LuCI and SSH.
7. Roll back by `docker compose down`, restore the saved legacy state/config and start legacy systemd services. Never run both stacks on the same ports.

## Notes / limitations
- This edition is not yet integration-tested end-to-end on a real VPS.
- Any upstream hardcoded systemctl actions beyond the isolated Xray restart are denied intentionally.
- Hub's old VPS terminal may not manage host services inside Docker; host maintenance remains outside the container.
- Nginx site changes / Let's Encrypt issuance remain managed by the host.
- `OWRT_REMOTE_VLESS_PORT` defaults to 18443 in the compose file. Existing per-router VLESS UUIDs must be preserved at migration.
- Repo's HTTP availability indicator is a heartbeat, not an authenticated check of the reverse path.
- The `data/`, `.env` and backups are ignored and must never be pushed.
