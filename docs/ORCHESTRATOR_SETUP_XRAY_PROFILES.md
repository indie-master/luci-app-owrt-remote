# Router Orchestrator: flexible setup and Xray transport profiles

Status: development branch, **offline preview only**, not production deployment.

## First-run wizard (target user experience)

A new installation receives a guided form:
1. Domain, DNS, HTTPS public URL, TLS certificate strategy, bind IP and public/backend ports. Check DNS A/AAAA, public reachability, certificate and listening port conflicts.
2. Select transports: WSS/TLS default, optional VLESS REALITY RAW, or preserve existing legacy TCP. HTTPS/Nginx front end must be independent from Xray WSS loopback port.
3. Choose exact server and OpenWrt Xray versions, inspect architecture and release SHA256, validate candidate configs.
4. Show a secret-redacted preview, take backups, apply with time-limited rollback and confirm LuCI, SSH and heartbeat.

After installation, expose these settings only to the owner under "Settings → Network, TLS, Transports, Xray". Do not silently rewrite the domain on routers in manual host mode. Each router stores its transport profile revision independently.

## WSS/TLS

- HTTPS public domain and port (typically 443), TLS ends at Nginx.
- Separate Nginx route /owrt-ws/ to loopback Xray 18444.
- Client VLESS+WS+TLS, SNI with checked HTTPS cert; server WS no TLS *on loopback only*.
- Old legacy TCP fallback preserved until individually migrated; do not rewrite any existing active router by default.

## REALITY/RAW (experimental, no production activation)

- Dedicated public Xray port or IP. Initial suggested alternative is 24443 to avoid the already occupied HTTPS/WSS 443 listener.
- Server requires an external target, allowed SNI serverNames, private X25519 key and allowed Short IDs.
- Client requires matching SNI, public key as realitySettings.password, a Short ID, and fingerprint.
- REALITY is not WebSocket+SNI. Current Xray docs support REALITY with RAW, XHTTP and gRPC, not WS.
- Beware: unrecognized REALITY handshakes are forwarded to target. Choosing a CDN target may make an abused port forward; design target validation and fallback rate limits.
- Require an isolated real two-node reverse test for both LuCI and SSH before marking a REALITY profile usable.

## Version pinning and upgrade policy

- Server core version and each OpenWrt core version are independent. Current and candidate versions must be visible together.
- Never default to unpinned latest or automatically update the fleet.
- Approved binary or package release and SHA256 verification; check native ELF architecture or OpenWrt opkg/apk arch and dependency metadata.
- Test candidate configs against each candidate core, then an isolated two-sided reverse connection. Do not treat syntax-only success as protocol compatibility.
- Explicit operator apply, full backup, transaction and watchdog rollback, per-router acceptance. Preserve old executable until confirmed.
- Existing AX3000T-Shteinberg remains on its old TCP transport until explicitly approved.

## Implemented offline development prototypes

- tools/orchestrator_setup_profiles.py: command-line domain/version setup wizard, schema and port conflict validation, private 0600 setup plan, WSS/REALITY paired *fragments*. They are marked NONDEPLOYABLE.
- tools/orchestrator_xray_core_stage.py: check extracted candidate ELF against trusted SHA256 and expected Xray version; run the supplied local config through Xray syntax validator; no installation or service changes.

Example for an **unused clean new host**, *not Belt*:

~~~sh
python3 tools/orchestrator_setup_profiles.py init \
  --domain hub.example.net \
  --hub-xray-version 26.10.21 \
  --router-xray-version 26.10.21 \
  --output /root/orchestrator-setup-plan.json
python3 tools/orchestrator_setup_profiles.py preview \
  /root/orchestrator-setup-plan.json
~~~

Example Xray core syntax gate (trusted release digest must be supplied
separately; sample text is deliberately NOT a valid digest):

~~~sh
python3 tools/orchestrator_xray_core_stage.py \
  --role hub \
  --candidate /root/xray-candidate/xray \
  --sha256 REPLACE_WITH_TRUSTED_64_CHARACTER_DIGEST \
  --version 26.10.21 \
  --installed /usr/local/bin/xray \
  --config /etc/xray/owrt-remote.json
~~~

Private REALITY keypair/Short ID must never enter the public repository
or be embedded in public install links. No script currently provisions
Nginx, DNS, REALITY, Xray updates, routers, or persistent GUI settings.
The GUI installer/upgrade lifecycle and real Xray/REALITY integration pilot
remain outstanding.

## Production guardrails

The proven narrow vps_host export hotfix is independent from these
features. Do not remove the maintenance hold, replace the Belt HUB with
the experimental branch, auto-generate Xray from a drifting database, or
migrate existing transports without individually confirmed rollback.
