# Legacy rollback activation

`harness.rollback_orchestrator` stops at an authority assignment: both new
products remain frozen and the legacy writer is named in their journals. That
checkpoint is an input to `harness.rollback_activation`; it is not restoration
completion and therefore carries `rollback_completed=false` even when its
state is `both_reassigned`.

Before CUTOVER, capture the credential inventory while the legacy installation
gate can be held exclusively:

```sh
python -m harness.rollback_activation capture-credentials \
  --source-home /absolute/legacy-home \
  --output /absolute/private/credential-baseline.json \
  --operation-id cutover-operation
```

After the reverse candidate and `both_reassigned` window checkpoint exist, run:

```sh
python -m harness.rollback_activation activate \
  --source-home /absolute/legacy-home \
  --harness-home /absolute/harness-home \
  --knowledge-home /absolute/knowledge-home \
  --candidate /absolute/document-reverse/reverse \
  --knowledge-python /absolute/puddingknowledge/python \
  --checkpoint-dir /absolute/private/rollback-activation \
  --rollback-checkpoint /absolute/window-checkpoint/checkpoint.json \
  --credential-baseline /absolute/private/credential-baseline.json \
  --source-operation-id cutover-operation \
  --rollback-operation-id rollback-window-operation
```

The activator holds `.installation-gate-v1.lock` exclusively and verifies the
operation-bound source freeze and final admission capability. Its committed
order is:

1. copy the reverse candidate to a private versioned directory inside the
   legacy Home;
2. run the installed Knowledge path-rebind CLI against that final path;
3. rebuild every SQLite index and run `quick_check` plus `foreign_key_check`;
4. compare the legacy credential tree with the pre-CUTOVER baseline;
5. atomically replace only `db/catalog.sqlite3`, preserving the prior Catalog;
6. read the installed Catalog and every candidate-owned document body;
7. prevalidate, archive, and remove both new products' identical
   `active-installation.json` pointers;
8. hardlink the source freeze marker to its `.part` denial name, archive the
   marker, repeat the health probe, and restore the marker on failure;
9. remove the denial part, publish the activation receipt, and finally publish
   `rollback_completed=true`.

Every earlier checkpoint has `rollback_completed=false`. A crash while the
marker is being archived leaves `.installation-freeze-v1.json.part`, so normal
legacy startup remains denied. A crash after the final unlink is adopted from
the digest-bound ready receipt and retired marker. Exact retry verifies the
retired source marker and both retired active pointers. It deliberately does
not hash the live legacy Catalog again after completion because the admitted
writer may have committed legitimate new work.

The `window-rollback` rehearsal performs this complete sequence. Its source
freeze step captures the credential baseline, its frozen export reads the real
four-event post-cutover Knowledge journal, `window-rollback` ends at authority
reassignment, and `window-activate` produces the only terminal
`rollback_completed=true` receipt.

The activator never replaces the Home directory or its admission controls.
Harness and Knowledge remain frozen and assigned away. `production_activated`
stays false: the receipt proves local legacy restoration and writer admission,
not an external traffic switch or soak decision.
