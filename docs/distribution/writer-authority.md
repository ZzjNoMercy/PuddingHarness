# Harness writer enrollment, suspension, reassignment and audited thaw

This protocol registers the **existing** `session_harness` writer of one local
Harness Home. It is an installed runtime admission mechanism, not installation
PREPARED/CUTOVER orchestration, cross-product atomic commit, credential
migration or FINALIZED. The v2 journal extension adds single-direction writer
reassignment (`assigned` revisions) bound to a verified installation migration
manifest, and an audited thaw that retires the freeze marker only for an
installation assigned to this Harness. Unenrolled independent Homes retain
their existing behavior. Enrollment is explicit; it is not yet mandatory
admission for all three products.

```sh
python -m harness.installation_authority enroll \
  --home /absolute/private/harness-home \
  --authority /absolute/new-private/authority \
  --operation-id enroll-existing-1
python -m harness.installation_authority status --home /absolute/private/harness-home
python -m harness.installation_authority suspend \
  --home /absolute/private/harness-home --operation-id op-migration-1
python -m harness.installation_authority assign \
  --home /absolute/private/harness-home --operation-id op-migration-1 \
  --manifest /absolute/private-dir/manifest.json --writer puddingharness
python -m harness.installation_authority thaw \
  --home /absolute/private/harness-home --operation-id op-migration-1 \
  --manifest /absolute/private-dir/manifest.json
# Rollback direction (reassigns the writer back to the legacy product):
python -m harness.installation_authority assign \
  --home /absolute/private/harness-home --operation-id op-rollback-1 \
  --manifest /absolute/private-dir/manifest.json --writer puddingclaw \
  --rollback-evidence /absolute/private-dir/reverse-evidence.json
```

Enrollment accepts an existing private Home (normally mode `0700`). A legacy
`0755` Home is rejected without chmod; permission migration must be explicitly
reviewed and completed before enrollment. This command never silently repairs it.

Stop managed services and complete CLI writes before enrollment. It takes Home
exclusive admission and the CLI gate, refuses runtime ownership records or
unsettled tickets, and only registers an unfrozen existing Home. It never creates
a migrated writer or grants authority to another Home. Authority and Home must be
disjoint private owned directories; writable ancestors without sticky protection
reject. Virtualenv/runtime configuration remains trusted local configuration.

The permanent private Home binding commits both root identities and enrollment
operation. The canonical journal binds that document digest and a checked event
chain. Revision 0 records `existing_writer`; odd revisions record `suspended`
with no writer and the persisted freeze receipt commitment; even revisions at or
above 2 record `assigned`. The validator enforces chain continuity and this
exact alternation with no event-count cap: a repeated, skipped or regressed
state rejects the whole journal, as does any `assigned` revision missing a
required binding. These events are not the complete installation manifest or
domain writer map.

## Assigned revisions and reassignment

An `assigned` revision restores a non-null writer and carries the binding of
the operation that committed it: `writer` (`puddingharness` for a forward
assignment to this Harness, `puddingclaw` for a rollback assignment to the
legacy product), the superseded suspension's `freeze_receipt_sha256`,
`migration_manifest_sha256` (plain SHA-256 of the presented manifest bytes),
`active_installation_revision` (the same content digest in the manifest
schema's `sha256:`-prefixed notation), and `rollback_evidence_sha256` (exactly
the manifest's `rollback_evidence_digest`; required for a rollback assignment,
null otherwise).

`assign` requires the journal head to be `suspended` under the same operation
id, revalidates that the persisted freeze marker still matches the suspension
commitment, and validates the manifest structurally (`validate_manifest`). The
manifest state pins the direction: a forward assignment to this Harness
requires PREPARED (the journal assignment precedes the manifest's own
PREPARED→CUTOVER advance, which the future orchestrator commits); a rollback
assignment requires ROLLED_BACK plus `--rollback-evidence`, a private file
whose SHA-256 must equal the manifest's `rollback_evidence_digest`. Retry is
exact: re-running with identical bindings returns the journal unchanged; any
drift rejects. A committed assignment still activates nothing — the report
keeps `activation_allowed:false` until thaw, and forever for an assigned-away
installation.

Runtime admission follows the journal head: `existing_writer` or
`assigned(writer=puddingharness)` admits this Harness's writers; `suspended` or
`assigned(writer=puddingclaw)` denies Python process admission and Node CLI
tickets. The journal stays authoritative when the freeze marker is absent: an
assigned-away installation denies writes even if its marker is removed, and old
binaries that only understand the two-event journal reject any history at or
above revision 2 — the intended fail-closed behavior, since an old binary is
never the assigned writer.

## Audited thaw

`thaw` is the only path that removes a freeze marker, and only for an
installation whose journal head is `assigned` to this Harness. It requires the
same operation id as the assignment and the byte-identical manifest (both
committed digests are rechecked), takes Home exclusive admission, the CLI gate
and the authority exclusive lock, and revalidates the marker bytes against the
suspension commitment. It then, in order: writes a private
`thaw-receipt-rev<N>.json` into the authority directory (binding the operation,
head revision digest, freeze marker digest and manifest digests) with fsync;
and atomically renames the marker to `freeze-marker-rev<N>.json` inside the
authority directory — never unlinking it — followed by directory fsyncs. The
renamed marker and the receipt are the permanent audit record; thaw and Home
must therefore share one filesystem, and a cross-device rename rejects.

Retry is exact and crash-safe. A receipt without a retired marker is completed
by renaming; a retired marker with a matching receipt is acknowledged without
further mutation; a missing receipt for a retired marker, a changed receipt, a
changed retired marker, or a marker and retired record coexisting reject
loudly and are never auto-repaired. A killed thaw leaves the CLI admission
gate in place (denying writers) exactly like a killed freeze; after operator
removal of the stale gate, retry completes byte-identically. A successful thaw
reports `activation_allowed:true`; `status` reports the journal head revision,
state and writer.

Python admission holds Home shared flock and authority shared flock for its
lifetime. Managed children inherit both descriptors. The business Home resolver
is fixed by process admission and rejects later environment drift. Node checks
the same binding and full journal chain under the existing Home CLI gate, then
durably records binding digest/head revision/head event digest in its ticket
before running the callback. Node does not hold an authority flock: the
authorized mutator must first freeze Home, which refuses active/unsettled
tickets and managed runtime records.

Suspension first completes the existing persistent Home freeze, then obtains
authority exclusive admission and commits the next odd revision. Live Python
writers, Node tickets and managed frontend/launcher runtime records prevent
this operation. The supervisor clears a runtime record only after authenticated
process shutdown; an unresolved ownership or stop failure remains blocking. All
flocks are nonblocking, so contention returns an explicit failure rather than
waiting while holding another lock. A failed acquisition closes acquired
descriptors. Re-suspension after a thaw recreates the marker and continues the
alternating chain (revisions 3, 5, …), which is how a rollback assignment
sequence is built.

Crash after freeze but before revision commit leaves Home frozen at the last
committed revision; exact suspension retry completes the journal. A completed
suspension revalidates its freeze marker before acknowledging retry; a removed
marker is not recreated. Even without that marker, enrolled Python/Node writers
reject suspended or assigned-away authority. File/directory fsync precedes
successful publication. Partial enrollment markers reject startup; complete
matching publication can be retried. Incomplete bytes or an unresolved CLI gate
are not silently removed.

Deleting the permanent binding, replacing Home/locks/control roots, directly
editing journal content, or arbitrary same-user code writing files without the
participating entrypoints is outside the cooperative fence. This does not claim
remote/container writer authority. Dotenv may select trusted startup Home before
admission; that selected Home is validated and then fixed. It cannot redirect an
already-admitted process through the supported business path factories.

## Explicit non-goals of this increment

- Cross-product CUTOVER/ROLLED_BACK orchestration: no `active-installation.json`
  pointer publication, no delegation to the Knowledge product's authority, and
  no manifest state advance (`PREPARED→CUTOVER`, `PREPARED→ROLLED_BACK`,
  FINALIZED) happens here. The two-phase commit that consumes these journal
  primitives is a separate future increment. The Knowledge product's own
  assign/thaw commands live in that product; Harness only revalidates the
  published Knowledge journal, which mirrors this protocol with
  `knowledge_catalog`/`connector_jobs` writers valued `puddingknowledge` or
  `puddingclaw`.
- Old PuddingClaw binaries never participate in the journal/fence: their writes
  after a rollback stay outside this cooperative contract, as already stated
  for suspension.
- Cross-machine or remote Platform migration orchestration and fencing of
  container/external writers.
- Credential rebind execution, rollback data movement and vector index rebuild;
  the rollback assignment only binds the already-produced reverse evidence
  digest.

This stage is tested through the independent noneditable backend and a packaged
CLI source closure. The full embedded runtime candidate remains the artifact
built at `bc2b422`; it has not been rebuilt for this protocol. The current CLI
refuses to install or start that older runtime in an enrolled Home because it
lacks `writer_authority: 1`. Full verified builds now emit this contract. Full multi-product
active-installation revision, lossless rollback, credential continuity and final
product/release validation remain required.
