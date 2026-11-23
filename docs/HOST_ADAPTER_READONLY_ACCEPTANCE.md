# Belt read-only host adapter acceptance

**Stage:** supervised host integration; **not** Router Orchestrator production cutover.

Earlier gates passed: source-exact Docker shadow, new Docker candidate on six routers,
single-writer preflight, and additive WSS enrollment preview on six existing
routers plus one synthetic (5 WSS, 1 legacy TCP). Host systemd HUB and dedicated
Xray remain active.

## Security boundaries

- The root-owned, pinned copy of the adapter is installed under
  /opt/router-orchestrator/host-adapter/host_adapter.py (mode 0600).
  It **never executes from the changing Git release checkout**.
- The opt-in systemd ExecStart specifies --peer-uid 23088 and no mutation flags.
  ProtectSystem=strict; only /run/router-orchestrator is writable.
- The Unix socket is root-owned, numeric GID 23088, mode 0660; the
  directory is root-owned, mode 0750. SO_PEERCRED verifies the UID
  **before any JSON is read**. A root peer (UID 0) is denied.
- Supported read-only requests: health, xray.status, host.metrics,
  xray.current. The latter contains the full Xray configuration for
  the authenticated UID, so the acceptance command never prints it:
  only the SHA256 is compared against the live file.
- A socket left after SIGKILL is removed only if it is owned by root,
  inactive, and its inode is unchanged during the check.
- No removal of the Xray maintenance hold, restart, apply or cutover.

## A. Pinned read-only preflight

On Belt, from the clean release checkout:

    cd /opt/router-orchestrator/release || exit 1
    EXPECTED="$(git rev-parse HEAD)"
    sudo python3 tools/orchestrator_host_adapter_readonly_acceptance.py \
      preflight --expect-commit "$EXPECTED"

Expected: HOST ADAPTER PREFLIGHT: PASS and unchanged production services.
This step is read-only.

## B. Explicit installation and live read-only acceptance

Only after review and authorization to start a new separate read-only service:

    cd /opt/router-orchestrator/release || exit 1
    EXPECTED="$(git rev-parse HEAD)"
    sudo bash tools/orchestrator_host_adapter_readonly_install.sh \
      --install-readonly "$EXPECTED"

The opt-in installer refuses pre-existing adapter files or unit, verifies the
Git commit and clean tree, copies the pinned code and reviewed unit, runs
systemd-analyze verify, daemon-reloads, and starts **only**
router-orchestrator-host-adapter.service. Acceptance checks:

1. Root peer denied; numeric UID 23088 accepted (SO_PEERCRED).
2. health, xray.status, host.metrics and xray.current work.
3. Xray SHA256 is unchanged and matches the socket response.
4. No write operation is sent; write flags are absent in the unit.

If acceptance fails, only the new adapter is stopped, not HUB or Xray.
The adapter is NOT enabled at boot. Files remain for inspection.

Because the adapter is pinned, changing the Git release branch will not
silently upgrade the running root code. A future upgrade needs a separate
review and explicit installation.

## Stop this adapter only

    sudo systemctl stop router-orchestrator-host-adapter.service
    sudo systemctl is-active owrt-remote.service owrt-remote-xray.service

Do not remove the existing maintenance hold, manually alter Xray JSON,
or start Python with --allow-xray-apply or --allow-xray-restart.

## Remaining release gates

- Prove read-only adapter on real Belt.
- Independently review write-capability Xray service: exactly one additive
  WSS router at a time, xray run -test, restart, rollback, and a separate
  explicit operator approval. Not available with the current unit.
- Test new-router WSS heartbeat, LuCI and SSH and keep all six old entries.
- Test VPS terminal parity, Push, and supervised single-writer Docker cutover.
