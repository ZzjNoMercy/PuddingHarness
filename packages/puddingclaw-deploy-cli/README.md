# Pudding Harness Deploy CLI

This package is the standalone deployment and local runtime client for the
Agent Harness. It owns an isolated `~/.puddingharness` Home (or the absolute
`PUDDINGHARNESS_HOME` supplied by the caller) and communicates with the local
Harness Backend over its documented agent protocol.

The package identity is `@puddingai/puddingharness` and its only executable is
`puddingharness`. It can be installed beside the legacy PuddingClaw package
without taking ownership of the `puddingclaw` command. The historical source
folder name is retained for existing repository build scripts.

The package remains private while repository separation and release gates are
incomplete. Local tarball packing and installation are supported; registry
publication and an authoritative remote repository URL are not configured.

```bash
puddingharness init --profile harness --non-interactive --yes
puddingharness status --json
puddingharness agent run "inspect the workspace" --json
puddingharness agent respond <run_id> --input-json - --json
puddingharness agent cancel <run_id> --json
puddingharness start --port auto --json
puddingharness stop --json
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

For a local release candidate, commit the source first and use Python 3.11 or
3.12 for the staging/wheel verifier:

```bash
PUDDINGHARNESS_SOURCE_ROOT=/absolute/path/to/harness \
PUDDINGHARNESS_BUILD_PYTHON=/absolute/path/to/python3.12 \
npm run build:runtime
npm run verify:candidate
npm pack
```

A candidate build records the full clean source commit, checks staged Python
and frontend source against that commit, and checks every wheel Python file
and its package metadata against the independent backend stage. The Python
package version and npm release version are separate identities. The current
CLI requires `home_freeze=1`; older runtime bundles must be rebuilt instead of
being relabeled. `--skip-build` remains an explicitly unverified developer
artifact and cannot pass candidate or publication checks.

`verify:candidate` validates artifact structure and build evidence while the
package remains private. It does not certify installation migration, runtime
health, signatures, production activation or publication. A release candidate
still needs actual temporary-Home installation and lifecycle validation.
Registry publication remains blocked by the normal `verify:publish` gate.
