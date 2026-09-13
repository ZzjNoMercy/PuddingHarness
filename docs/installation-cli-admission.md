# Installation CLI admission

The Harness deploy CLI uses a cooperative write admission protocol for the
shared Home. A writer briefly creates
`Home/.installation-cli-admission` as an exclusive gate, checks the Python
freeze marker (`.installation-freeze-v1.json` or its `.part`), and writes a
0600 random ticket under the private
`Home/.installation-cli-leases` directory. The ticket is fsynced before the
gate is released and remains present while the business operation runs.

Successful operations reacquire the gate and remove their own ticket with
directory and Home fsyncs. Failed or killed operations retain the ticket.
Gate remnants are treated as a refusal and are never auto-cleaned; a PID is
not evidence that a gate or ticket is stale. Nested calls in one async context
share the same ticket through `AsyncLocalStorage`.

This is a cooperative protocol for the current CLI and supervisor entry
points. It does not protect arbitrary scripts, old CLI binaries, or provide a
general rollback mechanism. Freeze and admission outputs do not expose Home
paths, command arguments, or secrets.

## Python admission and freeze

On POSIX, `app` and `evaluation.worker` acquire a shared process lease before
business imports. Managed subprocess call sites pass the same open-file
description, retaining admission if the parent dies before the child. Children
that close descriptors, remote MCP servers, containers and arbitrary external
writers are outside this guarantee; inherited host Docker CLI descriptors do
not fence a container. Non-POSIX startup checks marker presence only and does
not provide process exclusion or the freeze command.

`python -m harness.home_freeze --home ABSOLUTE_HOME --operation-id ID` takes
exclusive Python admission, the CLI gate, and any existing legacy backend
lease before publishing a private immutable marker. It binds the operation
and Home directory identity. It never activates, thaws or switches versions.
An already-running legacy backend blocks publication, but an old binary
starting later does not participate in this protocol.

A normal completed freeze supports exact same-operation retries. A handled
publication exception releases its gate, allowing retry of a complete part or
linked record. **SIGKILL or power loss while holding the mkdir gate retains
that gate and refuses automatic retry.** Likewise failed/killed CLI operations
retain their tickets, even after their PID disappears. These are unresolved
states, not a successful freeze or evidence that all descendants have exited.
An audited reconciliation authority is still required; deleting internal
files or restarting an old version is not a supported recovery procedure.

This repository's CLI sources have admission, but the embedded runtime bundle
has not been rebuilt by this change. Full installer/release acceptance must
verify version compatibility and container/external writer retirement before
claiming whole-installation CUTOVER or rollback.

The Node CLI write-admission implementation is POSIX-only. On Windows it
explicitly rejects write operations before creating Home or admission files;
read-only commands remain available. A native Windows writer/lease protocol
is not implemented or validated. The Python non-POSIX marker-only startup
compatibility path must not be interpreted as equivalent exclusion.
