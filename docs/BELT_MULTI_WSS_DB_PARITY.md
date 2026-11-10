# Belt multi-WSS SQLite reconciliation: DRY-RUN parity gate

Status (2026-11-10): **read-only simulation only; live DB reconciliation is
not authorized by this document**.

Operator's real Belt audit showed exactly six drift findings:
`main`, `WR3000S_2`, `PaxoBs` each have `vps_transport=tcp`
and `vless_port != 443` in the HUB DB while their router-side WSS/443
migrations have been confirmed. `Netis_N6_router5` already has
matching WSS state; `AX3000T-Shteinberg` remains TCP and explicitly
deferred. No per-router UUID, reverse-port or routing mismatches were
reported by the audit.

**Do not immediately update only the six reported fields.** The HUB
may regenerate Xray from its SQLite database. Its
`vps_allow_legacy` setting determines whether the existing
TCP/18443 registrations of newly WSS-migrated devices survive a
re-render. Therefore the safe migration must first reproduce the
**entire deployed** Xray JSON from a private, altered SQLite clone.
The existing upstream `tools/stage_belt_wss.py` has hardcoded
two-client WSS assumptions and is not suitable for this four-router
fleet without a separate generalization.

## Isolated simulation (no live changes)

Use the pinned `tools/belt_wss_db_reconcile_stage.py` from this branch
on Belt in a private root-owned path. It:
1. Takes a consistent online SQLite backup using the backup API, not a
   plain `cp` of the active WAL database.
2. Verifies all five active DB identities, their credentials against
   server TCP/WSS inbounds, LuCI/SSH reverse ports and routing.
3. Creates **two separate SQLite copies**. In both, only
   `main`, `WR3000S_2` and `PaxoBs` receive
   `vps_transport=ws`, `vless_port=443`. Scenario 0 sets
   `vps_allow_legacy=0` on these rows; scenario 1 sets it to 1.
4. Invokes the **installed** HUB Python renderer on these isolated
   DB copies with isolated state environment and private JSON output,
   and compares canonicalized configurations to the deployed Xray.
   Canonicalization only reorders inbound/client lists and distinct
   nonoverlapping routing selectors; Xray outbound order remains
   significant (the default outbound is positional).
5. For exact matches, validates the candidate using the installed Xray.
   Does not restart or signal any daemon, modify the live SQLite
   database or replace the live Xray JSON.

**Run from a pinned GitHub revision**, e.g.:
```bash
python3 /tmp/belt_wss_db_reconcile_stage.py \
  --db /var/lib/owrt-remote/hub.db \
  --live /etc/xray/owrt-remote.json \
  --hub-source /opt/owrt-remote/owrt-remote-hub.py \
  --xray-bin /usr/local/bin/xray \
  --migrate main WR3000S_2 PaxoBs \
  --existing-wss Netis_N6_router5 \
  --leave-tcp AX3000T-Shteinberg
```

The operator should inspect only the sanitized summary; staging
artefacts contain real credentials and must stay under a mode-0700
private root-owned directory. Never publish DB, Xray candidate or
generator error logs to GitHub.

`RESULT: STAGED MATCH` is a **necessary but not sufficient** gate
for a later reviewed, atomic production DB update with consistent
backup, lock, rollback and post-update no-restart checks. If both
variants differ, investigate generator support for the four-WSS fleet
and missing fields instead of changing production.

**AX3000T-Shteinberg must not be modified.** All existing reverse
connectivity, Xray/Nginx binds and old transport records stay in
place during staging. Docker HUB cutover is a separate later change.
