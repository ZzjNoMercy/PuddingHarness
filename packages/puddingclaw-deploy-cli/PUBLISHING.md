# Pudding Harness package publishing

The npm package keeps the existing `@puddingai/puddingclaw` name and
`puddingclaw` bin as a compatibility entry point. Runtime identity and Home
are Harness-specific. Publishing is a separate release decision.

Build from an explicit Harness source checkout:

```bash
PUDDINGHARNESS_SOURCE_ROOT=/absolute/path/to/harness npm run build:runtime
npm test
npm run verify:publish
npm pack --dry-run
```

The runtime builder does not infer a sibling or parent checkout. It ships one
Harness requirements profile, verifies checksums, removes environment files,
rejects symlinks and credential material, and records only package-relative
runtime paths.
