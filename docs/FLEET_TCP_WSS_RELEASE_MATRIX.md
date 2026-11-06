# Router Orchestrator — mixed TCP / WSS router fleet release matrix

**Cudy is the canary hardware, not a model-specific target or the fleet
configuration template.** The package is one shared client implementation
for OpenWrt routers with different hardware, RAM/flash capacities and
per-device transports. No TCP-only assumptions from `main` are allowed
in the shared client release path.

| Existing router state | Update behavior | Transport after update |
|---|---|---|
| Legacy TCP, no `vps_transport` UCI value | Preserve UCI, UUIDs, custom ports and the running Xray binary/config | Stays TCP |
| Explicit `vps_transport=tcp`, any valid configured port | Preserve individual port, reverse tags, SSH/LuCI bindings and Xray config | Stays TCP |
| Working WSS `vps_transport=ws`, correct SNI and `/owrt-ws/`-style path | Keep TLS SNI, certificate validation, WSS path, credentials and remote HUB routing | Stays WSS |
| Existing Xray is WSS but UCI still says TCP | **BLOCK** release on that router until its *existing* WSS settings are reflected in UCI, with a separately reviewed configuration step | Never silently replace WSS with TCP |
| UCI requests WSS but existing Xray is TCP | **BLOCK** rather than silently switching reverse transport during a client-version update | No automatic migration |
| New router with no existing Remote HUB installation | Separate fresh-install/enrollment path with unique HUB credentials | Defaults determined by newly issued HUB configuration; WSS supported |
| Missing optional client files | Create them when applying the bundle; keep an absence inventory so rollback deletes **only newly created** paths | Does not affect reverse transport |

The deployed `main` Cudy read-only inventory contains **6 present
and 4 absent** client files. The missing files are
`usr/lib/owrt-remote/wan.sh`,
`usr/lib/owrt-remote/logs.sh`,
`etc/init.d/owrt-remote-logs`,
`etc/hotplug.d/iface/99-owrt-remote`. The synthetic transaction test
`test_cudy_six_existing_four_missing_files_restore_exact_inventory`
now reproduces this precise state and verifies restoration of the
six originals and removal of the four introduced files.

## Compatibility acceptance before deployment

The **read-only generic** `tools/router_fleet_transport_check.sh
--inspect` is independent of Cudy's particular reverse port and router
ID. It checks both `vps-interconn` and `vps-ssh-interconn` against
the device's UCI transport. TCP requires TCP/none on both; WSS requires
WS/TLS, verified SNI and path, certificate verification enabled, and
HTTP/1.1 ALPN. No reverse UUID, private key, SNI, remote host or
raw configuration content is printed.

Use the independent transport checker **before** accepting a
router into the fleet rollout. It is not an installer or a migration
tool. A mismatch is a block, never an implicit change. The CI
`test_fleet_transport_parity.py` exercises both transport shapes,
mixed links, invalid WSS credentials/path/security flags and invalid
reverse-tag counts.

The older `tools/router_pilot_transaction.sh` and its guard retain
`router_id=main`, TCP/18443 **only as hardcoded pilot restraints**.
The current production-upgrade implementation is deliberately
disabled. A fleet updater must replace those with a per-router
identity/transport/port policy derived from *each router's* UCI
and independently checked against its actual Xray state; do **not**
remove restrictions from the pilot scripts merely to roll out fleet-wide.

The shared ten-file candidate bundle does not contain or overwrite
`/etc/config/owrtremote`, `/etc/xray/owrt-remote-client.json`, Xray
binary, UUIDs, hostnames, ports, routing lists, SNI or WSS path. The
agent code supports TCP and WSS simultaneously, but **updating the
client must not switch an already-working TCP router to WSS**.

## Release channel and fleet sequencing

As currently implemented, the original `install.sh` and
`vps/install-vps.sh` default to the earlier
`feature/wss-reverse-v119` source. Pushing commits to
`feature/indie-branding` does **not** update routers or change the
working Belt host. A controlled release must pin one immutable commit,
explicitly approve the client artifact and separately configure the
update source after the initial Cudy canary passes.

A normal `install.sh` is NOT approved for in-place bulk upgrades:
it installs files one by one and can reload management services
without a coordinated rollback. After a reviewed production
transaction is implemented, rollout should be sequential:
(1) Cudy canary; (2) a second recoverable TCP device;
(3) one WSS device; (4) other TCP and WSS devices individually;
(5) clean-device enrollment separately. Each router must pass
Belt-side SSH/LuCI reachability, transport continuity, unchanged
Xray process identity, and transaction confirmation. STOP on first
failure — no blind retry or forced restart of Xray.

No current changes authorize a real client upgrade, a WSS migration,
the existing legacy fleet “Update all” button, or changes to Belt HOLD.
