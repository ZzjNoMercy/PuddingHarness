# Phase 0C boundary register

## First-principles invariant

The only facts that need to cross the future process/repository boundary are
caller identity, opaque correlation, knowledge resource identity, evidence,
query results, errors, and short-lived database query plans. Everything else
is an implementation detail of the Platform or Harness. The first executable
version of that boundary lives in [`backend/knowledge_contracts`](../../backend/knowledge_contracts).

`knowledge_contracts` is standard-library-only. It must not import FastAPI,
Pydantic, SQLAlchemy, MCP SDKs, LangChain, LangGraph, `graph`, `harness`,
`tools`, `knowledge`, `analytics`, or `vanna`.

## Required dependency direction

```text
future interfaces (REST / MCP / CLI / Workspace) -> application -> domain
future infrastructure ---------------------------> domain/application ports

domain/application -X-> FastAPI / MCP SDK / LangChain / DeepAgents / Claw graph
```

The current PuddingClaw modules remain legacy and are intentionally not
rewired in Phase 0. The guard is therefore forward-looking: it fails if the
new contract package starts importing a runtime or if a future
`backend/knowledge_platform` package imports a forbidden legacy layer.

## Phase gates

| Gate | Evidence | Status |
| --- | --- | --- |
| 0A inventory | `phase-0a-inventory.yaml` and credential ownership register | implemented as baseline draft; skill internals still require per-file review |
| 0C contracts | `backend/knowledge_contracts` plus contract tests | implemented |
| 0C Queue Lease port | `knowledge_contracts.lease`, `knowledge_platform.jobs.LeaseStore`, deterministic reference adapter | implemented as provider-neutral reference; legacy SQL worker adapter not rewired |
| 0C Wiki Compiler port | `knowledge_platform.wiki` application worker and five ports | implemented as provider-neutral reference with source identity fencing; legacy compiler/MCP/graph runtime not rewired |
| 0C Deep Research delegation | `knowledge_contracts.delegation`, `knowledge_platform.research.DynamicCapabilityPlanner` | implemented as dynamic authorized read-capability reference; legacy Deep Research and Intent Router not rewired |
| 0C Citation/Blob/Trace ports | `knowledge_contracts.artifacts`, `knowledge_platform.evidence` | implemented as provider-neutral Evidence, bounded stable-URI Blob read, and digest-only TraceSink ports; graph adapters not rewired |
| 0C DeepAgents wiring surface | `knowledge_platform.agent`, `docs/knowledge-platform/deepagents-wiring-boundary.md` | implemented as explicit capability surface and seven-point stop-flow register; legacy manager/mounts not rewired |
| 0C protocol v2 | `knowledge_contracts.protocol` | implemented as legacy-read/v2-emit boundary that removes `analytics_model_id`; legacy protocol runtime not switched |
| 0C static boundary | `test_knowledge_platform_boundaries.py` | implemented |
| 0B Catalog split | independent Bases, migration rehearsal, copy/verify/rollback | slice implementation in progress; production cutover not started |
| Phase 1 shadow | isolated Catalog application service and read-only shadow runner | implemented locally; not activated |

No Phase 0 artifact changes production registration, routing, database
connections, or legacy implementation behavior.

`phase_0c` readiness is intentionally scoped to provider-neutral reference
artifacts and their executable contract/boundary checks. It does not claim
that legacy adapters are rewired, that production data has been copied, or
that Phase 1 shadow traffic is safe; those claims require the separate 0B and
Phase 1 gates.
