# Session-domain reverse migration candidate

The independent Harness distribution can now materialize post-cutover session
changes into a new inactive legacy Home candidate. It uses a verified raw Claw
snapshot, an unchanged session baseline, and the current enrolled Harness Home.
Both independent target writers are suspended through the durable writer barrier
before the current Harness session files are captured. Source and current Homes
are never modified by candidate copying; the explicit suspension operation does
freeze and revoke both target writers.

```sh
python -m harness.session_reverse \
  --source-snapshot /absolute/private/verified-claw-snapshot \
  --target-before /absolute/private/baseline-home-payload \
  --harness-home /absolute/private/current-harness-home \
  --knowledge-state /absolute/private/current-knowledge-workspace \
  --knowledge-python /absolute/knowledge/.venv/bin/python \
  --checkpoint-dir /absolute/private/writer-barrier \
  --operation-id rollback-session-1 \
  --staging /absolute/new-private/reverse-candidate
```

The baseline's session files must match the source snapshot exactly. Current
files under `sessions`, `data/attachments`, `data/large-tool-results`,
`data/harness-scratch` and `data/harness-rewind` replace those domains in the
candidate. Inserts, updates and deletions are realized as actual files. Empty
session lock files and empty owned directories are not restored. Every other
source file remains byte-identical, including source settings and other data
stores. A source file with the reserved `.reverse-part` suffix is rejected.
Session data remains opaque: no new permission grants or external URI rewrites
are inferred from stored conversation contents.

The verified source snapshot stays admitted throughout copying. The frozen
Harness Home holds exclusive admission and its suspended authority lease during
capture. The plan binds source commitments, baseline, current session inventory,
both-product barrier and output identity. Each copied file is hash-checked and
published through a private fsynced part; created parent directories are synced.
The copying manifest permits exact retry. Completed missing, changed or unknown
candidate content is rejected without silent repair. Final verification checks
both revocations, source bytes, baseline and current session inventories, plus
the entire candidate inventory before publishing `verified_inactive`.

This candidate reverses only the session file domain. By default source settings, Catalog,
Wiki, indices and credentials have not incorporated their target-side changes.
It must not be used as a runnable rollback Home until those domain reversals,
credential continuity and the audited installation revision/thaw gates pass.
The original raw snapshot may retain Catalog WAL siblings that still require
normalization. This command does not claim SQLite consistency or complete
installation rollback. Output flags keep activation and rollback completion false.

Bounds remain those of the existing source snapshot (2 GiB per file, 16 GiB
aggregate, 50,000 entries) and session import (128 MiB per file, 2 GiB aggregate,
10,000 files). The session import budgets were sized for small synthetic
fixtures and were raised after the specification section 11.20 item 10
real-data rehearsal measured a real PuddingClaw Home (67 MB largest document,
about 300 MiB of sessions, 50 MB catalog.sqlite3) exceeding them; the source
snapshot envelope remains the outer denial-of-service gate. Output files are
private. Current Harness files may retain normal
runtime file modes inside its private Home, but must be owned, regular and
unlinked. Arbitrary nonparticipating writers and hostile same-user file/lock
replacement remain outside the cooperative process fence. Changed inputs reject;
there is no automatic thaw or rollback of a completed suspension.

## Include generic Harness settings

Add `--include-generic-settings` to the same command to reverse `compression`,
`cache`, `subagents`, `harness` and `write_middleware` alongside session files.
Source snapshot, baseline Home and current Harness Home must each have a bounded,
owned, unlinked config.json. The baseline's validated generic projection must
match the source snapshot. Unsupported target sections must be identical between
baseline and current config; any change requires a separate reverse migration
and is rejected. A rejection during current-target validation can leave both
products suspended; no automatic thaw is performed.

Selected sections are replaced from the current validated target, including
section removal. All other source configuration fields are retained as JSON
values, including knowledge configuration and source credential references.
Formatting/order may change in the generated config.json. Preserving old
credential values does not prove they are still valid or usable: credential
continuity remains explicitly unverified. No target credential value is promoted
through this generic projection. Plan/receipt contain hashes and fixed section
names, never configuration values. Target/baseline bytes are checked again before
completion; generated settings are covered by the candidate inventory. Reusing a
candidate with a different settings option or changed settings rejects.
