# DeepAgents wiring boundary (Phase 0C)

This register describes the first-principles stop-flow for the current
`deepagents_manager.py` wiring center. It is a design and verification
artifact; it does not change the legacy runtime in Phase 0.

## Invariant

The Harness may establish caller `Principal` and opaque `Correlation`. It may
discover and expose authorized capability descriptors. It must not derive
Platform authorization or business arguments from a tool name, a session
object, a virtual filesystem mount, or a chat message. Platform application
services receive explicit resource URIs and explicit arguments only.

## Seven-point elimination register

| Current wiring point | Target behavior | Stop-flow evidence before removal |
| --- | --- | --- |
| Tool-name injection | capability discovery returns descriptors; adapter translates protocol details | descriptor inventory test and no hard-coded Platform tool names |
| Toolset assembly | expose only discovered, caller-authorized capabilities | arbitrary-provider and unauthorized-capability tests |
| Intent Router | no Knowledge/Analytics keyword registry in pure Harness | capability description/delegation contract test |
| Prompt instructions | no `/knowledge`, `/semantic-assets`, `/sql-guardrails`, `/analytics-models` business mounts | prompt snapshot and route-equivalence review |
| Middleware | no implicit session/query/attachment injection | `AgentCapabilitySurface` rejects reserved fields |
| Virtual mounts | replace with explicit `knowledge://` Resource/Blob reads | Blob URI/range contract and reachability rehearsal |
| Interrupt resolver | resolve generic job/correlation state, not Knowledge job classes | job contract and terminal-state replay tests |

## Virtual mount stop order

The order is dependency-driven and must not be shortened:

1. `/knowledge` → Platform Catalog/Query/Blob Resource capabilities.
2. `/semantic-assets` → Platform Semantic Asset capabilities.
3. `/sql-guardrails` → Platform Database Query/Guardrail capabilities.
4. `/analytics-models` → Platform Semantic/Collection provider capabilities.

For each mount, A1 extraction and equivalence tests precede shadow traffic;
shadow traffic precedes a capability-specific stop switch. The legacy mount
remains available for rollback until its capability rollback window closes.

## Current implementation state

`backend/knowledge_platform/agent/` provides the provider-neutral surface and
explicit context fields. It is not connected to `backend/graph/` or any
legacy Tool registry. The static boundary test covers the complete
`knowledge_platform` package and rejects imports from graph, harness, tools,
knowledge, analytics, and vanna.
