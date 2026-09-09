# Phase 0B Catalog split rehearsal

This remains a rehearsal, not a production migration. The core
`knowledge_bases`/`knowledge_documents`, Connector identity/lease, Feishu
Credential/Vault metadata, Read Later, Structured Asset, QueryResult,
Processing Job, Semantic Authoring Job, Database Source and TaskNotification
split slices now have
executable runners; real provider/production migration work remains open. It must run against an
explicit copy of the current Catalog and must not change the active revision
or the PuddingClaw database.

## Executed full migration orchestration

`backend/knowledge_platform/catalog/migration_orchestrator.py` now provides
the missing operational boundary above the individual slices. It requires an
injected drain controller, rejects source/Platform/Harness database aliases,
prepares independent Platform and Harness schema histories, runs the
ownership-ordered slice adapters, copies Harness-owned `worker_access_logs`,
and emits a versioned `CatalogMigrationManifest`. The source state digest must
be identical before and after the run; the active revision remains unchanged.

The orchestration test injects failures after drain, schema, copy and before
verification, and compares the complete pre-run state of both targets. The
manifest schema is `catalog-migration-manifest.schema.json`. This proves the
production-copy workflow against an explicit database copy, but a real
installation still needs a production drain controller and a real Vault
provider before any cutover authority can be granted.

## Invariant

At every failure point either the legacy source remains authoritative or the
new Catalog is complete and independently verifiable. There is no long-lived
dual-write period. A process may prepare a target, but only one audited
revision pointer may be active for a request.

## Replayable sequence

1. Freeze a source snapshot and record source migration version, database
   digest, file-root digest and active revision.
2. Create a target SQLite file/schema with `KnowledgeSessionFactory`; create a
   separate Harness schema with `HarnessSessionFactory`.
3. Copy each row in ownership order, redacting secret values and preserving
   only credential references. Record row count and primary-key digest per
   table.
4. Verify intra-owner foreign keys, normalized content digests, file/object
   reachability, lease state and secret redaction.
5. Inject failures after each table, after file copy, and before pointer CAS.
   Rerunning the same migration must not duplicate rows, jobs, credentials or
   indexes.
6. Verify the target can start with the legacy source unavailable, but do not
   activate it. Verify rollback leaves the legacy source untouched.
7. Only a later Phase 8 deployment rehearsal may exercise pointer CAS; the
   Phase 0B rehearsal records the proof inputs and expected outcomes here.

## Executed core slice

`backend/knowledge_platform/catalog/rehearsal_runner.py` accepts independent
source and target SQLAlchemy connections/engines. Its rollback-probe wrapper
first prepares the target schema, then reflects only the two legacy source
tables and maps them to Platform-owned `knowledge_spaces`,
`knowledge_assets` and versioned `knowledge_datasets`, and stores no host
filesystem path or credential value in the target. The runner applies the
same immutable rows twice, compares normalized source/target snapshots, and
emits the following machine-readable envelope:

```text
agent-knowledge-platform-catalog-rehearsal/v1
```

The executable test covers a real SQLite copy, `knowledge://` mappings,
nested secret redaction, file-reference reachability, report serialization,
retry idempotency, and failures injected at all five checkpoints. Each probe
must leave the complete target database state unchanged, including schema
history and indexes, while the source remains readable. The report records
`active_revision_changed: false` and only the checkpoints actually executed
by the wrapper; its structure is validated by
`catalog-rehearsal-report.schema.json`. This is a core-slice exit artifact; it
does not claim that the full 18-table ownership matrix has been copied.

## Executed Connector slice

`backend/knowledge_platform/catalog/connector_rehearsal.py` extends the same
proof to the legacy `knowledge_source_connections`, `knowledge_source_items`
and `knowledge_sync_runs` tables. It writes Platform-owned
`knowledge_connectors`, `knowledge_source_items` and `knowledge_sync_runs`
rows with stable application IDs, while preserving only `vault://`,
`secret://`, `credential://` or `ref://` credential references. Source URLs
are digested, path values containing URLs or absolute host paths are digested,
and an explicit file checker proves physical references before copy.

Sync leases are fail-closed: only the known queued/running/terminal states are
accepted; running rows require owner, heartbeat and future expiry at the
explicit rehearsal `as_of`; queued and terminal rows must not carry a lease.
The wrapper probes schema/core/connector checkpoints and compares the complete
target database state after each rollback. Existing target rows are excluded
from the source-slice equality snapshot but are recorded as
`target_out_of_scope_tables`, so they cannot be silently mistaken for migrated
data.

## Executed Credential/Vault metadata slice

`backend/knowledge_platform/catalog/credential_rehearsal.py` migrates the
non-secret metadata of `feishu_app_credentials`, `feishu_user_grants`, and
`feishu_oauth_sessions` into Platform-owned Credential, Grant, and OAuth
Session tables. App, token, and PKCE verifier values are never read from the
Vault by the rehearsal; only references matching the restricted
`vault://`, `secret://`, `credential://`, or `ref://` shape are retained.
Invalid, pending, or suspicious references fail the rehearsal before any
target write rather than being treated as copied secrets.

OAuth `state_hash` is retained as a digest and `redirect_uri` becomes a
digest; the raw callback URL and verifier never enter target rows or the
machine report. Source app/grant/OAuth relationships are checked before any
target write. Schema version 2 records the credential tables, and the wrapper
probes schema, credential, grant, OAuth, and verification checkpoints while
comparing complete target row-content digests after rollback.

## Executed Read Later / WebCapture slice

`backend/knowledge_platform/catalog/read_later_rehearsal.py` maps the legacy
`read_later_items` to the Platform-owned `knowledge_web_captures` table. The
legacy `knowledge_import_jobs` and `knowledge_import_events` rows are required
dependencies, but their Platform copy is owned exclusively by the Processing
slice. The adapter
re-checks the legacy URL normalization contract, then stores only URL and
source-path digests; content, raw snapshots and job sources are represented by
stable `knowledge://` URIs. Capture/job/event relationships are checked
against the legacy base, document, connector, source-item and sync-run rows
before any target write.

Ingestion jobs retain the legacy queued/running/terminal state and explicit
lease fields. A running lease requires an explicit rehearsal `as_of`, a
present owner/heartbeat and a future expiry; physical source/storage paths
require an explicit reachability checker. The wrapper probes schema, capture,
job, event and verification checkpoints, compares complete target state after
each rollback, and records pre-existing target rows as out-of-scope. This is
still a rehearsal: no read-later worker or production fetch path is switched.

For the structured-asset slice, `after_schema` is explicitly a post-schema
preflight checkpoint: the v4 DDL/history is prepared and checked before the
data transaction. SQLite DDL is not represented as an atomic data-transaction
rollback claim; fresh migration, repeated migration, and v3-to-v4 history
advancement are verified separately. The remaining checkpoints prove the
structured-asset data rollback itself.

## Executed Structured Asset slice

`backend/knowledge_platform/catalog/table_asset_rehearsal.py` maps
`knowledge_table_assets` to `knowledge_structured_assets`. The target keeps
Sheet, Schema, Profile and reference status, plus stable source/profile URIs;
host storage and profile paths become digests and are checked only through an
explicit reachability checker. Legacy base/document ownership, profile/count
consistency, ID length budgets, status allowlists, replay and rollback are
verified before the slice is considered safe. Schema v4 and migration history
are validated for complete columns, PK, indexes, unique constraints and FK
shape, including pre-created pending tables.

## Executed QueryResult slice

`backend/knowledge_platform/catalog/query_result_rehearsal.py` maps
`analytics_query_results` to `knowledge_query_results`. The target is not
owned by a Claw Session or ToolCall: those values are correlation digests only.
SQL, artifact paths and profile/correlation secrets are sanitized or digested;
large result rows remain behind a stable artifact URI. Status, row-count,
artifact reachability, idempotent replay, out-of-scope rows and rollback are
checked. Schema v5 uses the same fail-closed migration/history validation.
This remains metadata rehearsal only; no QueryResult repository or query
execution path has been switched.

## Executed Processing Job slice

`backend/knowledge_platform/catalog/processing_job_rehearsal.py` maps
`knowledge_import_jobs` and `knowledge_import_events` to
`knowledge_processing_jobs` and `knowledge_processing_events`. Import kind,
file metadata, source asset links, progress and lease fencing are retained;
the host source path becomes an input digest plus stable `knowledge://` URI.
The adapter validates legacy base/document/connector/source-item/sync-run and
event relationships before writing, accepts `staged` as a non-running state,
and requires owner/heartbeat/future expiry plus explicit `as_of` for running
jobs. It probes schema, job, event and verification checkpoints with complete
target-state rollback and records out-of-scope rows. The existing import
worker remains untouched; Semantic Authoring Job is implemented as a separate
slice below because its lifecycle and ownership fields differ.

## Executed Semantic Authoring Job slice

`backend/knowledge_platform/catalog/authoring_job_rehearsal.py` maps
`semantic_dimension_build_jobs` and `semantic_dimension_build_events` to
Platform-owned `knowledge_authoring_jobs` and `knowledge_authoring_events`.
The target keeps semantic dimension, adapter, progress, result and lease
metadata, while Session/Query identifiers become non-authorizing correlation
digests. Requested scope, input/result snapshots and event metadata are
recursively sanitized; staging and published paths are checked by an explicit
file checker and stored only as stable `knowledge://` URIs plus digests.

The adapter uses a status allowlist, requires an explicit `as_of` for running
leases, and rejects leases on waiting/published/failed/cancelled states. It
checks event ownership, a same-owner target FK, derived-ID length budgets,
immutable replay, out-of-scope target rows and v8 schema/history before
comparing the complete target state after each rollback probe. Relative and
`file://` references are included in the explicit reachability check. No
semantic authoring worker, Graph or Tool path has been switched, and active
revision remains unchanged.

## Executed Database Source split

`backend/knowledge_platform/catalog/database_source_rehearsal.py` maps the
legacy `knowledge_database_sources` table to the Platform-owned
`knowledge_database_connectors` table. The target keeps database type, host,
port, database name, username, selected tables and sanitized metadata. It has
no password column: a legacy password produces only a deterministic
`vault://` placeholder marked `pending_rebind`, while an existing credential
reference is accepted only when it passes the restricted reference policy.

The rehearsal checks KnowledgeBase ownership, supported database types and
ports, selected-table uniqueness, unsafe credential references, recursive
metadata redaction and idempotent replay. Its three failure probes restore the
complete target database state, including schema history and row-content
digests. This is a metadata split only; the placeholder is not evidence that
secret bytes have been copied, and the real provider rebind remains a separate
Vault operation.

## Executed TaskNotification split

`backend/knowledge_platform/catalog/notification_event_rehearsal.py` maps
legacy `task_notifications` to the append-only Platform
`knowledge_notification_events` table and the framework-neutral
`knowledge_contracts.NotificationEvent`. The target removes `read_at` because
read/acknowledgement state belongs to the Harness inbox consumer; subject IDs
are external stable IDs and are not database FKs. Session and Query IDs are
stored only as correlation digests, and the event payload is explicitly
allowlisted.

The runner proves deterministic replay, message/path sanitization, payload
rejection, report-schema compatibility and complete target-state rollback at
schema, event-write and verification checkpoints. It does not rewire the
legacy notification writer or claim that a Harness consumer has been deployed.

Schema version 11 adds an explicit event-to-Space ownership binding. Only an
event with an independently proven `space_id` is discoverable by the Platform
Admin notification read surface; legacy event rows without that binding remain
hidden. A single local Space, matching subject IDs, or a generic scope URI is
not sufficient evidence for this binding, and no read/acknowledgement state is
copied into the Platform Catalog.

## Executed non-secret Vault rebind/rotation rehearsal

`backend/knowledge_platform/catalog/vault_rebind_rehearsal.py` defines the
`VaultReferenceOperator` boundary. The provider owns secret bytes and exposes
only readability checks, server-side rebind/rotation, compensation, and a
non-secret signed provider state proof verified by an independent callback.
The rehearsal never calls a `get-secret`
operation and its manifest contains only source-reference digests and safe
target/rotated references.

Three failure probes (`after_rebind`, `after_rotation`, and `before_finalize`)
must restore the provider state digest before the probe can pass. Successful
replay must produce identical binding evidence, and the old reference must
remain readable during the installation rollback window. The current fixture
is an opaque provider contract test; a production Vault adapter and real
installation copy are still required before Phase 0B can exit.

## Exit evidence

The rehearsal is complete only when a machine-readable report contains source
and target counts/digests, injected failure checkpoints, retry outcomes,
redaction checks, and an explicit `active_revision_changed: false` result.
The Read Later, Structured Asset, QueryResult, Processing Job, Semantic
Authoring Job, Database Source and TaskNotification slices are independently
executable, but the
full Catalog rehearsal remains open until real Vault rebind/rotation and a real production-copy
drill are complete. Processing and Semantic Authoring Job slices remain
separate because their lifecycles and ownership fields differ.
