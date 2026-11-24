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


## Catalog integration

The Docker HUB exposes an owner-only catalog at /secure-profiles/ with links
from router cards. Mutating endpoints require the transport action CSRF
token and check the request origin. The host adapter stores immutable draft
artifacts in the systemd-managed root-private StateDirectory
/var/lib/router-orchestrator-secure-profiles (0700), profiles.db (0600).
Back up this database as a separate sensitive artifact, using SQLite's
backup API, alongside the existing HUB state. The HUB does not mount it.
Catalog responses contain metadata only.

The primary workflow is save draft, then validate the saved revision.
Empty REALITY key fields generate X25519 on the host using OpenSSL; supplied
pairs must match, and an empty shortId list generates a random 8-byte ID.
TLS drafts can automatically select an already-mounted matching certificate
from the dedicated TLS directory (root or one immediate child); both
explicit paths must be supplied otherwise. The saved artifact digest is
checked before validation. Validation checks the active Xray image and,
for TLS, the certificate name, key and expiration. It does not validate
external nginx ownership, create DNS records or activate listeners.

Per-router selection records desired primary/fallback revisions only.
There is intentionally no public publish API until a verified server
publication transaction exists. A draft cannot be assigned; validation
does not publish it. Existing router runtime remains untouched.
The new StateDirectory and host adapter require an approved service upgrade;
do not change the pinned production worktree to test this UI.


## Publication planning and recovery engine

`tools/inspect_secure_ingress.py` reports the current external nginx disk
configuration digest, SNI map hints, include locations, occupied TCP ports
and Xray listener metadata. It prints neither certificate keys nor raw
nginx/Xray configurations. This is a read-only onboarding aid (nginx -T
tests the config; no reload). It cannot infer ownership from arbitrary
includes, wildcard or regex rules. An operator-reviewed ownership inventory
is required at /var/lib/router-orchestrator-secure-profiles/ingress.json
(mode 0600, root-owned). Its exact nginx -T stdout digest must match before
and after planning. A matching digest describes files on disk, not proof
of the running nginx configuration.

The owner-only catalog action "План публикации" builds a candidate adding
one empty protected VLESS listener on loopback, preserves all baseline
Xray configuration and existing managed routes, rejects unapproved SNI,
port conflicts and obvious recursive REALITY targets, and validates the
exact candidate with the running Xray image. It returns metadata only.
There is no publish/apply endpoint yet.

The two-file publication engine saves both original files and modes,
records an fsynced recovery journal before replacing either file, and
requires explicit activation and runtime acceptance callbacks. If
acceptance fails it restores both originals; a crash leaves a recovery
journal. It refuses to overwrite unrelated concurrent edits. Its tests
use temporary files and stub runtime callbacks. Production integration
requires the shared Xray lifecycle lock, root-owned managed nginx include,
safe activation and independently supervised recovery, live authenticated
canary and baseline reachability checks. DNS aliases and REALITY target
loop detection still require runtime checks. Do not enable publication
until those gates are met. No production files or devices are mutated
by the new planning endpoint.


## Belt onboarding and draft reservation

Belt onboarding at b94cb3b completed with 53 existing SNI mappings,
55 reserved names (including HTTP vhosts), outgoing PROXY protocol on,
an empty /etc/nginx/router-orchestrator-managed.map and no service reload.
Default route 127.0.0.1:8443 and legacy listeners remain unchanged.
This is disk onboarding, not live publication.

New drafts use automatic loopback backend ports 24000–24999 by default
(port 0 in the form). Explicit ports are checked against live TCP sockets,
Xray listeners and all stored revision reservations. SQLite BEGIN IMMEDIATE
serializes selection and insertion; the reservation does not bind a socket.
Unused draft reclamation and in-place revision replacement are not yet
implemented. Before applying, recheck live port ownership under the common
publication lock.

The owner can explicitly approve the exact SNI names of a sealed draft
from the catalog. The approval only extends the reviewed private inventory
after checking the current nginx digest, reserved routes, public port and
live socket occupancy. No DNS record is created, no nginx file is changed,
and no listener or router is activated. The future publication worker must
also hold ingress.lock, revalidate ownership and verify REALITY target
resolution (including self-referential aliases) before any activation.
PROXY protocol is emitted only on server inbounds when the reviewed
external listener sends it. Actual core/transport interoperability remains
a live acceptance requirement.


## Existing certificate import and isolated wire rehearsal

Saving a TLS draft first tries the dedicated TLS mount, then imports an
existing matching certificate and key from literal nginx -T directives or
standard Certbot lineages in approved source roots. Browser-supplied source
paths are not used by the importer. Imported pairs are validated, stored in
content-addressed immutable subdirectories (directory 0700, key 0600),
and referenced using the Xray container mount. Existing slots are not
replaced. No ACME issuance, automatic renewal or deployment restart is
performed by this importer. A future renewal operation must validate and
activate a new pair without disrupting unrelated profiles. The host
adapter service now requires the dedicated TLS directory to exist and
allows writes there.

The operator-only tools/rehearse_secure_reverse.py performs isolated
wire tests with the exact locally running Docker Xray image (both server
and synthetic client), a separate nginx process, temporary loopback
listeners, independent random test UUIDs and two independent random
nonce responses. It uses the existing certificate for TLS, and requires
an explicit target for any REALITY profile. The nginx stream harness
includes outgoing PROXY protocol by default, matching Belt. It checks
both reverse channels instead of accepting a TCP connect or TLS handshake
as proof. It requires at least 512 MiB available RAM and bounds each test
container to 192 MiB. Normal exit and handled signals clean up temporary
processes and files. SIGKILL or abrupt host failure may leave test
containers; remove only specifically identified ro-probe containers.
An imported certificate slot persists; the production Xray configuration,
existing nginx routing, HUB and router UCI are not intentionally changed.

This is a rehearsal, not server publication or actual OpenWrt acceptance.
Core compatibility on MIPS, production ingress/DNS reachability and
authenticated real LuCI/SSH still require separate acceptance. The
production publication worker must use the common lifecycle lock,
independent recovery and validated durable inventory/metadata commit;
no live publish endpoint is enabled yet.


## Belt TLS wire rehearsal acceptance (2026-11-24)

Operator output for c50d804 confirms 30 local regression tests passed.
The production image Xray 26.3.27 (d2758a0, linux/amd64) completed both
reverse nonce proofs on each of TCP/TLS, WS/TLS, XHTTP/TLS and gRPC/TLS,
through isolated nginx with outgoing PROXY protocol. Existing certificate
import for sprt-wrt.cdnvc.xyz succeeded. Production HUB/Xray IDs and
configuration hashes were unchanged, nginx syntax and HUB health passed,
and no probe containers remained. Production Xray currently has no TLS
mount; the isolated tests mounted TLS independently. The production mount
must be included in the eventual controlled server upgrade.

REALITY trials now preflight the explicit target using verified TLS 1.3
and h2, with the selected SNI. This is a deliberately conservative gate
for the three REALITY profiles, not proof of REALITY compatibility.
REALITY wire tests and OpenWrt hardware acceptance remain outstanding.
No profile is published or assigned by these tests.


## Belt full secure wire matrix acceptance

Operator output for 93811cc confirms the remaining 30 local tests and
all three REALITY dual reverse rehearsals (TCP, XHTTP and gRPC) passed
on the exact deployed Xray 26.3.27 image. The target swiftlessvpn.ru
negotiated verified TLS 1.3 and h2. The combined isolated server matrix
is therefore seven of seven passing. The production container identities,
Xray and nginx config hashes, nginx syntax and HUB health were preserved;
probe containers were removed. This does not authorize using an existing
HTTP vhost SNI as a new live stream route or prove OpenWrt interoperability.

## Publication commit and concurrency

The host adapter's direct restart and legacy enrollment mutation now
acquire the shared Xray lifecycle lock and recheck the maintenance hold.
The file transaction supports a durable FILES_ACCEPTED decision and an
idempotent forward-only metadata finalizer. Before this decision failures
restore both original files. After it, recovery must not roll back files
that might already be visible in the catalog. The finalizer checks the
sealed artifact, exact installed inbound/map bytes, prevalidated complete
nginx dump digest and explicit runtime evidence; writes the updated
inventory first and commits catalog visibility last. Recovery tests cover
interruption before and after the database commit. No router preferences
are modified.

The external nginx candidate can be tested using an isolated private mount
namespace, temporarily overlaying only the managed include within that
namespace. This supplies a complete candidate nginx -T digest without
changing the host file or reloading nginx. The host worker must have
mount-namespace privileges; do not add CAP_SYS_ADMIN to the HUB adapter.
The current test suite mocks namespace execution, so a real Belt rehearsal
of this preflight is required.

The production activation/recovery worker, shared ingress lock integration,
actual listener acceptance, and protection of published REALITY secrets
in the legacy full-config adapter API remain release gates. Existing
production Xray lacks the required TLS mount. No live publish endpoint
has been enabled, and no production profile or router has been changed.


## External-stream publication worker (first operator-only implementation)

Belt 5f01bde passed all 20 publication regression tests and the real
namespace nginx preflight; baseline and live services remained unchanged.

The root-only secure_publication_worker.py now requires the installed
production revision, active recovery timer, exact draft digest, explicit
maintenance confirmation, exact Docker image/container and read-only
Xray config/TLS directory mounts. It checks every recently online router's
heartbeat, SSH banner and LuCI HTTP reachability, not just 80 percent.
It checks the reviewed nginx digest, candidate Xray syntax and TLS pair
(or REALITY target at 443). The first REALITY live implementation rejects
target addresses overlapping the dial hostname or local interfaces; this
is a conservative recursion gate, not proof against all DNS changes.

For live acceptance the transaction temporarily installs two independent
random reverse credentials and private loopback portals on the new inbound.
A test client connects through the configured public hostname/port and
proves two independent nonce responses for each selected SNI. The worker
then removes the probe, installs the final empty inbound and checks the
fleet again. This entails two Xray restarts and nginx reloads; a failed
trial can entail a third restart during recovery. No existing router
configuration is changed. The final catalog commit is forward-only after
FILES_ACCEPTED; actual router enrollment remains a separate feature.

Recovery uses the same lifecycle and ingress locks and a durable maintenance
hold. The independent timer retries incomplete transactions or forward
metadata commits. The core watchdog skips this hold; shadow rollback cannot
remove another transaction's hold. The worker refuses to reload unrelated
changed nginx files and retains recovery state for operator review.
The legacy config API masks server REALITY private keys and transient
proof identities; unchanged keys are hydrated only inside the host adapter.
Core validation now mounts the dedicated TLS directory when present.

The production worker and timer are not installed or enabled by these
commits. They require real deployment rehearsal and fault-injection tests.
The Web publish job endpoint is intentionally not enabled yet. Current
Belt lacks the production TLS mount; do not run the live publish command
from a server-candidates checkout or bypass the mount gate.


## Web publication queue and existing deployment upgrade

The owner-only catalog now exposes a CSRF-protected publication job action
after planning a sealed draft. A fixed privileged systemd job consumes one
root-private job record. The browser supplies only the profile ID, revision,
artifact digest and explicit maintenance confirmation. The job cannot run
unless the root-private web-publication.enabled file contains exactly
"enabled" followed by a newline. The marker must remain absent until
production upgrade and controlled live acceptance are complete. No profile
is assigned to a router by publication. UI shows the recorded job status;
the owner can refresh status and reload the catalog after completion.
Ambiguous or interrupted jobs block subsequent requests. Operator-only
secure_publication_job.py reconcile verifies that the unit is inactive,
the maintenance hold is absent and the transaction is terminal before
clearing the queue gate; success additionally requires the matching
published revision and accepted journal.

deploy/prepare_active_upgrade.py is for the existing active unified stack,
not the first installation. It builds a candidate HUB image from the
detached server-candidates checkout and retains private env, Compose,
Xray configuration, SQLite online backups and existing unit snapshots.
It preserves the active Xray image reference and reports whether a
recreation is required for the dedicated TLS mount. It refuses a dirty or
unrelated candidate and checks that the active source, containers,
environment and Xray configuration are unchanged after staging.

This is preparation only. A controlled active-stack cutover with runtime
fleet acceptance and rollback still must be implemented and tested.
Do not enable the publication marker or run the original initial
apply_belt.py on the already-active stack. The old runtime Xray has no TLS
mount, so the eventual upgrade needs one planned Xray recreation while
keeping the exact core image. Real live publication and hardware acceptance
remain outstanding.
