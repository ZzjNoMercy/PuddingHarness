# Unified offline Harness Home checkpoint

Run from the installed Harness Python environment:

```sh
python -m harness.home_import --source-snapshot /absolute/offline-copy --staging /absolute/private-stage
```

The resulting `payload/` is a single inactive candidate Home containing session
files, session-owned attachments/results/scratch/rewind, and projected
`config.json`. The source snapshot is read only. Neither the current Home nor any
active installation pointer is changed. This complements the per-domain import
commands with one checkpoint that binds both domains to the same source path and
content digests. It does not call Knowledge code or access its stores.

The manifest binds the source config digest (including absence), the session file
inventory, the projected settings digest, and the complete candidate inventory.
Before marking verified_inactive, both source domains and every candidate file are
verified again. A changed source, mixed-generation retry, modified completed
payload, missing file, unexpected staged file, or forged control flag rejects.
SIGKILL during session copying can resume before settings copy; no domain-level
success is treated as combined completion. Empty session inventories and missing
source config are valid, using the same sparse config defaults as a new install.
The session-only command retains its existing requirement for session files.

Only the five generic sections described in `harness-settings-import.md` are
projected. Database/MCP/provider/credential and Knowledge data are not copied.
Rebinding is still required before use. The candidate can contain private prompts
and conversation content: keep staging private. CLI output contains counts,
known section names and plan digest, not source paths or values. The private
manifest includes relative session filenames, sizes and digests, but no contents.

Use the same offline snapshot path and staging path to resume. Do not modify the
candidate, use it as a live Home, or reset its manifest during resume. Source and
candidate remain separate and the original is never deleted.

The staging lock serializes imports; it is not a source writer fence. Hash
rechecks detect mutations but cannot establish transactional consistency across
live stores. The caller must provide an offline snapshot. The output therefore
keeps activation_allowed=false, writer_fence_verified=false and
credential_rebind_required=true. It is not the spec's installation PREPARED state.
Actual source freezing, version compatibility, Knowledge migration delegation,
active-writer revision switching and rollback remain installer work.

Validation includes real config and SessionManager reads, source mutation in
both domains, empty install, tampering, and real process-kill/CLI resume:

```sh
python -m pytest backend/tests/test_home_import.py backend/tests/test_settings_import.py backend/tests/test_session_import.py -q
```
