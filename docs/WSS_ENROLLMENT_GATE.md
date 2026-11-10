# Belt WSS enrollment gate — Router Orchestrator

Successful pilot: WR3000S_2 changed from TCP/18443 to WSS/443 on
2026-11-10 only after the dedicated Belt Xray WSS inbound received the
router's existing two VLESS identities. Before enrollment, TCP inbound had
8 IDs; WSS inbound only had Netis_N6_router5's 2 IDs. TLS/WebSocket 101
alone did not prove VLESS authentication. After adding the two IDs and
restarting only owrt-remote-xray.service, LuCI returned HTTP 200 and SSH
returned SSH-2.0-dropbear; the router confirmed WSS.

Server preparation, **one router at a time**:

1. Identify the precise router ID from existing inventory (not a model
   name guess), its two TCP VLESS identities and destination WSS host.
2. On Belt run `bash tools/belt_wss_enroll.sh --router-id ROUTER_ID`.
   Default mode validates without changing the config or printing UUIDs.
3. After review, run `bash tools/belt_wss_enroll.sh --router-id ROUTER_ID --install`.
   A timestamped backup is created; only the two matching IDs from TCP
   are added to the WSS inbound. Xray is tested but **not restarted**.
4. Schedule a controlled restart of dedicated
   `owrt-remote-xray.service`. This momentarily disconnects all remote
   reverse channels. Verify all existing links, both Netis WSS sessions,
   and LuCI/SSH before proceeding.
5. Stage a WSS candidate on that router, with explicit WSS hostname
   (`sprt-wrt.cdnvc.xyz` for the current Belt), correct SNI/path and
   per-router TCP rollback port. No global UCI overrides.
6. Use guarded `apply`; verify LuCI *and* SSH from Belt; then confirm
   within 300 seconds. Failure must roll back to that device's TCP state.
7. STOP on the first failure. Never run `apply` on already confirmed WSS
   devices (WR3000S_2, Netis_N6_router5).

This is **not** authorization for an automatic fleet-wide upgrade. The
legacy "Update all" path remains on HOLD. The preflight does not upload
UUIDs from the router or change running processes by itself: it strictly
copies identity records already registered in the Belt TCP inbound.
