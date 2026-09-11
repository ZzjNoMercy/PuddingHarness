# Offline migration orchestrator

The installed Harness owns the combined checkpoint and sessions/generic settings.
An independently installed Knowledge process owns interpretation of an opaque
request, document conversion, artifact verification and its domain coverage.
No source-level cross-repository import is used.

```sh
python -m harness.migration_orchestrator \
  --source-snapshot /absolute/offline-claw-home \
  --knowledge-request /absolute/private-knowledge-request.json \
  --knowledge-python /absolute/knowledge-venv/bin/python \
  --staging /absolute/new-migration-staging
```

Use the Knowledge distribution's `migrate-from-claw-request/v1` specification to
produce the private request. Its Catalog and body roots must belong to the same
approved `--source-snapshot`; Knowledge verifies containment and returns the
snapshot root's identity. Harness checks that identity and the exact request
byte digest without interpreting Knowledge's request or Catalog schema.
The explicit executable is trusted local configuration; an ordinary virtualenv
Python symlink is supported without discarding the virtualenv interpreter path.

Staging contains the Harness candidate Home (`harness/payload`), Knowledge-owned
output (`knowledge/candidate`), the private request, immutable plan and combined
checkpoint. Retrying the same command revalidates both domains. Source and target
changes reject. The completed receipt commitment survives failed retries.
Timeout, oversized/malformed stdout, wrong digests and nonzero process exits
cannot complete the checkpoint. The child inherits the orchestrator lock, so
killing the parent cannot permit another migration while the old child is live.

The combined result is `verified_inactive_partial`, **not installation PREPARED**.
It records the Harness plan digest and Knowledge's verified receipt with its own
covered/pending labels. `installation_prepared`, `activation_allowed` and
`writer_fence_verified` remain false; `credential_rebind_required` remains true.
Snapshot creation and cross-domain consistency, compatibility versions, remaining
Knowledge domains, credential rebind, active-writer CUTOVER and complete rollback
are still required by specification section 11.20. The caller must keep the
supplied offline snapshot immutable. No old source files are removed or activated.
