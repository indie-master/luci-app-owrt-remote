# Instructions for developers and AI agents

Read [approved specification](docs/MASTER_SPEC.md) first. It defines scope, production policy and release gates. Existing historical deployment docs are not approval to run live operations.

- Work on the current feature branch/PR #18; keep the existing worktree and PR. Do not merge/push main or publish a release without explicit approval.
- Allowed: source changes, CI, isolated staging, read-only production inspection. No production changes, router updates, transport switching, restart/reboot, external nginx, deletion or cleanup without specific approval. Do not touch external ssl-renewal, other services, system networking/firewall or other Xray instances.
- Reliability over feature count: preserve all working channels and existing rollback assets. Never bypass HOLD, weaken gates, or hide failures to make tests pass.
- Start with the audit and regression fixes; follow roadmap dependencies. Add regression tests, run actual CI, distinguish unit/static/synthetic/virtual/physical evidence. Never mark a stage accepted based solely on preview, mocks or a heartbeat.
- Keep changes small, traceable and reversible. Test against pinned versions. No unreviewed third-party binaries; verify official checksums and exact config. Use narrowly scoped host operations, never arbitrary browser-origin shell or Docker commands.
- No duplicate competing configuration sources, new permanent overlay stacks, or temporary production-only fixes. No secrets or live customer data in Git or logs.
- Preserve existing functionality except explicitly excluded scope and the approved phased Legacy retirement. Do not implement unrelated features.
- Report commit SHA, tests and their outcomes, real blockers, and whether staging/production was modified. Keep unverified physical acceptance and live deployment blocked until explicitly approved and tested.
- Make routine implementation decisions independently within the specification; document deviations and request approval only for scope/architecture or restricted live operations.
