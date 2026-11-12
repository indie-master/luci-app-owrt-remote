# Belt HUB: narrow production hotfix for OpenWrt `vps_host` export

The proven clean Movietonn router (Xiaomi AX3000T) required a manual
correction: the HUB emitted `uci set owrtremote.main.vps_host='https://...'`.
The remote agent rejects `://` in `vps_host`. The development source
on `feature/indie-branding` fixes this in the single shared config
payload builder. That source is larger and may contain other not-yet-
deployed work; replacing all of it on Belt would be unsafe.

Instead, `tools/belt_hub_host_export_hotfix.py` applies **only the
reviewed export-block substitution to Belt's installed Python file**.
It does not overwrite the installation with an unrelated branch
version.

### Safety gates

- Default invocation has no live modifications. Full SHA-256 checks
  before and after staging, a single exact match of the old code block,
  no ambiguous or already partially applied patch.
- Requires a pre-existing `/var/lib/owrt-remote/.xray-maintenance-hold`
  and healthy HUB and dedicated Xray services. Does not remove the hold.
- Candidate Python must compile and pass an isolated import smoke test
  proving `vps_host='sprt-wrt.cdnvc.xyz'` while preserving
  `hub_url` and `public_url` as HTTPS URLs.
- `--apply` backs up the installed source to a root-only staging
  directory, atomically replaces the **installed Python file only**,
  restarts **only** `owrt-remote.service` and checks `/health`.
- Xray JSON hash and Xray service MainPID must be unchanged. The script
  attempts to restore the original source and HUB service if the
  source-only deployment fails.
- No SQLite rewrite, no Xray service restart, no router update, no
  adjustment to the shared HUB bearer token.

### Operator procedure

Pin and download the script from a reviewed Git commit to Belt:

```bash
python3 -m py_compile /root/ro-host-hotfix.py
python3 /root/ro-host-hotfix.py
```

Expect `PRECHECK: PASS`, `SMOKE: PASS`, `READ ONLY`.
If preflight blocks because the installed HUB's source block is not
identical to the known version, stop. **Do not force replacement or
deploy the whole development branch.**

After a successful dry run, with a reviewed maintenance window:

```bash
python3 /root/ro-host-hotfix.py --apply
```

Expected: `APPLIED`, healthy HUB, unchanged Xray PID/hash, root-owned
source backup, maintenance hold retained. After applying, use the
authenticated `OpenWrt config` button for an existing router to
verify `vps_host` has no scheme. Do **not** paste that config into
an already connected router; merely inspect it.

This hotfix is independent of the future one-command customer
enrollment feature. Existing TCP router
`AX3000T-Shteinberg` must stay untouched.
