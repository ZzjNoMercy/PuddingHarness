# PuddingHarness

Independent development checkout extracted from PuddingClaw history.

Backend: `cd backend && uv sync --locked --no-editable && uv run --no-sync puddingharness-api`.
Tests: from backend, run `uv run --no-sync pytest -q`. After Python edits rebuild with `uv sync --locked --no-editable --reinstall-package puddingharness-backend`; installed-runtime tests deliberately execute outside the checkout and reject editable imports.
Frontend: `cd frontend && npm ci && npm run build`.

The backend, frontend, Electron and deployment CLI contain target cleanup. Electron uses an independent application identity and the Harness Home. Set PUDDINGHARNESS_HOME to an absolute owned directory, and PUDDINGHARNESS_PORT for a custom API port. No model credentials are needed to start the API; Agent generation needs a configured provider.

The deployment CLI owns only the puddingharness executable; the legacy puddingclaw command and Home remain separate. Building its runtime requires explicit PUDDINGHARNESS_SOURCE_ROOT pointing to this repository. Migration tooling under packages/puddingharness-extraction records immutable source cleanup provenance; it is not the target build entrypoint. Tags under source-puddingclaw/ are source history, not target releases.

This is not a production release. See docs/knowledge-platform/repository-separation-workplan.md for incomplete contracts.

Audit the actual extracted backend with `python3 scripts/audit-target-backend.py --output /private/tmp/harness-target-audit.json`. Use a new output filename per run. The source overlay provenance remains immutable and is not reapplied to the already cleaned target.

The one current compatibility finding remains visible with explicit SHA-bound reviews in provenance/target-boundary-reviews.json. Source changes invalidate a review. Static review does not certify runtime, frontend, distribution or production readiness.
