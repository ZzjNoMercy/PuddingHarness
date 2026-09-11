# PuddingHarness backend packaging and historical extraction

The independent repository's `backend/` is the runtime authority. Backend
staging and the default Python audit read that source directly, including owned
non-Python resources. Historical overlays cannot replace a source file or supply
a missing one. The stage manifest records `runtime_authority` and no applied
backend overlays. Source and staged digests are checked before manifest writing;
these checks do not fence concurrent writers or provide an atomic snapshot.

The historical extraction material below is retained for provenance research.
Use `audit.py --legacy-extraction` only to inspect that historical transformation;
it enforces the original immutable overlay receipt and may reject its stale
inputs. That receipt is not current product build authority. Frontend extraction
still uses its separately documented workflow.

The source audit retains one exact reviewed compatibility finding; any source
drift makes it unresolved again. See
[`compatibility review`](../../docs/knowledge-platform/harness-compatibility-audit-review.md).
Staging still records `releaseable=false`.
Independent repository packaging does not prove installation migration, production
activation, or full product parity.

## Historical extraction notes

These are target-only cleanup overlays and an executable Python dependency audit.
They are not an independent repository or a complete Harness distribution.
PuddingClaw source remains the legacy product; do not apply these overlays there.

`audit.py` selects the proposed Python runtime and rejects references to excluded
business modules, active business protocol fields and unresolved dynamic imports.
Whole-domain exclusions follow the Knowledge Platform specification. Generic
browser connectors are retained; Knowledge source connectors are excluded with
the Knowledge domain. Tests, frontend, Skills execution environments and release
assets require their own checks. A static-clean report cannot prove runtime or
repository completion.

`overlay-provenance.json` records the reviewed source and target bytes. The audit
CLI refuses stale source, changed overlay content, or an unexpected overlay file
set. Re-review an overlay when its source changes; never overwrite newer source
by silently applying stale cleanup content.

```bash
python3 packages/puddingharness-extraction/audit.py --output /private/tmp/new-harness-audit.json
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q packages/puddingharness-extraction/test
node packages/puddingharness-extraction/test/frontend-boundary.test.mjs
node packages/puddingharness-extraction/test/frontend-staging.test.mjs
```

The reproducible effective frontend staging rules, exclusion list, dependency
options, and artifact manifest format are documented in
[`frontend-staging.md`](frontend-staging.md) and implemented by
`scripts/stage_frontend.mjs`. The staged build does not claim a live external
MCP resource adapter; arbitrary MCP resource URI resolution remains a host
integration boundary.

The test suite checks tool construction, real local attachment/workspace reads,
MCP configuration behavior, and AST navigation/composition boundaries. Provider
requests and external MCP resource adapters are mocked where indicated; those
tests do not prove live service integration. The effective backend now has an independent locked wheel installation and
real app/HTTP Agent validation. Research capability inheritance and external MCP
Resource interoperability remain open; no final Git repository extraction is claimed.

Existing image-result markers are a protocol consumed by the generic runtime;
their legacy product spelling remains until producer and consumer can migrate
together. An overlay must preserve that protocol and existing attachment/grant
bindings rather than replacing them with adapters no caller supplies.

History extraction must use the recorded common source revision and
`git-filter-repo==2.47.0`, preserving a commit map and cleanup commit provenance.
A source-only file copy is not a substitute. Actual extraction and production
release decisions remain separate from these preparation artifacts.

`test/target_runtime_loader.py` is an integration-test loader: it resolves the
effective target Python tree and refuses imports of excluded modules. It has
run a real DeepAgents graph and middleware stack with an offline scripted model,
a real workspace read, and Session persistence. Headless pause/resume and owned
activity-log SQLite I/O are also exercised. These use the development Python
environment, not an independently installed distribution or a live provider.
The complete FastAPI lifespan now initializes the owned SQLite schema and runs
an HTTP Agent request with an offline scripted model. Startup rejects legacy,
foreign, future-version and structurally incomplete catalogs before opening a
writable SQLite connection; failure and shutdown release database resources and
the instance lease. Independent backend packaging and installed-runtime tests
now also pass; PostgreSQL integration and historical import remain unfinished.

Frontend preparation has a reproducible staging command in
`frontend-staging.md`. It applies target overlays, excludes business routes,
and can install dependencies and run TypeScript/Next checks in the stage.
The generated file manifest is build evidence, not Git history extraction.

New Evaluation requests use protocol 2.0; protocol-1.0 experiment artifacts are
read-only. Session business SQL ledger writers and SQL-specific handoff fields
are retired, while historical local evidence can still be read without tool
execution. Research and MCP Resource host integration remain unfinished.

The Skill evaluation hook does not report completion before persistence returns.
Cancellation stops an active evaluation stream, but cannot undo a save request
already sent for a completed, valid result. That network commit boundary is not
a transactional rollback guarantee. The generic `frontend/src/lib/evalApi.ts`
and `backend/api/eval_api.py` are retained base files, not missing overlays.


## Independent backend installation

```bash
python3 packages/puddingharness-extraction/scripts/stage_backend.py --output /private/tmp/new-harness-backend
uv sync --project /private/tmp/new-harness-backend --locked --no-editable
HARNESS_TEST_PYTHON=/private/tmp/new-harness-backend/.venv/bin/python \
  PYTHONPATH=backend backend/.venv/bin/python -m pytest -q \
  packages/puddingharness-extraction/test/test_runtime_integration.py
```

The test driver starts installed Python outside the checkout, removes PYTHONPATH,
and does not install the development target loader. Current evidence is in
`artifacts/repository-split/harness-backend-install-round1.json`: 519 installed
files match the effective target; seven installed-runtime cases pass with an
offline scripted model. The backend has its own `uv.lock`. Staging checks source
and output boundaries, carries generic Skill resources, and records packaging
hashes. It never makes a complete release claim from a clean static audit.

The reviewed target now contains 66 overlays. Static round11 retains four
explicit findings: two read-only legacy selector literals and two dynamic imports
from fixed registries. The former sandbox probe strings were local test-file
markers, not environment variables or external image wire markers; their complete
target literals now use the Harness name. No string concatenation hides findings.

CLI API startup is validated with `PUDDINGHARNESS_CLI_INSTALL_POLICY=never`.
The optional `cli_runtime` still detects the old Worker CLI; a complete independent
CLI/desktop/deployment release remains an explicit next step.

### Independent source parity

The independent Harness repository stages Python and owned runtime resources
from `backend/`. Historical extraction overlays are retained for review but are
not consulted by the backend packager or the default source audit. Missing
required sources fail packaging; the installed source and staged byte digests
are checked before publishing a manifest. This is a packaging consistency gate,
not a replacement for static domain audit or installation acceptance.

Installed behavior can be checked from outside the checkout, using the staged
noneditable environment and `scripts/acceptance/installed_source_parity.py`.
The check verifies `site-packages` imports, retired binding rejection without
rewriting historical configuration, and bounded MCP text blob decoding.
