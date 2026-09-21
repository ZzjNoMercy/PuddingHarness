# Rehearsal driver: checkpointed end-to-end forward chain

`harness.rehearsal_driver` drives the specification section 11.20 item 10
real-data rehearsal as one ordered command: it runs the shipped forward
migration chain — source-home snapshot, Knowledge migration request
generation, offline migration orchestration, manifest discover and prepare,
product enrollment, writer suspension, cutover and finalize — as ordered REAL
subprocesses against one private work root, so the rehearsal exercises the
same CLIs an operator would run, in the same order, and proves their
committed outputs compose. This increment covers the forward path only
(DISCOVERED, PREPARED, CUTOVER, FINALIZED); the window-rollback path is a
separate later increment.

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

Work-root layout (the work root itself is 0700; everything inside it is owned
by the caller and symlink-free, files are private 0600 single-linked, and
directories are sealed against group/other writes — chain-owned intermediate
directories keep the modes their producers create):

    snapshot/               rehearsal_snapshot envelope (plan/manifest/payload)
    snapshot-receipt.json   producer receipt (lives outside the envelope)
    request/                Knowledge request + generation receipt
    staging/                migration orchestrator staging (harness/, knowledge/)
    knowledge-receipt.json  receipt extracted from the orchestrator result
    manifest/               installation manifest (DISCOVERED..FINALIZED)
    harness-home/           fresh Harness product Home, enrolled
    harness-authority/      Harness writer authority (journal, retired markers)
    knowledge-home/         fresh Knowledge workspace, enrolled
    knowledge-authority/    Knowledge writer authority
    knowledge-setup/        bootstrap fixture catalog/wiki for the workspace
    barrier/                writer suspension barrier checkpoint
    checkpoint/             cutover orchestrator checkpoint
    driver-checkpoint.json  this driver's private checkpoint
    run-record.json         machine-readable rehearsal evidence

The ordered step table is `snapshot, request, orchestrate, discover, prepare,
enroll, suspend, cutover, finalize`. Each step invokes its real CLI
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
through the independent interpreter before the enroll CLI, mirroring the
cross-product test incantation.

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
directory and the enrollment roots are regenerated. Re-running a completed
scenario verifies all nine steps and compares or republishes a byte-identical
`run-record.json`. The run record is canonical JSON listing each step's name,
committing-execution idempotence flag, committed-receipt digest and output
digests, plus the terminal FINALIZED bindings (manifest, prepared and cutover
manifest, active pointer and both assigned event digests); it carries
digests, labels and counts only — no secrets and no row content. After the
last step the driver re-verifies the terminal state against the real
artifacts before publishing: FINALIZED manifest with closed rollback window
and cutover writer set, both journals' assigned revision-2 heads, retired
freeze markers and thaw receipts, the active-installation pointer, and a
successful Harness installation-guard admission. Failure anywhere prints one
bounded single-line JSON error (`rehearsal_driver_rejected`, with the step
name and the step CLI's own error code when one exists) and exits 1.

What this driver deliberately does not do: it performs no window rollback
(path B) and exposes no rollback CLI surface — the rollback increment will
append steps to the ordered table, and the checkpoint's ordered,
digest-verified step records are shaped so those steps can be appended
without changing the existing contract. It grants no activation and activates
no production installation: every pinned receipt keeps `activation_allowed`
false and the run ends at FINALIZED evidence, not a running system. It does
not expose the snapshot producer's `--follow-symlinks` (a real rehearsal
refuses symlinked sources), does not migrate credentials, does not invent the
Knowledge workspace bootstrap (it runs the documented library incantation
through the independent interpreter), and does not treat the work root as a
product Home — nothing in it may be moved into service.
