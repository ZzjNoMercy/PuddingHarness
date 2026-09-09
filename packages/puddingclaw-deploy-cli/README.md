# Pudding Harness Deploy CLI

This package is the standalone deployment and local runtime client for the
Agent Harness. It owns an isolated `~/.puddingharness` Home (or the absolute
`PUDDINGHARNESS_HOME` supplied by the caller) and communicates with the local
Harness Backend over its documented agent protocol.

The existing `puddingclaw` npm/bin name remains an installation compatibility
entry point for this package. It does not select a legacy Home or enable
product services.

```bash
puddingclaw init --profile harness --non-interactive --yes
puddingclaw status --json
puddingclaw agent run "inspect the workspace" --json
puddingclaw agent respond <run_id> --input-json - --json
puddingclaw agent cancel <run_id> --json
puddingclaw start --port auto --json
puddingclaw stop --json
```

Supported lifecycle commands are `init`, `config`, `profile inspect|apply
harness`, `database`, `agent`, `runtime`, `logs`, `start`, `stop`, `restart`,
`open`, `status`, `doctor`, and `version`. Initialization includes the generic
Provider, optional multimodal Provider, core SQLite/PostgreSQL catalog, Python
runtime, and verified embedded bundle. No product-specific service lifecycle
is represented in the deployment config or runtime manifest.

Runtime bundles must be built with an explicit source checkout:

```bash
PUDDINGHARNESS_SOURCE_ROOT=/absolute/path/to/harness npm run build:runtime
```

The builder never derives a source checkout by walking above this package.
