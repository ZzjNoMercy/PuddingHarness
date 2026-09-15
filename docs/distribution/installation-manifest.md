# Installation migration manifest (DISCOVERED/PREPARED)

`harness.installation_manifest` is the first Harness-side producer and consumer
of the versioned Installation Migration Manifest
(`docs/knowledge-platform/installation-migration-manifest.schema.json`, format
`agent-knowledge-platform-installation-migration/v1`). It covers only the
DISCOVERED and PREPARED states of the specification section 11.20 state machine.
CUTOVER, ROLLED_BACK and FINALIZED remain future increments: manifests carrying
those states validate structurally but are never reopened, advanced or
downgraded by this module. No Knowledge source is imported; Knowledge evidence
enters only as verified receipt digests committed by the offline migration
orchestrator's private staging.

```sh
python -m harness.installation_manifest discover \
  --source-snapshot /absolute/snapshot-envelope \
  --output /absolute/private-manifest-dir/manifest.json \
  [--credential-rebind 'slot|sha256:<64-hex>|credential://target/ref']

python -m harness.installation_manifest prepare \
  --source-snapshot /absolute/snapshot-envelope \
  --orchestrator-staging /absolute/migration-staging \
  --knowledge-receipt /absolute/private-receipt.json \
  --output /absolute/private-manifest-dir/manifest.json
```

Unlike the orchestrator's `--source-snapshot`, here the option always names the
verified `puddingclaw-source-home-snapshot/v1` envelope, never a raw payload:
the manifest must bind snapshot digests, and only the envelope carries them.

## DISCOVERED

`discover` admits the envelope read-only through `VerifiedSourceSnapshot` and
inventories only the session_harness domain (`harness.session_import` ROOTS,
`require_sessions=False`; empty `.lock` files are skipped exactly as the import
does). It writes a private DISCOVERED manifest:

- `source` binds the snapshot commitment: `installation_id` is the snapshot
  plan SHA-256, and `schema_revision`/`catalog_revision` are the snapshot
  contract revision `puddingclaw-source-home-snapshot/v1`. That contract is the
  only verified source revision evidence available to Harness; per-domain
  source schema/catalog revisions must be evidenced by the Knowledge side in a
  later increment and are not invented here.
- `object_summaries` contains exactly one measured `session_harness` entry
  (file count and a canonical SHA-256 over the import inventory). The
  `knowledge_catalog` and `connector_jobs` entries are omitted, not fabricated:
  Harness never reads the Knowledge catalog, and the Knowledge receipt carries
  covered/pending domain labels without object counts or content digests.
  Generic settings are likewise excluded from the count; their evidence rides
  in the orchestrator Harness plan digest registered at PREPARED.
- `id_resource_mappings` is empty: old-ID to Resource URI mappings are minted
  by target activation/CUTOVER, which this increment does not perform.
- `credential_rebinds` is empty, or exactly the caller-declared slots with
  status `pending` (`--credential-rebind`, repeatable). Harness cannot know
  source credential references, so undeclared slots are never synthesized;
  `credential_rebind_required` stays true.
- `active_writers` is `puddingclaw` for all three domains,
  `rollback_strategy` is `no_write_until_finalized`, `rollback_window_open` is
  true, and `snapshot_digest` binds the canonical snapshot commitment.
- `targets` records this backend (`puddingharness-backend@<version>`, mirroring
  `backend/pyproject.toml`).

`discover` is deterministic and byte-stable: retrying returns `idempotent=true`
with identical bytes. A stored manifest that differs from re-derivation, or
that already advanced past DISCOVERED, rejects; completed states never
downgrade back to DISCOVERED.

## PREPARED

`prepare` requires an existing DISCOVERED manifest (run `discover` first) and a
completed offline orchestration. It revalidates everything before advancing:

- The snapshot envelope is re-admitted and fully re-verified, including a final
  whole-payload rehash immediately before the manifest commit.
- The orchestrator staging must be private, complete and locked (the permanent
  `.orchestrator.lock` is held exclusively during verification, so a concurrent
  orchestration cannot mutate it mid-check). `plan.json` must recompute its own
  `plan_digest`, bind this envelope's payload identity and snapshot commitment,
  and match the staged request bytes. `checkpoint.json` must be exactly
  `verified_inactive_partial` with matching plan digest.
- The staged Harness Home manifest must be `verified_inactive`, recompute its
  plan digest, match the checkpoint's `harness_plan_digest`, and bind the same
  payload; every staged payload file is re-hashed against the plan, foreign
  staging entries reject, and a fresh source inventory must equal the plan's
  recorded session files.
- `--knowledge-receipt` is a private JSON file carrying the `knowledge_receipt`
  object printed by the orchestrator. It is revalidated with the orchestrator's
  own receipt rules (format, state, safety flags, request digest and source
  snapshot identity binding, and every Knowledge artifact re-hashed from the
  staging directory), and its canonical digest must equal the checkpoint's
  `receipt_digest`.

The PREPARED manifest then registers the three orchestrator digests in
`checkpoint`, adds the verified Knowledge release identity
(`package@version`) to `targets`, records `staging_namespace` as a path-free
SHA-256 of the staging path, stamps `started_at` once, and sets `state` to
PREPARED. `active_installation_revision` and `completed_at` stay null.

Retry is exact: a stored PREPARED manifest is fully re-derived from live
evidence and must match, returning `idempotent=true` with unchanged bytes.
`started_at` is the single carried field — stamped at the first PREPARED
transition and preserved verbatim; every other field is independently
re-derived, so any digest, state or structure drift fails closed. Replacing the
manifest with the byte-exact earlier DISCOVERED skeleton is treated as a
deterministic re-execution (same evidence, same commitments), matching the
orchestrator's replay semantics; only the stamped `started_at` is refreshed.
Caller-declared credential slots are committed at DISCOVERED and carried
forward; like every module's first private commit, their integrity rests on the
0600 file inside a 0700 directory, not on re-derivation.

The manifest file lives alone in a private directory with a dedicated permanent
flock (`.installation-manifest.lock`), atomic replacement, and validated stale
temporary recovery; unknown entries and symlinks reject. Both commands print a
path-free JSON report and are fail-closed: on any rejection the CLI prints
`activation_allowed:false` and `installation_cutover_performed:false` and exits 1.

## What PREPARED is not

A PREPARED manifest is not installation readiness. The report keeps
`activation_allowed`, `installation_prepared`, `installation_cutover_performed`,
`rollback_completed` and `writer_fence_verified` false and
`credential_rebind_required` true. PREPARED records that a verified inactive
staging exists and is bound to a verified source snapshot. Still required by
specification section 11.20 before any CUTOVER: per-domain active-writer
fencing and the auditable active-installation revision switch, Knowledge-side
object summaries and old-ID to Resource URI mappings, credential rebind
execution, remaining Knowledge domains (their receipt evidence stays partial),
rollback evidence production, and the reverse-delta or snapshot-restore
contracts that would let `rollback_strategy` change. Until that increment
exists, no source files are modified or removed and nothing is activated.
