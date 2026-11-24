# Router Orchestrator: secure transport and core policy

## Target release contract (2026-11-24)

Transport family and security are independent concepts. The first stable
interoperability matrix has these five fixed wire profiles:

| Family | Security | Current profile ID | Router UCI |
| --- | --- | --- | --- |
| TCP (RAW) | TLS | tcp-tls | tcp-tls |
| TCP (RAW) | REALITY | reality-tcp | reality-tcp |
| WS | TLS | wss | ws |
| XHTTP | TLS | xhttp | xhttp |
| gRPC | TLS | grpc | grpc |

TCP/TLS is the proposed default for new direct connections; WS/TLS remains
supported for existing HTTP reverse-proxy ingress. XHTTP/REALITY is deferred
until tested. All TLS clients must verify the server certificate and SNI.
No downgrade to security=none is permitted in a newly staged profile.
Legacy `tcp` means plaintext for already enrolled routers; never reinterpret
that ID as TLS in an existing UCI or listener. Migrate one router at a time.

## What this patch actually changes

- TCP/TLS server and client renderers, including both router reverse links.
- Web transport configurator offers only the five secure profiles.
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
