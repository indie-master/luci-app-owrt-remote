# Isolated Docker HUB rehearsal — source-exact, offline, no Belt changes

Status: **development and test only**. This is a real container boot smoke
test, but it is NOT a production Docker installation and NOT a cutover
coordinator. It cannot replace existing systemd services or access routers.

## Purpose

The first Docker migration step must protect all data in a mixed six-router
fleet (currently 5 WSS + 1 legacy TCP on Belt). The host HUB and host Xray
have already been independently verified. Keep the existing Xray process,
JSON, Nginx, HTTP/SSH reverse portals, TOTP/Passkey, notification state,
historical logs, session material, and exact router UUIDs intact.

This stage adds a realistic runtime test:

1. Take a complete **private rehearsal snapshot** with
   tools/orchestrator_belt_docker_migration_gate.py. SQLite is copied via
   online backup; other state files are archived under SHA256 manifest.
2. **Restore** the archive into a NEW, private, independent shadow state
   directory with tools/orchestrator_docker_rehearsal_restore.py.
   Every archive member must match its digest, every restored file
   receives mode 0600 and directories 0700. The tool checks SQLite
   table row counts, six router identity/transport mappings and
   original Xray client/port/reverse routing parity. It refuses symlinks,
   strange archive members, omitted files, or an unsafe destination.
3. Build the Docker image ONLY from reference/hub-source.py from that
   snapshot and explicitly chosen static assets. Do not add the state
   directory or any tokens to the Docker build context. The Dockerfile
   uses SOURCE_SHA256 to reject an unexpected source at build time.
4. Start the container with --network none, --read-only, --cap-drop ALL,
   no published ports, an isolated writable snapshot state bind mount,
   no host Docker socket and no real Xray/Nginx/certificate paths.
   Use --log-driver none because the old HUB displays agent.token when
   launched. This avoids recording credentials in Docker daemon logs.
5. Poll the internal /health endpoint from docker exec (the port is not
   reachable from the host). Assert DB router identities, WSS/TCP modes
   and historical router_logs count remain unchanged. Stop/remove the
   container after the test. Changes in the private copy are expected.
   The original files are never mounted.

This is **not** evidence that LuCI/SSH work in Docker. The networkless test
deliberately has no connection to actual routers. Those require a later
separate, supervised HUB-only cutover pilot with existing host Xray.

## Source and dependencies

- docker/hub/Dockerfile.rehearsal (Python 3.12 slim + OpenSSH client,
  pywebpush 2.5.0, fido2 2.2.1). The pinned top-level versions are
  NOT a final transitive dependency lock. Release images need image digests,
  full dependency hashes, SBOM/signature verification and parity testing.
- tools/orchestrator_docker_image_context.py
- tools/orchestrator_docker_shadow_runner.py
- tools/orchestrator_docker_rehearsal_restore.py
- tests/ci_orchestrator_shadow_fixture.py: generates only synthetic data.
  GitHub Actions can boot the actual development HUB in Docker from a
  synthetic six-router state. No real keys or Belt snapshots in CI.

The existing HUB source on Belt and the development-branch source differ.
The shadow process must use the EXACT source code that was included in
the chosen snapshot, never silently substitute a newer development
source. Docker build/run guards compare its SHA256. An isolated rehearsal
can therefore be performed before any production code upgrade.

## Development-only synthetic rehearsal

On a machine with Docker and an explicitly pinned, reviewed repository
revision (not a production server):

~~~bash
set -euo pipefail
umask 077
WORK="$(mktemp -d)"
python3 tests/ci_orchestrator_shadow_fixture.py --output-root "$WORK"
CTX="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["context"])' "$WORK/ci-paths.json")"
STAGE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["stage"])' "$WORK/ci-paths.json")"
SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["source_sha256"])' "$WORK/ci-paths.json")"
docker build --pull \
  --build-arg "SOURCE_SHA256=$SHA" \
  --file "$CTX/Dockerfile" \
  --tag orchestrator-shadow:ci "$CTX"
python3 tools/orchestrator_docker_shadow_runner.py \
  --stage "$STAGE" --image orchestrator-shadow:ci
~~~

Expected end: DOCKER SHADOW: PASS, FLEET 6/WSS 5/TCP 1, HISTORY
router_logs 5, and the test container is removed. The
result means **only** that the process boots, health responds, the archived
source matches the built image and the synthetic history survived.

## Operator rehearsal on Belt: NOT YET AN APPROVED RUNBOOK

The tools can support a future private Belt snapshot, but do not copy
real persistent files into GitHub Actions, public cloud CI, issue trackers
or container registries. A private snapshot may carry real credentials,
2FA settings, browser sessions and access to every router.

Before authorizing any real-snapshot Docker rehearsal:
- confirm a reliable protected recovery console to Belt;
- review all downloaded tools at a fixed Git SHA;
- verify existing runtime Xray and Nginx are still healthy;
- create snapshot/restore *on Belt* or an equally protected, trusted VPS;
- use isolated private state COPY and never bind the live state;
- use local-only Docker images without pushing them to a registry;
- retain /var/lib/owrt-remote/.xray-maintenance-hold;
- run and verify every check without changing live systemd/Nginx/Xray.

The live migration sequence must eventually include a single-writer
handover, real host Xray reverse-port reachability, an explicit
capability-limited Docker Xray control adapter and a tested rollback.
The networkless smoke test does not implement those operations.

## Security limitation: original token logging

Original cmd_serve() prints AGENT_TOKEN to stdout on startup. The
isolated runner uses --log-driver none to prevent that output from
being stored by the Docker logging driver. Before full Docker production
release, remove or redact secrets from all application logs by design
and introduce structured redacted logs. Never use the pilot as a
publicly exposed service.

## Exit conditions

- State restored and verified; every historical SQLite table count intact.
- Xray JSON/client/reverse parity checked without touching live service.
- Container and image source hashes agree.
- Internal HTTP health OK.
- No external network, no published Docker ports, no host Docker socket.
- Core source and auth/passkey/web-push dependencies available.
- No claim of production readiness until actual LuCI+SSH+heartbeat
  and all original v119 features are tested in a controlled setup.
