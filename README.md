# PuddingHarness

Independent development checkout extracted from PuddingClaw history.

Backend: `cd backend && uv sync --locked && uv run puddingharness-api --help`.
Frontend: `cd frontend && npm ci && npm run build`.

The backend and frontend contain the reviewed target cleanup. Electron and deployment assets retain historical implementation and still require boundary cleanup. Migration tooling under packages/puddingharness-extraction records the source cleanup provenance; its source-checkout staging assumptions are not the target build entrypoint.

This is not a production release. See docs/knowledge-platform/repository-separation-workplan.md for incomplete contracts.
