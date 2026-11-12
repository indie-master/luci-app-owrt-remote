# Safe HUB export of OpenWrt reverse-client host (WSS/TLS)

Router Orchestrator / Remote HUB builds installation instructions through
`build_openwrt_config_payload()`. For Xray the `vps_host` UCI setting is
a hostname or IP address, **not** a URL. HTTPS links are valid only for
`hub_url` and `public_url`.

## Regression fixed

Some HUB records and web UI overrides use
`https://sprt-wrt.cdnvc.xyz`. Before this fix, **OpenWrt config** copied
that entire URL into `owrtremote.main.vps_host`. The agent's
`render-client` then rejected the value because it contains `://`.

The export now resolves the host from the active override, saved
`vps_host`, saved `public_url`, or HUB URL (in that order), without
scheme/port/path, and emits:

```sh
uci set owrtremote.main.vps_host='sprt-wrt.cdnvc.xyz'
uci set owrtremote.main.vps_sni='sprt-wrt.cdnvc.xyz'
uci set owrtremote.main.vps_port='443'
uci set owrtremote.main.vps_transport='ws'
uci set owrtremote.main.vps_ws_path='/owrt-ws/'
uci set owrtremote.main.hub_url='https://sprt-wrt.cdnvc.xyz'
uci set owrtremote.main.public_url='https://sprt-wrt.cdnvc.xyz'
```

This normalizes **only the generated OpenWrt UCI payload** in the
manual UI, SSH apply, and CLI print/export paths. It does NOT rewrite
stored SQLite values, touch existing installed router configurations,
regenerate Xray, alter Nginx, restart services, or enroll UUIDs on the
Belt. A new router must have both identities and both reverse routes
registered in the **live server Xray** before the client can connect.

## Verification and rollout

From a checkout of the candidate commit:

```bash
python3 -m py_compile vps/owrt-remote-hub.py
python3 -m unittest discover -s tests -p 'test_wss_transport.py' -v
python3 -m unittest discover -s tests -p 'test_host_policy.py' -v
```

Use a **private staged deployment/isolated HUB** for a config-export
preview. Avoid using `vps/install-vps.sh` as an in-place upgrader.
No authorization to replace the working Belt HUB Python source is
implied by this commit. The existing `.xray-maintenance-hold` must
not be removed by a code-export fix. Use the reviewed guarded Hub
upgrade/rollback procedure for production.
