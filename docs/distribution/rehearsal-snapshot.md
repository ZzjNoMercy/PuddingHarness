# Rehearsal source-home snapshot producer

`harness.rehearsal_snapshot` assembles a `puddingclaw-source-home-snapshot/v1`
envelope from a live legacy PuddingClaw Home plus optional grafted external
trees, then opens its own output with the real
`harness.source_snapshot.VerifiedSourceSnapshot` admission check; any admission
failure is a producer bug. It exists to drive the specification section 11.20
item 10 real-data rehearsal, where the offline migration chain must run against
a snapshot of a real legacy Home with grafted knowledge corpora. The source
home is strictly read-only: files are byte-copied, never moved or hardlinked.

```sh
python -m harness.rehearsal_snapshot \
  --source-home /absolute/legacy/.puddingclaw \
  --output /absolute/new-private/source-snapshot \
  --graft /Users/pet/Documents/knowledge=external/knowledge \
  --exclude scratch/domain \
  --repair-document doc_611a9b85a8184082ba09456b \
  --repair-reason "dangling storage_path target is missing" \
  --receipt /absolute/private/receipt.json
```

The snapshot root contains exactly `.installation-gate-v1.lock`, `plan.json`,
`manifest.json` and `payload/`. Every payload file is normalized to a regular
0600 single-linked file, every directory to 0700, all owned by the current uid;
no symlinks exist anywhere in the product. `plan.json` binds the sha256 of the
absolute source path, the source directory device/inode, the sha256 of the
output root path and the recursive payload inventory. `manifest.json` is the
fully determined admission manifest (`state` `verified_raw_home_snapshot`,
activation and installation flags false, catalog WAL policy
`captured_as_sibling_files`). plan.json and manifest.json are canonical JSON;
`output_identity` binds the root path and `source_directory_identity` binds the
source directory, so identical inputs at the same root produce byte-identical
files.

Default exclusions drop transient domains before capture: any `.DS_Store`
name, any name matching `*.pre-migration-*`, and the relative prefixes
`sessions/traces`, `cache`, `logs`, `state`, `db/backups` and `tmp`.
`--exclude REL` repeats to extend the prefix set; excluded directory subtrees
are never inspected, so even symlinks beneath them are ignored. Any other
symlink in the source refuses the run by default; `--follow-symlinks` instead
copies regular-file targets as ordinary files (symlinks to directories still
refuse). Repeatable `--graft SRC_DIR=DST_RELATIVE` copies an external tree under
`payload/DST_RELATIVE` with the same normalization and exclusion rules; graft
destinations must be prefix-disjoint from each other and from every planned
payload path.

`db/catalog.sqlite3` and any `-wal`/`-shm`/`-journal` sidecars are captured as
sibling bytes; the catalog is never opened with sqlite. A quiescence proof
hashes the catalog set, crosses an fsync barrier and hashes again; any changed
digest refuses the run, so writers must be stopped first. The optional repair
flow first byte-copies the catalog set into a private working copy, opens only
the working copy, deletes the named `knowledge_documents` row, requires clean
`PRAGMA foreign_key_check` and `PRAGMA integrity_check`, checkpoints to a
single file and places it at `payload/db/catalog.sqlite3` without sidecars.
The repair receipt records before/after digests and the deleted row's
id/title/storage_path only — never row content or secret bytes, matching the
receipt discipline for `.vault-keys` material elsewhere in the payload.

Re-running into the same output root with identical inputs verifies the
existing envelope and returns byte-identical plan/manifest and receipt files
with `idempotent: true`. A pre-existing non-empty root that is not the complete
snapshot those inputs would produce — different source, exclusions, grafts or
repair, or an interrupted earlier run — refuses instead of silently
overwriting; remove the foreign root explicitly. The optional `--receipt` file
is canonical 0600 JSON summarizing counts, bytes, applied exclusions, grafts,
repair and the snapshot commitment; it is deterministic for identical inputs at
the same output root and must live outside the root, the source home and graft
sources. Bounds are the admission contract's: 50,000 entries, 2 GiB per file,
16 GiB aggregate. The resulting snapshot is admission input for
`harness.migration_orchestrator.prepare_migration`; it grants no activation,
installation or credential rights on its own.
