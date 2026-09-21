# Rehearsal driver: checkpointed end-to-end migration chains

`harness.rehearsal_driver` drives the specification section 11.20 item 10
real-data rehearsal as one ordered command: it runs the shipped migration
chain — source-home snapshot, Knowledge migration request generation, offline
migration orchestration, manifest discover and prepare, product enrollment,
writer suspension, cutover — as ordered REAL subprocesses against one private
work root, so the rehearsal exercises the same CLIs an operator would run, in
the same order, and proves their committed outputs compose. Two scenarios
share that forward chain: `path-a` (default) finishes with finalize
(DISCOVERED, PREPARED, CUTOVER, FINALIZED), and `window-rollback` stops after
cutover and drives the real reverse chain — window fence, frozen export,
other-catalog disposition, document reverse bundle, absent-wiki attestation,
rollback evidence, window rollback — to a ROLLED_BACK manifest with both
writers reassigned to puddingclaw.

```sh
python -m harness.rehearsal_driver \
  --work-root /absolute/new-private/rehearsal-work \
  --source-home /absolute/legacy/.puddingclaw \
  --knowledge-python /absolute/knowledge/.venv/bin/python \
  --installation-id install-rehearsal \
  --source-revision legacy-1 \
  --source-schema-revision claw-schema-v7 \
  --operation rehearsal-2026-09-21 \
  --graft /Users/pet/Documents/knowledge=external/knowledge \
  --map /Users/pet/Documents/knowledge=external/knowledge \
  --repair-document doc_611a9b85a8184082ba09456b \
  --repair-reason "dangling storage_path target is missing" \
  --exclude scratch/domain
```

`--knowledge-python` defaults to the `KNOWLEDGE_TEST_PYTHON` environment
variable and must be an explicit independent Knowledge interpreter, exactly as
the orchestration boundary requires. `--graft`, `--exclude`,
`--repair-document`/`--repair-reason` pass through to the snapshot producer;
repeatable `--map SRC=DST` passes through to the request generator (at least
one mapping is required); `--wiki-root-relative` selects the wiki root inside
the snapshot payload (default `llm-wiki`); `--timeout-seconds` bounds every
individual step subprocess. The source home is strictly read-only.
`--scenario window-rollback` selects the rollback rehearsal, which binds its
fence, reverse-chain plans and journal events to a dedicated window operation
(`--window-operation`, default `<operation>-window`); the window operation
must differ from `--operation`, and path A takes no window operation.

Work-root layout (the work root itself is 0700; everything inside it is owned
by the caller and symlink-free, files are private 0600 single-linked, and
directories are sealed against group/other writes — chain-owned intermediate
directories keep the modes their producers create):

    snapshot/               rehearsal_snapshot envelope (plan/manifest/payload)
    snapshot-receipt.json   producer receipt (lives outside the envelope)
    request/                Knowledge request + generation receipt
    staging/                migration orchestrator staging (harness/, knowledge/)
    knowledge-receipt.json  receipt extracted from the orchestrator result
    manifest/               installation manifest (DISCOVERED..FINALIZED, or
                            DISCOVERED..ROLLED_BACK in the window scenario)
    harness-home/           fresh Harness product Home, enrolled
    harness-authority/      Harness writer authority (journal, retired markers)
    knowledge-home/         Knowledge workspace bootstrapped from the staged
                            document-migration candidate, enrolled
    knowledge-authority/    Knowledge writer authority
    barrier/                writer suspension barrier checkpoint
    checkpoint/             cutover orchestrator checkpoint
    driver-checkpoint.json  this driver's private checkpoint
    run-record.json         machine-readable rehearsal evidence

The window-rollback scenario adds:

    lineage/                document-lineage stand-in workspace (state,
                            authority, frozen export, target-before/after
                            catalog copies)
    window-disposition/     other-catalog reverse disposition receipt
    document-reverse/       reverse bindings, attachment bindings and the
                            reversed legacy candidate (manifest + catalog)
    wiki-side/              version-1 wiki-shadow workspace (state, authority,
                            export, attestation) for the absent-wiki proof
    evidence/               assembled rollback evidence (own disjoint root)
    window-checkpoint/      window rollback orchestrator checkpoint

The ordered path-A step table is `snapshot, request, orchestrate, discover,
prepare, enroll, suspend, cutover, finalize`. Each step invokes its real CLI
(`harness.rehearsal_snapshot`,
`knowledge_platform.distribution.claw_migration_request`,
`harness.migration_orchestrator`, `harness.installation_manifest` discover and
prepare, `harness.installation_authority` enroll plus the Knowledge workspace
bootstrap and `knowledge_platform.local.writer_authority` enroll,
`harness.writer_barrier`, `harness.cutover_orchestrator` cutover and
finalize), validates the step's stdout receipt and the private files the step
committed — the snapshot receipt is cross-checked against the envelope
commitment, the orchestrator's Knowledge receipt is re-digested against the
staging checkpoint and republished as `knowledge-receipt.json`, and the
discover snapshot digest is recomputed from the snapshot commitment — then
records the step. Enrollment uses fixed operation ids (`enroll-harness`,
`enroll-knowledge`); `--operation` binds the suspension and cutover. The
Knowledge workspace bootstrap is library-only upstream, so enrollment runs it
through the independent interpreter before the enroll CLI: the workspace is
the real cutover target, opened with the documented
`open_persistent_workspace(state, document_migration=<staging
candidate>)` incantation against the staged document-migration candidate, so
it holds exactly what the migration produced (catalog, blobs, resources).

Checkpoint contract: after each step commits, the driver atomically publishes
`driver-checkpoint.json` (canonical JSON, 0600) recording the parameter
digest and, per step, `{name, idempotent, receipt, outputs}` where outputs
maps every work-root-relative root the step owns to a tree digest taken after
the step committed. On (re)start the checkpoint is validated for shape,
canonical bytes and step order, and every recorded root is re-digested and
compared against the latest committed record for that root — later steps
legitimately rewrite the product homes, authorities and manifest, so the fold
takes the newest record. Any divergence, unknown or symlinked work-root
entry, torn or non-canonical checkpoint, or a changed parameter set with at
least one committed step refuses closed; with zero committed steps a changed
parameter set simply reinitializes the checkpoint. A surviving `run-record.json`
with an incomplete checkpoint refuses.

Resume semantics: committed steps are never re-executed — they are verified
and reported `verified` with `idempotent: true`. A SIGKILL between steps
resumes at the first uncommitted step; a SIGKILL mid-step re-runs that step.
The step CLIs already recover their own interrupted publications, and the
driver additionally resets only the outputs it owns outright for steps that
never committed: a torn snapshot envelope fails its own admission and is
rebuilt (the producer deliberately refuses to overwrite one), and the request
directory, the enrollment roots and the reverse-chain roots are regenerated.
Every window step writes only roots no earlier step pins, so a kill mid-chain
never invalidates a committed root. Re-running a completed scenario verifies
every step and compares or republishes a byte-identical `run-record.json`.
The run record is canonical JSON listing each step's name,
committing-execution idempotence flag, committed-receipt digest and output
digests, plus the terminal bindings; it carries digests, labels and counts
only — no secrets and no row content. After the last step the driver
re-verifies the terminal state against the real artifacts before publishing:
for path A, a FINALIZED manifest with closed rollback window and cutover
writer set, both journals' assigned revision-2 heads, retired freeze markers
and thaw receipts, the active-installation pointer, and a successful Harness
installation-guard admission. Failure anywhere prints one bounded single-line
JSON error (`rehearsal_driver_rejected`, with the step name and the step
CLI's own error code when one exists) and exits 1.

## Window-rollback scenario

`--scenario window-rollback` runs the forward chain through cutover (steps
1–8 above, no finalize) and then appends eight steps that drive the real
Knowledge reverse chain against the post-cutover installation state and feed
its output to the real window-rollback orchestrator:

| step | roots | invokes |
| --- | --- | --- |
| window-delta | `knowledge-home` | `knowledge_python -c` title-edit snippet |
| window-suspend | `harness-home`, `harness-authority`, `knowledge-home`, `knowledge-authority` | `harness.installation_authority suspend`, `knowledge_platform.local.writer_authority suspend` |
| window-export | `lineage` | workspace bootstrap snippet, title-edit snippet, `writer_authority` enroll/suspend, `knowledge_platform.local.frozen_export` |
| window-disposition | `window-disposition` | `knowledge_platform.distribution.other_catalog_reverse` |
| window-document-reverse | `document-reverse` | bindings/attachment construction + `knowledge_platform.distribution.document_reverse` |
| window-wiki-absent | `wiki-side` | wiki-shadow snippet, `writer_authority` enroll/suspend, `frozen_export`, `knowledge_platform.distribution.wiki_reverse_absent` |
| window-evidence | `evidence` | `knowledge_platform.distribution.rollback_evidence` |
| window-rollback | `window-checkpoint`, `manifest`, both homes and authorities | `harness.rollback_orchestrator window-rollback` |

The choreography mirrors the operator runbook. `window-delta` applies one
deterministic Knowledge-era edit (a fixed title on the first migrated
document asset) to the real post-cutover workspace and records the catalog
digests before and after. `window-suspend` then fences both products under
the window operation with the two real suspend CLIs — `harness.writer_barrier`
cannot resuspend post-cutover journals (its receipt validation requires the
exactly-two-event shape), so the driver suspends each product directly; the
window-rollback orchestrator later consumes those revision-3 suspensions as
an exact retry and never appends a duplicate. Both freeze markers are
re-applied and pinned against the revision-3 receipts.

`window-export` produces the frozen export. A frozen export requires an
exactly-two-event journal (enrollment plus this operation's suspension),
which a post-cutover workspace — three or four events — can never present,
so the export runs against a document-lineage stand-in bootstrapped from the
same staged candidate. The bootstrap is byte-deterministic, and the honesty
pin is exact: the stand-in receives the identical title edit, its edit
receipt must equal the committed `window-delta` receipt, and the
`target-before` copy must digest to the receipt's pre-edit catalog while the
edited stand-in catalog must digest to the post-edit catalog of
`knowledge-home`. The stand-in is enrolled (`enroll-knowledge-lineage`) and
suspended under the window operation with the real CLIs before
`frozen_export` runs; `target-after` is copied from the post-export catalog.
`window-disposition` then maps every non-core catalog table between the two
targets into the frozen export's other-catalog disposition
(`--frozen-export` takes the export root directory).

`window-document-reverse` binds every migrated document asset to its body
blob under `knowledge-home` (re-digesting each blob against the catalog's
content digest), maps every attachment reference preserved in the migrated
metadata (`assets[].path`, `original_path`,
`multimodal.image_assets_dir`) back into the grafted corpus tree as
`resources/<graft>/<suffix>` — a reference escaping every graft refuses
closed — and runs `document_reverse` with `--body-root knowledge-home`
against the snapshot's legacy catalog copy. The reversed legacy candidate it
commits carries the Knowledge-era edit: exactly the edited document's title
changed, every other row is original.

`window-wiki-absent` proves the reversed installation carries no wiki
domain. No product bootstrap can produce the required shape: the
migrated-wiki bootstrap refuses the forward chain's empty staging wiki
archive (its projection requires `archive/wiki` or `raw/`), and every library
bootstrap inserts knowledge space/dataset identity rows and a `wiki-evidence`
member, both of which the absent-wiki attestation refuses. The driver
therefore hand-builds a version-1 wiki-shadow workspace — an empty
`migrate_to_latest` catalog that was never written to (forensically cleaner
than deleted rows, which leave SQLite tombstones), a one-page wiki and a
hand-written version-1 manifest — loads it through the real persistent
workspace open (a version-1 load never reads catalog rows), enrolls it
(`enroll-knowledge-wiki`), suspends it under the window operation, exports
it with `frozen_export`, and runs `wiki_reverse_absent`, which attests the
normalized catalog carries zero rows in the three knowledge core tables and
the six wiki state tables, with no wiki-evidence member.

`window-evidence` assembles the four artifact manifests into the rollback
evidence under its own root — the orchestrator requires the evidence
directory pairwise-disjoint from the homes, authorities, checkpoint and
manifest directories — and pins the assembled bytes against the receipt and
the window operation. `window-rollback` finally invokes
`harness.rollback_orchestrator window-rollback` with the assembled evidence;
the orchestrator consumes the pre-suspended revision-3 journals, advances
the manifest to ROLLED_BACK bound to the evidence digest, and appends both
revision-4 puddingclaw assignments.

The scenario's terminal verification mirrors the orchestrator's happy-path
test assertions against the real artifacts: a ROLLED_BACK manifest with
puddingclaw writers everywhere, the evidence digest bound, the rollback
window open and `completed_at` absent, cutover history (checkpoint
registrations, committed prepared revision, start timestamp, staging
namespace) preserved and equal to the checkpoint's preserved cutover copy;
both journals at five events with the cutover assignment under `--operation`,
the window suspension under the window operation and the revision-4
assignment binding the rolled-back manifest and evidence digests; both
freeze markers holding the revision-3 receipts; retired revision-2 artifacts
present and no retired revision-4 artifacts; the active-installation pointer
unchanged; the checkpoint directory holding the cutover and rolled-back
manifest copies; and fail-closed refusals — Harness installation-guard
admission, Knowledge workspace open, thaw, manifest finalize, cutover
finalize and re-cutover (which publishes no checkpoint) all refuse, with the
manifest bytes unchanged afterwards, and the Knowledge writer CLI's `status`
reporting exactly the committed journal.

The window operation and the cutover operation are distinct identities by
construction (the driver refuses their equality before any step runs):
`--operation` appears only in the revision-1 suspensions and revision-2
cutover assignments, while the window operation appears in the lineage and
wiki-side suspension plans, the frozen export plans, the assembled
evidence's `operation_id`, and both journals' revision-3/4 events.

What this driver deliberately does not do: it grants no activation and
activates no production installation — every pinned receipt keeps
`activation_allowed` false and each scenario ends at machine-readable
evidence (FINALIZED or ROLLED_BACK), not a running system. It does not
expose the snapshot producer's `--follow-symlinks` (a real rehearsal refuses
symlinked sources), does not migrate credentials, does not invent Knowledge
workspaces (the cutover target is bootstrapped from the staged migration
candidate through the documented library incantation run via the independent
interpreter, and the window stand-ins are pinned to it byte-for-byte), and
does not treat the work root as a product Home — nothing in it may be moved
into service. The window scenario's reverse chain and rollback are themselves
a rehearsal: they run against the driver's own enrolled products inside the
work root, never against a live installation.
