# Offline Harness session-domain import

The independent Harness package provides an explicit resumable session-file
import. Run with the installed Harness environment's Python:

```sh
python -m harness.session_import \
  --source-snapshot /absolute/offline/claw-snapshot \
  --staging /absolute/private/session-import
```

The source is an operator-created offline snapshot. This command does not
locate or lock an active Claw installation. Source and staging must be disjoint.
Staging is private and inactive; the result is `verified_inactive`, not the
installation manifest's `PREPARED` or `CUTOVER` state.

Owned roots currently included are:

- `sessions` (history, archives and trace sidecars)
- `data/attachments`
- `data/large-tool-results`
- `data/harness-scratch`
- `data/harness-rewind`

Files are copied byte-for-byte. Historical control fields remain in the stored
copy; SessionManager's existing read projection handles retired selectors.
Messages and tool results are opaque data. External resource references and
old permission grants are not rewritten or reauthorized. Knowledge databases,
Wiki/index data, configuration, provider credentials, skills and unrelated
source directories are not imported. Settings and cross-domain references need
a separate ownership-aware migration step before installation activation.

The importer inventories content digests, writes an owned manifest, takes an
exclusive staging lock and copies each file through an fsynced temporary file.
After a process is killed, the same source and staging can resume: already
verified files are reused, partial temporary files can be rewritten and the
completed payload is verified again. A changed source, tampered completed file,
unknown staging file or forged manifest flags is rejected. Successful replay
reports `idempotent=true` and creates no duplicate sessions. Empty `.lock` files
are not imported and do not establish writer fencing.

Source bytes are read only. Symlinks, hardlinked files and non-regular files
are refused. Limits are 128 MiB per file, 2 GiB total and 10,000 files. The
budgets were originally sized for small synthetic test fixtures; the
specification section 11.20 item 10 real-data rehearsal measured a real
PuddingClaw Home (67 MB largest document, about 300 MiB of sessions, 50 MB
catalog.sqlite3) that exceeded them, so they were raised to the unified
migration budgets shared with Knowledge. The source snapshot envelope
(2 GiB per file, 16 GiB total) remains the outer denial-of-service gate.
Staging uses a 0700 directory and 0600 files; reports expose counts and
digests, not session contents or source paths. The local manifest contains relative file
names and digests required for verification and resume, so it must remain
private. Filesystem checks are not a defense against a hostile process racing
path replacement outside the staging lock.

Do not point a running product at the staging payload. It is an immutable
migration candidate, not an active Home. No settings migration, source writer
fence or active-installation revision switch is performed; these fields remain
false and cannot be promoted through manifest edits. Cross-domain coordination
with Platform, credential rebind, shared checkpoints and lossless installation
rollback remain unfinished.

## Validation

From the independent repository:

```sh
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q backend/tests/test_session_import.py
```

The tests cover a killed importer, lock release, resume/idempotency, source
changes, staged content and manifest tampering, path boundaries, byte-preserved
history/attachments and real SessionManager readback. A non-editable staged
package was also tested outside the source checkout without development tools.

The extraction staging audit still reports four existing findings in evaluation
contracts, legacy-selector projection and dynamic-import review. Those findings
keep `releaseable=false`; passing this import test does not bypass that gate.
