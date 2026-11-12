# Router Orchestrator — Docker edge, TLS reuse and transport recovery

Status: **experimental source/preview; no production deployment**.
This document is the contract for the planned full implementation.
Do not run a Docker installer from this development branch on Belt.

## Target user flow: fresh installation

A new server gets a domain/TLS wizard before the HUB accepts router
enrollment. The wizard is available as a standalone static, offline
preview in vps/static/orchestrator-setup-preview.html. It creates
two private draft files, not live deployment instructions:

- orchestrator-edge.review.json — domain, reverse proxy, TLS and ports
- orchestrator-profiles.review.json — named WSS/REALITY options

The drafts can be validated with the Python modules in tools/.
Neither draft includes a global HUB token, router UUIDs or REALITY
private keys. The wizard does not make network requests or write
the original HUB database.

### A. Reuse an existing Nginx (recommended on Belt)

- Keep Nginx running on the host or an independently managed edge.
- Nginx keeps responsibility for 80/443 and Let's Encrypt renewal.
- Docker only publishes HUB and Xray WSS upstreams on host
  loopback addresses, e.g. 127.0.0.1:18088 and 127.0.0.1:28444.
- The reviewed Nginx snippet routes /owrt-ws/ to loopback WSS and
  the chosen site root to the HUB. Never overwrite an existing vhost.
- Preserve the already working swiftlessvpn.ru and cdnvc.xyz
  virtual hosts and certbot renewal. Containerized Nginx must NOT
  simultaneously try to bind the same public 443.
- No private certificate or key needs to be mounted into the HUB.

### B. Bundled Nginx in Docker (fresh empty VPS)

- Nginx runs as a Docker Compose service with the HUB/Xray stack.
- Use certbot/ACME with dedicated shared volumes for the challenge
  webroot and renewed certificate tree.
- HTTP-01 requires port 80 reachable from the Internet; the first
  boot serves HTTP challenge-only, obtains cert, validates its SAN
  and expiry, then enables 443/TLS. Nginx will not start if the TLS
  paths do not exist yet. This two-phase bootstrap MUST be
  implemented by the deployment coordinator, not by hopeful Compose
  restarts.
- DNS-01 allows wildcard and domains without inbound port 80, but
  requires a supported DNS provider plugin and private credentials
  mounted as Docker secrets. It is never safe to put API credentials
  in compose.yml, environment logs or an exported client plan.
- Existing PEM files may be mounted read-only. For an existing
  certbot tree, mount the whole /etc/letsencrypt directory (including
  live/ and archive/), not separate symlink leaf files, to preserve
  renewal semantics. Certificate renewal remains owned by the
  selected issuer and needs a reliable Nginx reload trigger.
- The HUB must never have the host Docker socket mounted.

### Container networking and lifecycle

The original HUB's reverse local LuCI/SSH tunnels listen on
127.0.0.1 in Xray's network namespace. Therefore, if HUB and Xray
are split into two containers, HUB must share the Xray network
namespace (network_mode: service:ro-core), or a new explicitly
tested reverse-port adapter must replace local 127.0.0.1
assumptions. This is reflected in the generated Compose preview.

On Docker, the original Python HUB cannot call systemctl to
restart owrt-remote-xray.service. Before shipping a Docker install,
implement a privileged, narrow Xray lifecycle adapter with its own
authentication and restart/rollback boundaries. The HUB application
must not gain the Docker socket merely to restart a container.
Existing systemd-mode deployments retain the original adapter.

The generated compose.review.yml contains deliberately unresolved,
pinned image placeholders and must not be deployed. HUB/Xray listener
addresses also require a container-mode binding policy. The preview
is to communicate dependencies and network layout, not to replace
the current working Nginx/Xray.

## Multiple per-router transport profiles

A router can have:
- WSS primary (domain A, valid TLS certificate, /owrt-ws/)
- WSS backup (domain B with a valid certificate and matching Nginx route)
- REALITY/RAW pilot (own public listener, separate SNI/target/keypair)
- Legacy TCP (existing clients only, never re-created automatically)

The new offline catalog permits multiple named profiles.
Use tests/test_orchestrator_profile_catalog.py as the behavior
contract. New profiles are generated on BOTH sides from one
versioned object; the control plane must not publish the server
REALITY private key in the client config.

### Safe change flow (planned, not yet implemented)

1. Existing connection stays active. Operator selects an alternative
   profile and clicks "Generate candidate".
2. Provision candidate server listener/identities without modifying
   the existing ones. Candidate UUID/reverse tags and probe ports
   must be isolated, so two active Xray instances do not race over
   the same reverse registration.
3. Candidate config is provisioned on OpenWrt while old link is still
   reachable. Candidate Xray starts as a separate supervised process
   with a different local JSON; legacy Xray and podkop are untouched.
4. Verify candidate config syntax with pinned cores, SNI/TLS/REALITY,
   both reverse channels, LuCI HTTP, SSH banner and a fresh HUB
   heartbeat. A successful TCP handshake alone is insufficient.
5. Only after both sides confirm health can a two-phase commit
   promote candidate to primary. Persist previous revision and
   always arm watchdog rollback before switching.
6. If candidate fails or either side disconnects, restore the old
   active revision automatically. Never delete its identities before
   the new route is accepted.
7. When a router is entirely unreachable, a remote HUB cannot
   magically install a new config. Automatic failover requires the
   router to already contain a tested secondary profile and a
   local watchdog; otherwise report "No safe alternative" and
   keep the last-known-good profile.

A fully automatic recovery setting may be offered per router only
after an offline state-machine test, then hardware pilot, with
hysteresis (at least 3 confirmed failures) and a cooldown period.
For now the offline planner merely suggests an action; it does not
alter router configuration.

## Prototype commands (development host only)

Create a draft for an installation **behind existing Nginx**:

~~~sh
python3 tools/orchestrator_edge_plan.py init \
  --mode external_nginx --domain hub.example.net \
  --output /tmp/edge-draft.json
python3 tools/orchestrator_edge_plan.py stage \
  /tmp/edge-draft.json --output-dir /tmp/edge-review
~~~

For **bundled Nginx with ACME HTTP-01**:

~~~sh
python3 tools/orchestrator_edge_plan.py init \
  --mode bundled_nginx --tls acme_http01 \
  --domain hub.example.net --email admin@example.net \
  --output /tmp/edge-bundled.json
python3 tools/orchestrator_edge_plan.py stage \
  /tmp/edge-bundled.json --output-dir /tmp/edge-bundled-review
~~~

The generated compose.review.yml and Nginx snippets are
deliberately review-only and do not automatically obtain certs.

Prepare a transport catalog:

~~~sh
python3 tools/orchestrator_profile_catalog.py init \
  --domain hub.example.net \
  --backup-domain backup.example.net \
  --add-reality \
  --out /tmp/transport-draft.json
python3 tools/orchestrator_profile_catalog.py profiles \
  /tmp/transport-draft.json
~~~

The REALITY profile starts disabled. Its SNI, target and key
references must be replaced with explicitly chosen and verified
values before even a test candidate can be prepared.

### Generate a selected alternative (private review fragments)

On an isolated development/test machine, create a 0600 router JSON
containing the two router UUIDs. If the selected profile is REALITY,
provide a 0600 JSON secret file with private_key, public_key and short_id.
Neither file belongs in a repository, a ticket, or an installation link.

~~~sh
umask 077
python3 tools/orchestrator_profile_catalog.py candidate \
  /root/transport-draft.json \
  --profile wss-primary \
  --router-file /root/private-router-identities.json \
  --server-out /root/candidate-server.review.json \
  --client-out /root/candidate-router.review.json
~~~

For REALITY only, add
`--secrets-file /root/private-reality-keypair.json`.
The command rejects world-readable secret files, refuses existing
output paths, and never prints UUIDs or key contents. The server fragment
includes the private key only where required; the client fragment
contains only the public key. Both outputs remain NONDEPLOYABLE
fragments, not a complete live Xray JSON or an installation script.
Do not copy either JSON directly to the production Xray config.

## Tests and next deploy gates

- Python backend validation and port collisions
- Standalone wizard JS executed in a fake DOM and its exported JSON
  checked by both Python validators
- WSS/REALITY server-client fragment parity, separate private keys
- Per-router verified switch policy and manual recovery
- Real integration pilot using packaged nginx, certbot, isolated HUB
  and exact Xray binaries. Add wildcard/DNS-01 provider integration
  only after credential isolation and automated renew+reload tests.
- Production deployment requires secured updater and no regression
  for original v119 features and existing six-router TCP/WSS fleet.

No Docker Compose up, Nginx reload, Xray restart or change to
/var/lib/owrt-remote/hub.db is part of these prototypes.
