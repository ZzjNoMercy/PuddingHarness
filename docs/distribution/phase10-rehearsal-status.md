# Phase 10 rehearsal status

This note records what the rehearsal driver currently proves, which files
implement it, and the commands that verify it. It is a status record, not a
design document; `rehearsal-driver.md` carries the contract details.

## Scenarios

* `path-a` (default): forward chain snapshot → request → orchestrate →
  discover → prepare → enroll → suspend → cutover → finalize, ending at a
  FINALIZED manifest with both products thawed onto the new writers.
* `window-rollback`: the same forward chain through cutover (steps 1–8, no
  finalize), then the real reverse chain against the post-cutover state —
  `window-delta` (deterministic Knowledge-era title edit), `window-suspend`
  (both products fenced under the window operation by the real suspend CLIs),
  `window-export` (document-lineage stand-in bootstrap, edit, enroll,
  suspend, frozen export, target-before/after pins), `window-disposition`
  (other-catalog reverse), `window-document-reverse` (reverse bundle with the
  edit surviving into the legacy candidate), `window-wiki-absent`
  (version-1 wiki-shadow export and absent-wiki attestation),
  `window-evidence` (rollback evidence assembly), `window-rollback`
  (`harness.rollback_orchestrator window-rollback`) — ending at a ROLLED_BACK
  manifest with both writers reassigned to puddingclaw.

Both scenarios bootstrap the Knowledge cutover target from the staged
document-migration candidate
(`open_persistent_workspace(state, document_migration=<staging
candidate>)`), so the rehearsed workspace holds the real migrated documents.
The window scenario's lineage and wiki-side stand-ins are byte-pinned to that
bootstrap; see `rehearsal-driver.md` for the pin contract and for why the
absent-wiki proof cannot come from a product bootstrap.

## Files

* `backend/harness/rehearsal_driver.py` — both scenarios, the checkpoint and
  resume contract, and both terminal verifications.
* `backend/tests/test_rehearsal_driver.py` — path-A end-to-end, SIGKILL
  resume, duplicate-run, tamper and parameter-change refusal tests plus the
  Harness-only checkpoint/hygiene unit tests.
* `backend/tests/test_rehearsal_driver_window.py` — window-rollback
  end-to-end (terminal assertions, reversed-candidate content, lineage
  pins), mid-reverse-chain SIGKILL resume, duplicate run, tampered-evidence
  refusal, window-operation validation, and the step-table unit test.
* `docs/distribution/rehearsal-driver.md` — operator-facing contract for
  both scenarios.

## Verification

Run from `backend/` with an explicit independent Knowledge interpreter:

```sh
KNOWLEDGE_TEST_PYTHON=/path/to/knowledge/.venv/bin/python \
  uv run --no-sync python -m pytest -q \
  tests/test_rehearsal_driver.py tests/test_rehearsal_driver_window.py \
  tests/test_window_rollback.py tests/test_rollback_orchestrator.py \
  tests/test_writer_barrier.py
KNOWLEDGE_TEST_PYTHON=/path/to/knowledge/.venv/bin/python \
  uv run --no-sync python -m pytest -q \
  tests/test_cutover_orchestrator.py tests/test_harness_migration_orchestrator.py \
  tests/test_installation_manifest_advance.py tests/test_installation_manifest.py
```

Latest local results: 77 passed (driver + window + rollback + barrier
suites), 104 passed (cutover + migration orchestrator + manifest suites),
and `ruff check` clean on the changed files. The repository does not enforce
`ruff format` (its house style predates it), so formatting is held to the
surrounding code style instead.
