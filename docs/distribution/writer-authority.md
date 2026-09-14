# Existing Harness writer enrollment and suspension

This protocol registers the **existing** `session_harness` writer of one local
Harness Home. It is an installed runtime admission mechanism, not installation
PREPARED/CUTOVER, cross-product reassignment, credential migration or thaw.
Unenrolled independent Homes retain their existing behavior. Enrollment is
explicit; it is not yet mandatory admission for all three products.

```sh
python -m harness.installation_authority enroll \
  --home /absolute/private/harness-home \
  --authority /absolute/new-private/authority \
  --operation-id enroll-existing-1
python -m harness.installation_authority status --home /absolute/private/harness-home
python -m harness.installation_authority suspend \
  --home /absolute/private/harness-home --operation-id suspend-existing-1
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
chain. Revision 0 records `existing_writer`; revision 1 records `suspended` with
no writer and the persisted freeze receipt commitment. Reassignment/reactivation
is intentionally unavailable until migration and reverse-data evidence can be
verified. These are not the complete installation manifest or domain writer map.

Python admission holds Home shared flock and authority shared flock for its
lifetime. Managed children inherit both descriptors. The business Home resolver
is fixed by process admission and rejects later environment drift. Node checks
the same binding/journal under the existing Home CLI gate, then durably records
binding digest/revision/event digest in its ticket before running the callback.
Node does not hold an authority flock: the authorized mutator must first freeze
Home, which refuses active/unsettled tickets and managed runtime records.

Suspension first completes the existing persistent Home freeze, then obtains
authority exclusive admission and commits revision 1. Live Python writers, Node
tickets and managed frontend/launcher runtime records prevent this operation.
The supervisor clears a runtime record only after authenticated process shutdown;
an unresolved ownership or stop failure remains blocking. All flocks are
nonblocking, so contention returns an explicit failure rather than waiting while
holding another lock. A failed acquisition closes acquired descriptors.

Crash after freeze but before revision commit leaves Home frozen at revision 0;
exact suspension retry completes the journal. A completed suspension revalidates
its freeze marker before acknowledging retry; a removed marker is not recreated.
Even without that marker, enrolled Python/Node writers reject suspended authority.
File/directory fsync precedes successful publication. Partial enrollment markers
reject startup; complete matching publication can be retried. Incomplete bytes or
an unresolved CLI gate are not silently removed.

Deleting the permanent binding, replacing Home/locks/control roots, directly
editing journal content, or arbitrary same-user code writing files without the
participating entrypoints is outside the cooperative fence. This does not claim
remote/container writer authority. Dotenv may select trusted startup Home before
admission; that selected Home is validated and then fixed. It cannot redirect an
already-admitted process through the supported business path factories.

This stage is tested through the independent noneditable backend and a packaged
CLI source closure. The full embedded runtime candidate remains the artifact
built at `bc2b422`; it has not been rebuilt for this protocol. Full multi-product
active-installation revision, lossless rollback, credential continuity and final
product/release validation remain required.
