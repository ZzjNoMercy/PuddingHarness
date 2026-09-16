# ROLLED_BACK orchestrator

`harness.rollback_orchestrator` performs the specification section 11.20
reverse direction in two forms:

- `rollback` (abort path): the two-phase rollback that returns all three
  writer domains to puddingclaw after a migration is abandoned before CUTOVER.
  It consumes the writer suspension barrier's end state, the PREPARED
  installation migration manifest, and a private rollback evidence file
  produced out-of-band by the Knowledge reverse chain.
- `window-rollback` (post-cutover window path, section 11.20 point 5): the
  rollback of a completed cutover inside the installation rollback window,
  under a NEW operation. Both writers are re-suspended at rev3, the CUTOVER
  manifest advances to ROLLED_BACK bound to the post-cutover rollback evidence
  digest with active writers flipped back to puddingclaw, and both writers are
  assigned back to puddingclaw at rev4.

The reverse-migration chain assembly that produces either evidence file is a
separate increment; this orchestrator binds the evidence by digest only.
Forward cutover lives in `harness.cutover_orchestrator`; the manifest-level
ROLLED_BACK advance lives in `harness.installation_manifest`.

```sh
python -m harness.rollback_orchestrator rollback \
  --harness-home /absolute/private/harness-home \
  --knowledge-state /absolute/private/knowledge-workspace \
  --knowledge-python /absolute/independent-knowledge/.venv/bin/python \
  --checkpoint-dir /absolute/new-private/rollback-checkpoint \
  --manifest /absolute/private-manifest-dir/manifest.json \
  --rollback-evidence /absolute/private-evidence-dir/rollback-evidence.json \
  --operation-id rollback-1

python -m harness.rollback_orchestrator window-rollback \
  --harness-home /absolute/private/harness-home \
  --knowledge-state /absolute/private/knowledge-workspace \
  --knowledge-python /absolute/independent-knowledge/.venv/bin/python \
  --checkpoint-dir /absolute/new-private/window-rollback-checkpoint \
  --manifest /absolute/private-manifest-dir/manifest.json \
  --rollback-evidence /absolute/private-evidence-dir/window-rollback-evidence.json \
  --operation-id window-rollback-1
```

## Preconditions

Every precondition is re-verified on every run before the first commit:

- Both writer journals are suspended (rev1) under exactly this
  `--operation-id`, as committed by `harness.writer_barrier`. A suspension
  belonging to another operation or an unsuspended writer rejects before
  anything is committed. An `assigned` rev2 in either journal without this
  orchestrator's checkpoint is foreign and rejects
  (`has no rollback checkpoint`).
- The manifest is a private file in its own private directory, validates
  against the installation migration manifest contract, and is in PREPARED
  state with all pre-cutover invariants (all writers puddingclaw, rollback
  window open, no minted resource URIs, rebinds pending). Its digest is pinned
  in the committed checkpoint as `prepared_manifest_sha256`.
- `--rollback-evidence` is a private bounded file. Only its SHA-256 enters the
  plan, the checkpoint (`rollback_evidence_sha256`), the manifest
  (`rollback_evidence_digest`) and both rev2 journal events
  (`rollback_evidence_sha256`); the content is never interpreted here. The
  digest is re-verified on every resume and around every delegation, so
  evidence drift between or during runs rejects (`Rollback plan changed` /
  `Rollback evidence changed`).
- Harness Home, Knowledge workspace, checkpoint directory, manifest directory,
  evidence directory and both authority roots are pairwise disjoint.

The window path re-verifies its own preconditions before the first commit:

- The manifest is CUTOVER; PREPARED belongs to the abort path and FINALIZED
  closed the window, and both refuse
  (`requires a CUTOVER installation manifest`). Its digest is pinned in the
  committed checkpoint as `cutover_manifest_sha256`, and the exact CUTOVER
  bytes are preserved as `cutover-manifest.json` in the checkpoint directory.
- Each journal's head is exactly this manifest's registered cutover: the rev2
  `assigned` event (Harness writer `puddingharness`, both Knowledge domains
  `puddingknowledge`) whose digest the CUTOVER manifest registered as
  `harness_assigned_event_sha256` / `knowledge_assigned_event_sha256`, binding
  the same `active_installation_revision`. A journal already carrying this
  operation's rev3 suspension is the crash window before the first checkpoint
  and is consumed as an exact retry. Any other `assigned` head (a rev4 without
  this orchestrator's checkpoint) is foreign and rejects
  (`no window rollback checkpoint`), even when the caller reuses the foreign
  operation id.
- `--operation-id` must be NEW: reusing the cutover operation rejects
  (`requires a new operation`).
- The evidence contract is identical to the abort path (bound by digest only,
  re-verified on every resume), and the disjoint-roots rule additionally
  covers the evidence directory.

Knowledge is invoked only through the explicitly selected independent
interpreter (`-m knowledge_platform.local.writer_authority`), with the same
contract as the barrier and the cutover orchestrator: scrubbed environment
(`PATH`/`HOME` only), bounded stdout, the checkpoint lock passed down so a dead
parent cannot admit a second coordinator, and executable plus installed-release
identity re-verified before and after every delegation. Every Knowledge receipt
is validated through `harness.knowledge_writer_receipt.validate_receipt`.

## Reverse two-phase commit (pre-cutover abort)

Checkpoint states, in order, each committed atomically before the next step
starts:

1. `manifest_rolled_back` —
   `harness.installation_manifest.rollback_installation` advances the manifest
   PREPARED → ROLLED_BACK, binding `rollback_evidence_digest` to the evidence
   SHA-256. Active writers stay puddingclaw and the rollback window stays
   open. The exact PREPARED bytes are preserved as `prepared-manifest.json`
   and the ROLLED_BACK bytes as `rolled-back-manifest.json` inside the
   checkpoint directory before the checkpoint commits.
2. `harness_reassigned` — the Harness rev2 `assigned` event commits through
   `harness.installation_authority.assign` (writer `puddingclaw`), bound to
   the preserved ROLLED_BACK bytes and the evidence digest.
3. `both_reassigned` — the delegated Knowledge CLI commits its rev2 `assigned`
   (both domains `puddingclaw`) with `--rollback-evidence` to the same
   ROLLED_BACK digest and evidence digest; the Harness journal is re-verified
   unchanged, both freeze markers are verified to still be in place (below),
   and completion is committed with `rollback_completed: true`.

Both assignments commit to the preserved ROLLED_BACK copy, never to the live
manifest file, so a tampered live file rejects on resume
(`Rolled back installation manifest changed`) instead of being committed to.
Each rev2 head is pinned by `_rolled_back_event`: exactly revisions 0-2, the
rev1 suspension's operation matching the rev2 assignment, writer puddingclaw
(Harness) or both domains puddingclaw (Knowledge),
`migration_manifest_sha256` equal to the ROLLED_BACK digest,
`active_installation_revision` equal to its `sha256:`-prefixed form,
`rollback_evidence_sha256` equal to the evidence digest, and self-consistent
event digests.

## Window two-phase commit (post-cutover)

`window-rollback` checkpoint states, in order, each committed atomically
before the next step starts:

1. `harness_resuspended` — the Harness rev3 `suspended` event commits through
   `harness.installation_authority.suspend` under the new operation. The
   suspension re-applies the Harness freeze marker.
2. `both_resuspended` — the delegated Knowledge CLI commits its rev3
   `suspended` under the same operation (`suspend --operation-id`), the
   Harness journal is re-verified unchanged, and both re-applied freeze
   markers are verified against the rev3 freeze receipts. This fence/freeze
   step completes on both products before any reverse migration runs.
3. `manifest_rolled_back` —
   `harness.installation_manifest.rollback_installation` advances the manifest
   CUTOVER → ROLLED_BACK: `rollback_evidence_digest` binds the evidence
   SHA-256, `active_writers` flip back to puddingclaw in all three domains,
   `rollback_window_open` stays true, and `started_at`,
   `staging_namespace`, `active_installation_revision` and the cutover
   checkpoint registrations are carried as history. Nothing new is registered:
   the rev4 events are journal-level, and the manifest-level evidence binding
   is the `rollback_evidence_digest` alone. The ROLLED_BACK bytes are
   preserved as `rolled-back-manifest.json` before the checkpoint commits.
4. `harness_reassigned` — the Harness rev4 `assigned` event commits through
   `harness.installation_authority.assign` (writer `puddingclaw`) against the
   preserved ROLLED_BACK bytes and the evidence digest.
5. `both_reassigned` — the delegated Knowledge CLI commits its rev4 `assigned`
   (both domains `puddingclaw`) with `--writer puddingclaw
   --rollback-evidence` to the same ROLLED_BACK digest and evidence digest;
   the Harness journal is re-verified unchanged, both freeze markers are
   verified to still be in place, and completion is committed with
   `rollback_completed: true`.

Each rev4 head is pinned by the generalized `_rolled_back_event`: exactly
revisions 0-4, the rev3 suspension's operation matching the rev4 assignment,
the rev3 suspension following the rev2 cutover assignment head
(`previous` linkage), writer puddingclaw (Harness) or both domains
puddingclaw (Knowledge), `migration_manifest_sha256` equal to the ROLLED_BACK
digest (the rev2 cutover assignment committed to the PREPARED digest instead),
`active_installation_revision` equal to its `sha256:`-prefixed form — both
computed by the writer authority layer from the exact ROLLED_BACK manifest
bytes passed to `assign` — `rollback_evidence_sha256` equal to the evidence
digest, and self-consistent event digests. Once a rev4 assignment exists, the
rev3 suspension is never re-appended; resume re-derives every step and
compares against the recorded journals.

## Why no thaw happens here

Rollback returns writer authority to the legacy product, so the new products
must remain inert: both freeze markers (`/.installation-freeze-v1.json` in the
Harness Home, `.workspace-freeze-v1.json` in the Knowledge workspace) are
verified to still exist and to still match the freeze receipts committed in
the suspensions (rev1 for the abort path, rev3 for the window path), checked
after the assignments and on every exact retry. A vanished or replaced marker
refuses (`Freeze marker vanished` / `Knowledge freeze marker changed`) and
completion is never published. Nothing is retired into the authority
directories and no thaw receipt is written; the new products stay fenced out
of their runtimes. On the window path the retired rev2 markers, the rev2 thaw
receipts and the `active-installation.json` pointer stay untouched as history
— the pointer records the cutover, and admission is gated by the journals and
the freeze markers, not by the pointer. Thawing the legacy source Home is the
source product's own responsibility and is deliberately out of scope — this
orchestrator never writes to the source Home.

## Exact retry and crash windows

The checkpoint plan binds both enrollments, the checkpoint directory identity,
the manifest directory identity, the evidence directory identity and digest,
the operation id and the selected Knowledge interpreter and release
identities; a changed plan rejects. A completed checkpoint never downgrades:
resume re-derives every step and compares against the recorded journals,
digests and copies. Crash windows and their resume:

| Crash point | Observable state | Resume |
| --- | --- | --- |
| Before `manifest_rolled_back` | Both suspended; manifest PREPARED (or already ROLLED_BACK binding this evidence, with the preserved PREPARED copy) | Advance is exact-retry; checkpoint commits |
| After `manifest_rolled_back` | Manifest ROLLED_BACK; Harness suspended or already carrying this operation's rollback rev2; Knowledge suspended | Harness assign commits (or is found committed) |
| After `harness_reassigned` | Harness rev2, Knowledge suspended or (delegated child committed, receipt lost) already carrying this operation's rollback rev2; manifest ROLLED_BACK | Knowledge assign commits (or is found committed); freeze markers re-verified |
| After `both_reassigned` | Complete; both still frozen | Rerun returns the identical result with unchanged checkpoint bytes |

Window-path crash windows and their resume:

| Crash point | Observable state | Resume |
| --- | --- | --- |
| Before `harness_resuspended` | Both journals rev2 (the registered cutover) or Harness already rev3; manifest CUTOVER | Suspension is exact-retry; checkpoint commits |
| After `harness_resuspended` | Harness rev3 and re-frozen; Knowledge rev2 or already rev3; manifest CUTOVER | Knowledge suspension commits (or is found committed) |
| After `both_resuspended` | Both rev3 and re-frozen; manifest CUTOVER (or already ROLLED_BACK binding this evidence, with the preserved CUTOVER copy) | Advance is exact-retry; checkpoint commits |
| After `manifest_rolled_back` | Manifest ROLLED_BACK; Harness rev3 or already carrying this operation's rev4; Knowledge rev3 | Harness assign commits (or is found committed) |
| After `harness_reassigned` | Harness rev4, Knowledge rev3 or (delegated child committed, receipt lost) already carrying this operation's rev4; manifest ROLLED_BACK | Knowledge assign commits (or is found committed); freeze markers re-verified |
| After `both_reassigned` | Complete; both still frozen | Rerun returns the identical result with unchanged checkpoint bytes |

## Failure matrix

| Failure | Effect | Required action |
| --- | --- | --- |
| Unsuspended writer, suspension under another operation | Rejects before any commit; no checkpoint | Run the writer barrier with the same operation id |
| `assigned` rev2 without a rollback checkpoint | Rejects (`has no rollback checkpoint`) | Inspect the journals; never auto-repaired |
| Manifest not PREPARED (and no preserved copy crash window) | Rejects before any commit | Restore the PREPARED manifest or rerun the manifest rollback under this orchestrator |
| Window: manifest not CUTOVER (PREPARED, FINALIZED, or already ROLLED_BACK) | Rejects before any commit (`requires a CUTOVER installation manifest`); no checkpoint | PREPARED belongs to the abort path; FINALIZED is irreversible; a rolled back manifest is complete |
| Window: cutover operation reused | Rejects (`requires a new operation`); no checkpoint | Choose a fresh operation id |
| Window: journal head is not the registered cutover rev2 (or a foreign rev4) | Rejects (`not the registered installation cutover` / `no window rollback checkpoint`) | Inspect the journals and the manifest registrations; never auto-repaired |
| Evidence digest drift between runs | Rejects (`Rollback plan changed` / `Window rollback plan changed`); checkpoint unchanged | Restore the exact evidence bytes |
| Delegated CLI failure or invalid receipt | Checkpoint stays at the last commit; the Knowledge journal may legitimately hold the next revision (child committed) | Fix the interpreter/installation; rerun the exact command |
| Freeze marker vanished or replaced | Rejects; completion never published; committed journal revisions stand | Restore the committed freeze marker; rerun |
| Interpreter or installed-release identity change | Rejects; partial state stays suspended/frozen | Restore the exact trusted installation; rerun |
| Manifest or preserved-copy tampering after a committed checkpoint | Rejects (`manifest changed` / copy mismatch) | Restore the committed manifest bytes |

No automatic repair of a partial rollback is attempted: both products remain
frozen at every intermediate state, so no window exists where a new product
believes it is the writer.

## Out of scope (fail closed)

- ROLLED_BACK → FINALIZED remains a future increment;
  `harness.installation_manifest.finalize_installation` rejects it and this
  orchestrator exposes no finalize command. A manifest rolled back from
  CUTOVER is equally refused: the window-rollback tests pin this.
- Re-cutover after a window rollback. A rolled back manifest never re-advances:
  `cutover_installation` requires PREPARED and rejects ROLLED_BACK, and the
  cutover orchestrator rejects the rev4 journal heads. Re-migration is a NEW
  migration operation (new snapshot, new manifest, new journals) and is not
  this module's concern.
- Any thaw: neither new product is thawed (their markers must remain), and the
  legacy source Home's thaw is the source product's responsibility, performed
  with its own evidence, never by this module.
- Source-Home writes of any kind.

## Fail-closed flags

- `rollback_completed`: false on every intermediate checkpoint and on any
  error; true only in the committed `both_reassigned` state.
- `installation_cutover_performed`: always false; this module performs no
  cutover.
- `production_activated`: always false.
- `activation_allowed`: always false in orchestrator output. The orchestrator
  itself grants no runtime authority; the journals are the authority, and both
  new products stay frozen.

On any rejection the CLI prints `error_code: installation_rollback_rejected`
with all four flags false and exits 1.

## Tests

```sh
KNOWLEDGE_TEST_PYTHON=/absolute/knowledge/.venv/bin/python \
  python -m pytest tests/test_rollback_orchestrator.py
KNOWLEDGE_TEST_PYTHON=/absolute/knowledge/.venv/bin/python \
  python -m pytest tests/test_window_rollback.py
```

The installed tests drive the real Knowledge writer-authority CLI: happy path
with exact retry and persistent freeze, crash injection at every checkpoint,
delegation failure and invalid-receipt injection, each precondition violation
(including evidence digest drift between runs), freeze-marker-vanished
detection, and committed-manifest tampering. The window tests additionally
drive a real cutover to `both_thawed` first, then cover rev3 resuspension
idempotence, the new-operation rule, foreign rev4 rejection, CUTOVER-state
gating, and finalize/re-cutover refusal after the rollback. Without the
explicit interpreter the installed tests skip; a skip is not proof of the
cross-product contract. Manifest-level advance tests
(`tests/test_installation_manifest_advance.py`) and the manifest-level
CUTOVER → ROLLED_BACK unit tests in `tests/test_window_rollback.py` run
without Knowledge.
