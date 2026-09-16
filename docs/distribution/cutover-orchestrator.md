# CUTOVER orchestrator

`harness.cutover_orchestrator` performs the specification section 11.20 CUTOVER:
the two-phase commit that reassigns all three writer domains from puddingclaw
to the new products and retires both freeze markers. It consumes the writer
suspension barrier's end state and the PREPARED installation migration
manifest. Rollback (reverse-migration chain assembly, ROLLED_BACK journal
assignment) is out of scope for this module; the manifest-level ROLLED_BACK
advance lives in `harness.installation_manifest`.

```sh
python -m harness.cutover_orchestrator cutover \
  --harness-home /absolute/private/harness-home \
  --knowledge-state /absolute/private/knowledge-workspace \
  --knowledge-python /absolute/independent-knowledge/.venv/bin/python \
  --checkpoint-dir /absolute/new-private/cutover-checkpoint \
  --manifest /absolute/private-manifest-dir/manifest.json \
  --operation-id cutover-1

python -m harness.cutover_orchestrator finalize \
  --manifest /absolute/private-manifest-dir/manifest.json \
  --checkpoint-dir /absolute/new-private/cutover-checkpoint
```

## Preconditions

Every precondition is re-verified on every run before the first commit:

- Both writer journals are suspended (rev1) under exactly this
  `--operation-id`, as committed by `harness.writer_barrier`. A suspension
  belonging to another operation, an unsuspended writer, or a foreign
  assignment rejects before anything is committed.
- The manifest is a private file in its own private directory, validates
  against the installation migration manifest contract, and is in PREPARED
  state with all pre-cutover invariants (all writers puddingclaw, rollback
  window open, no minted resource URIs, rebinds pending).
- The manifest digest equals what both journals validate: each assignment is
  committed against the byte-exact PREPARED manifest, and every resume
  re-checks the recorded digest against the live file and both live journals.
- Harness Home, Knowledge workspace, checkpoint directory, manifest directory
  and both authority roots are pairwise disjoint.

Knowledge is invoked only through the explicitly selected independent
interpreter (`-m knowledge_platform.local.writer_authority`), with the same
contract as the barrier: scrubbed environment (`PATH`/`HOME` only), bounded
stdout, the checkpoint lock passed down so a dead parent cannot admit a second
coordinator, and executable plus installed-release identity re-verified before
and after every delegation.

## Two-phase commit

Checkpoint states, in order, each committed atomically before the next step
starts:

1. `harness_assigned` — the Harness rev2 `assigned` event commits
   (writer `puddingharness`), bound to the PREPARED manifest digest.
2. `both_assigned` — the delegated Knowledge CLI commits its rev2 `assigned`
   (both domains `puddingknowledge`) to the same PREPARED digest; the Harness
   journal is re-verified unchanged.
3. `manifest_cutover` — `harness.installation_manifest.cutover_installation`
   advances the manifest PREPARED → CUTOVER: `active_writers` flips
   (`session_harness` → puddingharness, `knowledge_catalog`/`connector_jobs`
   → puddingknowledge) and both rev2 event digests are registered in
   `checkpoint` as `harness_assigned_event_sha256` /
   `knowledge_assigned_event_sha256`.
4. `active_pointer_published` — `active-installation.json` is published in the
   Harness Home root (see below).
5. `harness_thawed` — Harness thaw retires its freeze marker against the
   preserved PREPARED bytes.
6. `both_thawed` — the delegated Knowledge CLI thaw retires its freeze marker;
   the retired marker and thaw receipt are re-verified (receipt fields, retired
   marker digest) before completion is committed with
   `installation_cutover_performed: true`.

Both thaws run only after the manifest is CUTOVER and the pointer is published.
Thaw binds the PREPARED bytes, not the advanced CUTOVER bytes: the journals
committed to the PREPARED digest at assign time, so the orchestrator preserves
the exact PREPARED manifest as `prepared-manifest.json` inside the checkpoint
directory and passes that copy to both assign and thaw commands. Passing the
advanced CUTOVER file to thaw rejects by design.

## Active-installation revision binding (ruling)

The design left the `active_installation_revision` value open; this
implementation pins it:

- Both rev2 `assigned` events carry `active_installation_revision` =
  `sha256:` + the SHA-256 of the PREPARED-state manifest bytes both journals
  committed to at assign time (`migration_manifest_sha256` carries the same
  digest without the prefix).
- The CUTOVER manifest records that same value in its own
  `active_installation_revision` field, so manifest and journals agree.
- The `active-installation.json` pointer (format
  `puddingharness-active-installation/v1`) binds: the CUTOVER-state manifest
  digest (`cutover_manifest_sha256`), both rev2 assigned-event digests, the
  PREPARED digest they committed to (`prepared_manifest_sha256`, plus the
  prefixed form in `active_installation_revision`), the operation id and the
  post-cutover `active_writers` map.

The design document does not pin the pointer's location. This implementation
publishes it in the **Harness Home root** (`active-installation.json`, private
0600, atomic replace, directory fsync): the Home is the installation root every
runtime admission path already inspects, and no Home directory hygiene rule
restricts entries. The pointer is compare-or-publish: existing different bytes
reject, existing identical bytes resume.

## Exact retry and crash windows

The checkpoint plan binds both enrollments, the checkpoint directory identity,
the manifest directory identity, the operation id and the selected Knowledge
interpreter and release identities; a changed plan rejects. A completed
checkpoint never downgrades: resume re-derives every step and compares against
the recorded journals, digests and receipts. Crash windows and their resume:

| Crash point | Observable state | Resume |
| --- | --- | --- |
| Before `harness_assigned` | Both suspended (or Harness rev2 already committed); manifest PREPARED | Assign is idempotent; checkpoint commits |
| After `harness_assigned` | Harness rev2, Knowledge suspended; manifest PREPARED | Knowledge assign commits (or is found committed) |
| After `both_assigned` | Both rev2; manifest PREPARED or already CUTOVER | Advance validates both journals against the stored manifest and commits |
| After `manifest_cutover` | Manifest CUTOVER; pointer maybe published | Pointer compares or publishes |
| After `active_pointer_published` | Both still frozen | Thaws run; Harness thaw is exact-retry |
| After `harness_thawed` | Harness thawed, Knowledge frozen | Knowledge thaw is exact-retry; receipt and retired marker re-verified |
| After `both_thawed` | Complete | Rerun returns the identical result with unchanged checkpoint bytes |

## Failure matrix

| Failure | Effect | Required action |
| --- | --- | --- |
| Unsuspended writer, suspension under another operation | Rejects before any commit; no checkpoint | Run the writer barrier with the same operation id |
| Knowledge assignment exists without a checkpoint | Rejects (`Knowledge assignment has no cutover checkpoint`) | Inspect the Knowledge journal; never auto-repaired |
| Manifest digest drift (file changed, journals committed elsewhere) | Rejects before or at the assignment step | Restore the exact PREPARED bytes |
| Delegated CLI failure or invalid receipt | Checkpoint stays at the last commit; the Knowledge journal may legitimately hold rev2 (child committed) | Fix the interpreter/installation; rerun the exact command |
| Interpreter or installed-release identity change | Rejects; partial state stays suspended/frozen | Restore the exact trusted installation; rerun |
| Changed checkpoint directory, plan, journals, pointer or thaw receipts | Rejects; completed checkpoints never silently change | Restore the committed evidence |
| Manifest tampering after a committed checkpoint | Rejects (`manifest changed` / advance mismatch) | Restore the committed manifest bytes |

No automatic rollback of a partial cutover is attempted: any crash leaves at
least one product suspended and frozen, so no window exists where both products
believe they are the writer.

## finalize

`finalize` is a separate explicit command, never part of the cutover run. It
requires the completed `both_thawed` checkpoint bound to the same manifest
(both registered rev2 digests and the prepared commitment must match), then
advances the manifest CUTOVER → FINALIZED: `rollback_window_open` becomes false
and `completed_at` is stamped once. Retry returns `idempotent=true` with
unchanged bytes. ROLLED_BACK → FINALIZED is a future increment and rejects.

## Fail-closed flags

- `installation_cutover_performed`: false on every intermediate checkpoint and
  on any error; true only in the committed `both_thawed` state and in the
  `finalize` report (which verifies the completed checkpoint).
- `rollback_completed`: always false; this module performs no rollback.
- `production_activated`: always false. CUTOVER reassigns writer authority;
  production/release activation remains a separate acceptance gate.
- `activation_allowed`: always false in orchestrator output. The orchestrator
  itself grants no runtime authority; the journals and the per-product thaw
  receipts are the authority.

On any rejection the CLI prints `error_code: installation_cutover_rejected`
with all four flags false and exits 1.

## Tests

```sh
KNOWLEDGE_TEST_PYTHON=/absolute/knowledge/.venv/bin/python \
  python -m pytest tests/test_cutover_orchestrator.py
```

The installed tests drive the real Knowledge writer-authority CLI: happy path
with exact retry, crash injection at every checkpoint, delegation failure and
invalid-receipt injection, and each precondition violation. Without the
explicit interpreter the tests skip; a skip is not proof of the cross-product
contract. Manifest-level advance tests
(`tests/test_installation_manifest_advance.py`) run without Knowledge.
