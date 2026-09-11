# Offline generic settings import

`python -m harness.settings_import --source-snapshot /absolute/offline-copy --staging /absolute/private-settings-stage`

This installed-runtime command projects the source `config.json` into a private
inactive directory. It complements `harness.session_import`; each currently owns
its own staging directory. It is not the installation upgrade orchestrator and
must not be interpreted as installation PREPARED or CUTOVER.

Version 1 transfers `compression`, `cache`, `subagents`, `harness`, and
`write_middleware`. The actual runtime config validator rejects retired settings
and normalizes inherited empty rubric overrides. Sparse settings retain runtime
defaults and subagent replacement semantics remain unchanged.

Database, MCP, provider, credential, Knowledge and unknown sections remain in the
source snapshot. Only their count is reported. A separate explicit credential and
connection rebind is always required; copying a Postgres URL or MCP command could
silently reconnect the target to old writers. No vault/registry is imported, no
credential is rotated, and model references are not certified as rebound.

The staged config may contain private prompts: mode 0600 inside mode 0700 staging
is mandatory; do not publish it. The manifest contains only known section names,
counts and SHA-256 digests, never settings values or source paths. The source is
byte-preserved. Review untransferred settings in the original private snapshot.

Limits: 1 MiB config, 32 nested levels, object schema version 1; duplicate keys,
non-finite numbers, symlink/hardlink/nonregular inputs, overlapping roots, unexpected
staging content, source changes, payload tampering and forged manifests reject.
Private exclusive locking plus atomic fsync writes allow retry of an interrupted
copying checkpoint; completed payloads are immutable and checked on retry.

The caller must supply an offline snapshot. The staging lock is not a source
writer fence; repeated hashes do not prove cross-store consistency.
`activation_allowed=false`, `writer_fence_verified=false` and
`credential_rebind_required=true` remain explicit. Do not use staging as a live
Home and then resume its immutable import. Combined session/settings checkpoints,
activation, installation orchestration and rollback are still separate work.

`python -m pytest backend/tests/test_settings_import.py -q` validates real config
loading, connection exclusion, interruption/resume, tampering and CLI redaction.
