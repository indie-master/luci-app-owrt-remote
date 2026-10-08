# Docker Compose Edition — VPS

Dedicated fork branch `docker-edition`. Only the VPS side is containerized; the OpenWrt agent is retained from upstream. **Do not run this alongside the existing systemd Hub/Xray on the same VPS ports.**

## Architecture

- `hub`: upstream Python app, bind `127.0.0.1:8088` by default, persistent SQLite and state. Refuses to serve without a configured administrator password.
- `xray`: official Xray-core 26.3.27 binary with an isolated supervisor, public VLESS on `18443` by default.
- Both use `network_mode: host` on Linux to preserve localhost LuCI/SSH forward ports for multiple routers; no container publishes `443`.
- Synchronous UNIX socket control: validate candidate Xray config before changing files, activate, confirm child stays up, rollback previous config if the new one fails. No systemd, Docker socket or privileged container.
- Config and state in ignored directories `data/xray`, `data/state`; private control socket in `data/control`.
- Server generator takes `XRAY_PORT` from `.env` (not hardcoded 8443). VLESS target is normalized to a hostname/IP; public Hub URL remains a URL.
- Existing Nginx and Remnawave remain external on the host. Do not run the upstream `install-vps.sh` in Docker.

The web UI's VPS terminal runs **inside the Hub container** and cannot administer the host. Host maintenance, Nginx, UFW, certbot remain outside Docker. Heartbeat Online is not proof of a healthy reverse tunnel.

## Install on a clean staging VPS

Requirements: rootful Docker Engine on Linux amd64/arm64, Compose v2, unrestricted loopback, free public VLESS port and free local Hub port. Runtime directories are root-owned (0700) because capabilities are dropped in containers.

```sh
git clone --branch docker-edition https://github.com/indie-master/luci-app-owrt-remote.git
cd luci-app-owrt-remote
cp .env.example .env
mkdir -p data/state data/xray data/control
sudo chown -R 0:0 data/state data/xray data/control
sudo chmod 700 data/state data/xray data/control
docker compose config --quiet
docker compose build
read -r -s -p 'New Hub password (12+ characters): ' HUB_PASS; echo
printf '%s\n' "$HUB_PASS" | docker compose run --rm --no-deps -T hub set-password-stdin admin
unset HUB_PASS
docker compose up -d
docker compose ps
docker compose exec -T hub owrt-hub xray-status
```

An initial Hub does not start with the upstream admin/admin default: you must set a password first. Bind public HTTPS with your **existing host Nginx** to `http://127.0.0.1:8088`. Do not expose Hub HTTP directly. Set `PUBLIC_URL` to your own production URL before onboarding new clients; examples contain no secrets.

## Operations

```sh
docker compose logs -f --tail=80 hub xray
docker compose exec -T hub owrt-hub list-routers
docker compose exec -T hub owrt-hub apply-xray
docker compose exec -T hub owrt-hub xray-status
```

Hub UI changes trigger a synchronous, validated Xray reload automatically. If you change router records using CLI, explicitly run `apply-xray`. An invalid config is rejected without terminating the active Xray. No automatic reload on individual WAN reconnect (this avoids dropping other tunnels).

## Consistent cold backup and restore

**All backup archives contain UUIDs, tokens, auth data and other private information. Do not upload them into the repository.**

```sh
docker compose stop hub xray
sh docker/scripts/backup.sh
docker compose up -d
```

This creates a private archive in `backups/` containing the *entire* `state/` directory and the active `xray/` config. Unlike the original built-in Hub backup, this captures the complete Compose volumes. Cold snapshots require Hub and Xray to be stopped.

Restore is allowed **only with stopped services and an empty data directory**:

```sh
docker compose stop hub xray
# Move or archive old data/ securely and restore only into an empty destination.
sh docker/scripts/restore.sh /secure/path/owrt-remote-YYYYMMDDTHHMMSSZ.tar.gz
docker compose up -d
```

The restore helper rejects absolute/traversal paths, symlinks, duplicate file entries and oversized payloads; it does not overwrite an existing installation.

## Production migration from systemd (do this later)

1. Test the fork on an **independent staging VM**. Do not connect the same production router to two Xray servers simultaneously. Test UI, actual reverse LuCI and SSH, and rollback.
2. Take a protected offline copy of original `/var/lib/owrt-remote`, `/etc/xray/owrt-remote.json` and the old app version. Preserve auth, tokens and UUIDs; no credentials go into Git.
3. During a maintenance window, stop only `owrt-remote` and `owrt-remote-xray` systemd services. Leave Nginx/Remnawave untouched.
4. With the new Docker project `data/state` and `data/xray` not yet created, run `sh docker/scripts/migrate-from-host.sh` (requires old units inactive). This imports the legacy state and Xray config without changing originals.
5. Verify `XRAY_PORT` in `.env` equals the actual inbound port in the imported Xray JSON, and `HUB_PORT` equals host Nginx's upstream port. Keep original tunnel hostnames and client UUIDs.
6. Start `docker compose up -d`. Check health endpoints plus actual LuCI and SSH for each router. The old systemd units remain installed but stopped.
7. Rollback: `docker compose stop hub xray`, restore the previously saved host state/config, start only the original two systemd units. Never enable both stacks at once on overlapping ports.

An original Hub-format archive (`manifest.json` + `state/`) can be imported separately using original `restore` CLI, but it is **not interchangeable** with the cold Compose `state/xray` archive.

The current fork is not presented as production-proven until independently tested on a VPS. Code does not contact the user's Belt host, nor include client IDs, real domain names, tokens or server keys.
