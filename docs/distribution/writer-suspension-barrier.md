# Two-product writer suspension barrier

Section 11.20 requires both target products to stop writing before reverse
migration. The Harness coordinator now verifies and records revocation in both
independent enrolled writer journals, in addition to the persistent freeze each
product publishes. This command does not enroll, activate or thaw installations.

```sh
python -m harness.writer_barrier \
  --harness-home /absolute/private/harness-home \
  --knowledge-state /absolute/private/knowledge-workspace \
  --knowledge-python /absolute/independent-knowledge/.venv/bin/python \
  --checkpoint-dir /absolute/new-private/checkpoint \
  --operation-id rollback-stop-1
```

Both products must already be enrolled with their own writer-authority CLI.
Their data roots, authority roots and the checkpoint directory must be disjoint.
The checkpoint plan binds both enrollment records, checkpoint directory identity
and the explicitly selected executable identity. Knowledge is invoked through
its installed, versioned CLI; Harness never imports Knowledge implementation.
The caller must select and maintain a trusted compatible installation. Interpreter
identity is not a signature or immutable attestation of its site-packages.

Before the first suspension, both current journals are checked. Harness then
freezes and suspends its writer and commits `harness_suspended`. Knowledge freezes
and suspends its two domains through its CLI; the coordinator verifies the full
revision chain, binding, actual journal and actual freeze marker before committing
`both_writers_suspended`. The result records both journals, including all three
null writers. Mere successful process exit cannot satisfy the barrier.

Contention fails without waiting for a lock. A Knowledge writer may keep the
operation at `harness_suspended`; stop that writer and retry the exact command.
A failure after product suspension but before checkpoint publication is recovered
by inspecting the product's committed journal. Completed checkpoints never
silently downgrade. Removed committed freeze markers, changed operation or
binding, replaced checkpoint roots, and invalid child receipts reject. No rollback
of partial revocation or automatic marker recreation is attempted. The child
inherits the coordinator lease so parent death does not admit another coordinator
while the delegated command is still running. Private interrupted checkpoint
replacement temporaries remain diagnostic and are not committed state.

The barrier is a prerequisite to reverse export/import and installation revision
switching. It is not CUTOVER, ROLLED_BACK, a unified active-installation assignment,
credential continuity or proof of lossless reverse migration. Existing cooperative
POSIX runtime limitations apply, including nonparticipating writers, binding/lock
removal and hostile same-user path replacement. Production activation remains false.

Installed integration tests require an explicitly selected independent Knowledge:

```sh
KNOWLEDGE_TEST_PYTHON=/absolute/knowledge/.venv/bin/python \
  python -m pytest tests/test_writer_barrier.py
```

Without that explicit dependency the integration tests skip; a skip is not proof
of the cross-product contract. Pure receipt tests run independently.

## Installed-code continuity

The plan now includes the versioned installed Knowledge identity, captured before the first Harness freeze/suspension. Control checks compare fresh observations before and after delegated work and before publishing completion. Same-version inventory changes or older plans without this binding reject without downgrading a completed checkpoint. Partial freezes remain frozen; restoring the exact trusted installation permits explicit retry. This RECORD-based observation is unauthenticated and does not fence concurrent package replacement, dependencies, bytecode or hostile interpreters. It grants no new writer authority.
