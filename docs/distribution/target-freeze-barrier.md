# Two-product target freeze barrier

Specification 11.20 requires both new products to stop writing before reverse
migration. This installed Harness command coordinates their existing cooperative
freeze protocols and records durable partial completion:

```sh
python -m harness.target_freeze \
  --harness-home /absolute/private/harness-home \
  --knowledge-state /absolute/private/knowledge-state \
  --knowledge-python /absolute/knowledge-venv/bin/python \
  --checkpoint-dir /absolute/new-private-checkpoint \
  --operation-id rollback-operation-1
```

Stop managed Harness services and Knowledge workers first. The three roots must
be disjoint. Only an existing owned Knowledge workspace is accepted. The selected
Knowledge interpreter is trusted local configuration; Harness delegates the
versioned `puddingknowledge-workspace-freeze/v1` process contract and never imports
Knowledge source. Standard virtualenv interpreter symlinks are supported, while
the resolved executable bytes and identity are bound into the immutable plan.

The durable states are `harness_frozen` and `both_targets_frozen`. They are **not**
installation PREPARED, CUTOVER or ROLLED_BACK. The plan binds both directory
identities, operation, interpreter and checkpoint root. A failed Knowledge call
leaves Harness frozen with its checkpoint. Repeating the exact command validates
previous commitments before resuming. Completed checkpoints are never downgraded
on a failed retry. Removing a previously committed marker rejects; the coordinator
does not silently recreate it. Receipt hashes bind actual private marker bytes,
and Knowledge also verifies its own domain manifest before returning a receipt.

The permanent checkpoint lock is inherited by the Knowledge child, so parent
SIGKILL cannot admit a competing coordinator while that child remains live.
Atomic checkpoint replacement uses file and directory fsync. Interrupted private
temporary files are not committed checkpoints and are retained for diagnosis.
If Harness is killed while publishing its own freeze, its unresolved CLI gate can
still require manual reconciliation; this command adds no unsafe automatic cleanup.

This is a cooperative local POSIX barrier. Arbitrary old binaries, external stores,
malicious same-user path/lock replacement and an untrusted configured interpreter
are outside its fence. Identity checks reject observed directory/executable drift;
they do not turn path access into a security boundary against hostile same-user
code. No automatic thaw exists, and failed partial completion remains frozen.
Use disposable installation copies until an audited writer revision, lossless
reverse migration and credential continuity are implemented and validated.

A successful receipt always leaves `activation_allowed`, `rollback_completed`,
`installation_cutover_performed` and `external_writers_fenced` false. The embedded
CLI candidate built at `bc2b422` predates this Python orchestrator; this stage is
validated through the independent noneditable backend distribution, not a newly
rebuilt npm candidate.
