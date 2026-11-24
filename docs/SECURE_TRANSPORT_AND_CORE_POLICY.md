# Router Orchestrator: secure transport and core policy

## Target release contract (2026-11-24)

Transport family and security are independent concepts. The first stable
interoperability matrix has these seven fixed wire profiles:

| Family | Security | Current profile ID | Router UCI |
| --- | --- | --- | --- |
| TCP (RAW) | TLS | tcp-tls | tcp-tls |
| TCP (RAW) | REALITY | reality-tcp | reality-tcp |
| WS | TLS | wss | ws |
| XHTTP | TLS | xhttp | xhttp |
| gRPC | TLS | grpc | grpc |
| XHTTP | REALITY | reality-xhttp | reality-xhttp |
| gRPC | REALITY | reality-grpc | reality-grpc |

TCP/TLS is the proposed default for new direct connections; WS/TLS remains
supported for existing HTTP reverse-proxy ingress. XHTTP/REALITY and
gRPC/REALITY are included in the renderers and require actual reverse trials. All TLS clients must verify the server certificate and SNI.
No downgrade to security=none is permitted in a newly staged profile.
Legacy `tcp` means plaintext for already enrolled routers; never reinterpret
that ID as TLS in an existing UCI or listener. Migrate one router at a time.

## What this patch actually changes

- TCP/TLS server and client renderers, including both router reverse links.
- Web transport configurator offers only the seven secure profiles.
- Host adapter and privileged stage worker reject new plaintext pilots.
- Old TCP listener and old enrollment schema remain unchanged for continuity.
- Persistent content-addressed Xray executable path is recognized and
  verified. Missing/corrupt explicitly pinned binary fails closed; no
  unrelated fallback is selected by automatic startup.
- Boot runtime acceptance verifies the actual executable against the UCI path,
  including the persistent digest-named binary.

## Mandatory remaining release gates

This is not authorization to deploy or migrate. The existing enrollment,
persistent transport promotion, fleet parity checks and UCI/LuCI transport
editor must be extended consistently before advertising secure-only
enrollment. No changes to current public listeners are implied by this patch.

Every candidate core must pass syntax and live reverse LuCI/SSH checks
on the actual architecture, with the selected profile. Reverse VLESS
support, TLS behavior, REALITY fields and XHTTP/gRPC implementation
vary by release. "Latest" is not a compatibility guarantee. Test both
reverse identities and authenticated LuCI; HTTP 403 alone is not a pass.

Core upgrades must be explicit, independent of transport changes and agent
updates. Download from a pinned release, validate the candidate version,
architecture, SHA256 and config, reserve space for rollback, preserve
the last known-good binary, and confirm only after reverse reachability.
Ordinary boot must not silently replace a working selected core. A
persistent full binary is an option for flash-rich devices, not a fleet
requirement. Smaller devices need a verified compressed boot cache with
a version/digest selection independent of the release asset filename.

Hardware pilot Netis_N6_router5: boot trial rolled back; Xray 26.11.9
softfloat copied to persistent storage and pinned by operator; config test
passed. Reboot and full new release acceptance have not yet been confirmed.
The old boot snapshot must not be reused after this pin because UCI and
runtime state changed. Rebuild a new snapshot from the actual current
configuration and preserve the persistent Xray executable across rollback.
Do not change the existing Belt production worktree or fleet listeners
for a client candidate test.

## Operator workflow and ingress automation

The operator chooses a friendly name, transport, security, dial hostname,
SNI, public port and (for HTTP transports) path or service name. The basic
form must hide raw streamSettings. Multiple REALITY serverNames and random
X25519 key pair / shortId generation are supported in the preview UI;
WebCrypto requires HTTPS and a supporting browser. A server-side key
generation action with owner/CSRF checks remains a release gate.

The profile catalog is independent of router deployment. Save as draft,
validate both cores and the selected certificate, provision a server
listener and ingress transaction, verify actual connectivity, then publish
an immutable revision. Only published revisions appear in router profile
selection. Saving or publishing must never change existing router UCI,
active runtime or credentials. Assign an explicit revision to one router.
A profile may not be retired while any active or fallback assignment needs it.

Prefer one dedicated SNI per listener with the common public port 443.
The SNI passthrough plan routes to a dedicated loopback Xray listener.
The nginx stream listener does NOT terminate REALITY or TLS in this mode;
the original shared HTTP vhost remains the default route. SNI routing
cannot distinguish HTTP paths or two transports with the same SNI and
public port. Such collisions need another hostname, a dedicated port, or
an explicitly modeled TLS-terminating HTTP ingress. REALITY always uses
passthrough; never send it to a TLS-terminating HTTP location. The REALITY
target must not point back into its own listener (loop detection required).
A third-party REALITY serverName is not a DNS record to create or a
certificate to issue; dial host and SNI are independent.

Host certificate inventory: explicit approved roots including the external
nginx certificate locations and Lets Encrypt archive/live directories.
Select by hostname/SAN and expiry, verify matching key. Dedicated
root-managed deployment directory under /opt/router-orchestrator/tls
mounted read-only in Xray/nginx is preferred to mounting the entire
host /etc. Use directory mounts (avoid stale individual-file bind mounts
after certificate rotation). Renew via existing host renewal hooks,
validate the renewed pair before atomic publication, then guarded reload
or restart where needed. No private key bytes in browser API responses,
logs or catalog. REALITY requires its key pair, not a local TLS certificate.
Issuance for new owned names needs DNS validation and operator-approved
ACME configuration. DNS record creation is optional and requires a
configured provider integration.

`secure_ingress.py` renders a pure ingress plan and detects supplied
SNI/port conflicts; `secure_certificate_inventory.py` validates an approved
certificate inventory; `secure_profile_catalog.py` stores metadata-only
immutable revisions and explicit desired preferences. These modules are
not yet wired to the live publication worker or router cards. No live nginx
configuration changes are performed by these helpers. Production publication
must inventory existing shared stream maps, default routes and socket
ownership, preserve unrelated Remnawave routes, validate staged nginx and
Xray configs, keep rollback artifacts, and use a single serialized commit.
A catalog digest is not by itself evidence that a listener is reachable.

Router card: display active profile, selected revision, heartbeat, reverse
SSH/LuCI status, Xray version and last successful connection. Provide
primary and up to two optional fallback selections. Keep small sealed
configs on persistent storage and run one Xray process by default;
fallback is a separate local state machine using consecutive failed
reverse-health probes, bounded retries, cooldown and explicit recovery
verification. Do not start multiple permanently running cores by default.
No fallback to plaintext, no automatic core upgrades, no global rollout.
Memory-constrained routers must be tested before enabling shadow processes.
Desired catalog selections are not active assignments until the actual
transport transaction confirms them.
