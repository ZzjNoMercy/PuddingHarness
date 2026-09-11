# PuddingHarness package publishing

The package identity is `@puddingai/puddingharness`; its only executable is
`puddingharness`. Do not add a `puddingclaw` compatibility bin: the legacy
installation must retain ownership of that command and its Home.

The current package is deliberately private. `verify:publish` must reject this
state, even when local package tests pass. A successful local pack/install is
not registry publication readiness. No registry ownership or remote repository
URL is established by the local package metadata.

Local identity and coexistence acceptance:

```bash
npm test
npm pack --ignore-scripts --dry-run
```

Release preparation requires the actual embedded runtime and all repository
separation, migration and release approvals. Build from an explicit source:

```bash
PUDDINGHARNESS_SOURCE_ROOT=/absolute/path/to/harness npm run build:runtime
npm test
npm run verify:publish
```

The final command intentionally remains blocked while private or while the
verified embedded runtime and hashed dependency locks are missing. Do not
remove that gate merely to make an identity test pass.

The runtime builder does not infer a sibling or parent checkout. It ships one
Harness requirements profile, verifies checksums, removes environment files,
rejects symlinks and credential material, and records only package-relative
runtime paths. The historical source folder name is not the npm/bin identity.
