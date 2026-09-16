# Installation migration manifest

`harness.installation_manifest` is the Harness-side producer and consumer of
the versioned Installation Migration Manifest
(`docs/knowledge-platform/installation-migration-manifest.schema.json`, format
`agent-knowledge-platform-installation-migration/v1`). DISCOVERED and PREPARED
are derived from verified snapshot and orchestrator staging evidence. PREPARED
advances to CUTOVER once both writer journals commit their assigned revision 2
to the exact PREPARED bytes, or to ROLLED_BACK bound to caller-supplied
rollback evidence; CUTOVER advances to FINALIZED only by explicit command.
ROLLED_BACK to FINALIZED remains a future increment and rejects. No Knowledge
source is imported; Knowledge evidence enters only as the verified receipt
digests committed by the offline migration orchestrator's private staging or as
journal events already validated by the writer authority layer.

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

python -m harness.installation_manifest cutover \
  --output /absolute/private-manifest-dir/manifest.json \
  --harness-journal /absolute/private-harness-journal.json \
  --knowledge-journal /absolute/private-knowledge-journal.json

python -m harness.installation_manifest rollback \
  --output /absolute/private-manifest-dir/manifest.json \
  --rollback-evidence /absolute/private-rollback-evidence.json

python -m harness.installation_manifest finalize \
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
temporary recovery; unknown entries and symlinks reject. All commands print a
path-free JSON report and are fail-closed: on any rejection the CLI prints
`activation_allowed:false` and `installation_cutover_performed:false` and exits 1.

## CUTOVER

`cutover` advances a PREPARED manifest once both writer journals carry their
assigned revision 2. It is normally driven by `harness.cutover_orchestrator`,
which passes live authority-validated journals; the CLI form accepts both
journals as private JSON files. Each journal must contain exactly revisions
0-2: a rev1 suspension whose operation matches the rev2 assignment, a rev2 head
assigned to the correct new writer (`puddingharness`, or both Knowledge domains
`puddingknowledge`), no rollback evidence, and a self-consistent event digest.
Both heads must bind the same operation and the same
`migration_manifest_sha256`, which must equal the SHA-256 of the stored
PREPARED bytes — the manifest advances only if it is exactly what both journals
committed to.

The CUTOVER manifest flips `active_writers` (`session_harness` →
puddingharness, `knowledge_catalog`/`connector_jobs` → puddingknowledge),
registers both rev2 event digests in `checkpoint` as
`harness_assigned_event_sha256` and `knowledge_assigned_event_sha256`, and sets
`active_installation_revision` to `sha256:` + the committed PREPARED digest —
the same value both rev2 events carry (see the binding ruling in
`docs/distribution/cutover-orchestrator.md`). `rollback_window_open` stays
true, `completed_at` stays null, and credential rebinds, resource mappings and
all PREPARED evidence are carried unchanged. The re-encode is canonical and
byte-stable.

Retry is exact: a stored CUTOVER manifest is validated against the post-cutover
invariants (flipped writers, window open, preparation evidence intact, both
assigned events registered, committed revision bound), and both supplied
journals must still match the registered event digests and commitment, returning
`idempotent=true` with unchanged bytes; conflicting journals reject. Unlike
PREPARED, the CUTOVER state cannot be fully re-derived from staging evidence —
the registered commitments are re-verified against the live journals, while the
carried PREPARED fields rest on the 0600 file inside the 0700 directory.

## ROLLED_BACK

`rollback` advances a PREPARED manifest to ROLLED_BACK, binding
`rollback_evidence_digest` to the SHA-256 of the caller-supplied private
rollback evidence file. Active writers stay puddingclaw and the rollback window
stays open; the reverse-migration chain assembly that produces the evidence is
a separate increment. Retry with the same evidence returns `idempotent=true`;
different evidence rejects (`does not match`), and any other stored state
rejects. The rollback journal assignment (rev2, writer puddingclaw) is
performed by the writer authority layer against the ROLLED_BACK manifest, never
by this module. The full reverse-direction choreography — manifest advance,
both writer reassignments bound to the evidence digest, and the persistent
no-thaw freeze verification — is driven by `harness.rollback_orchestrator`
(see `docs/distribution/rollback-orchestrator.md`).

## FINALIZED

`finalize` advances a CUTOVER manifest to FINALIZED by explicit command only:
`rollback_window_open` becomes false and `completed_at` is stamped once and
carried verbatim on retry. The CUTOVER invariants are re-verified first, so a
manifest that never registered both assigned events rejects. Retry returns
`idempotent=true` with unchanged bytes. ROLLED_BACK → FINALIZED is not
specified for this increment and fails closed. `finalize` here validates the
manifest layer only; `harness.cutover_orchestrator finalize` additionally
requires the completed `both_thawed` cutover checkpoint bound to the same
manifest, so an installation is never finalized while a thaw is outstanding.

The advance reports keep `activation_allowed`, `installation_prepared`,
`installation_cutover_performed`, `rollback_completed` and
`writer_fence_verified` false: the manifest layer records state transitions but
performs no writer fencing, thaw or activation itself.

## What PREPARED is not

A PREPARED manifest is not installation readiness. The report keeps
`activation_allowed`, `installation_prepared`, `installation_cutover_performed`,
`rollback_completed` and `writer_fence_verified` false and
`credential_rebind_required` true. PREPARED records that a verified inactive
staging exists and is bound to a verified source snapshot. Per-domain
active-writer fencing and the auditable active-installation revision switch are
now performed by `harness.cutover_orchestrator` on top of a PREPARED manifest.
Still required by specification section 11.20: Knowledge-side object summaries
and old-ID to Resource URI mappings, credential rebind execution, remaining
Knowledge domains (their receipt evidence stays partial), rollback evidence
production, and the reverse-delta or snapshot-restore contracts that would let
`rollback_strategy` change. Until those increments exist, no source files are
modified or removed.
