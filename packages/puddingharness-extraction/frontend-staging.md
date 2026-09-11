# Independent frontend source authority

The frontend packager now copies the independent repository's frontend source.
Historical overlays cannot supply missing files or replace current source. The
manifest records `runtimeAuthority=independent_repository_source`, empty overlay
lists and `releaseable=false`. Source and staged bytes are checked after install,
typecheck and build. These digest checks do not atomically fence source writers.

`SourcesPanel.tsx` is a generic citation component owned by Harness and is copied
from source. Removed business routes remain excluded. Dependency cache reuse is
developer-only; release evidence requires an actual dependency installation and
build. Existing extraction-specific descriptions below are historical context.

## Historical extraction workflow

# Effective frontend staging

`scripts/stage_frontend.mjs` creates a reviewable effective Harness frontend
tree from a frontend checkout plus the target-only overlays in this package.
The staged tree contains copied TypeScript and Next configuration, so its
`tsconfig.json` and build do not resolve modules through the original checkout.

The base copy excludes these business paths:

- `src/app/analytics/`
- `src/app/knowledge/`
- `src/components/knowledge/`
- `src/app/api/chat/route.ts` (the retired legacy chat proxy)
- `src/components/citations/SourcesPanel.tsx` (replaced by the generic overlay)
- `src/components/settings/DocumentParserSettings.tsx`

The declared overlays are copied after the base selection and therefore own
their target paths. The script also excludes `node_modules`, `.git`, `.npmrc`,
all `.env*` files, Next build/cache directories, TypeScript build-info, logs,
and common certificate/key file suffixes. Registry configuration must be
provided through the caller's environment or npm configuration outside the
stage. The manifest also contains source snapshots for every selected base
file and overlay, and the script verifies those hashes after copying and after
the requested checks. The exact rules, overlay hashes, selected file hashes,
and check flags are written to
`harness-frontend-artifact-manifest.json` inside the stage.

The output directory must be absent or empty. The script refuses to merge into
an existing non-empty directory, so a rerun uses a newly-created directory
after the previous stage has been reviewed or removed by the caller. A source
root, overlay root, or output root that is itself a symlink is rejected. Parent
directory symlinks are resolved before containment checks, so normal macOS
paths such as `/tmp` remain usable without allowing output inside the source or
overlay tree.

From a checkout containing this package and a frontend source tree:

```bash
node packages/puddingharness-extraction/scripts/stage_frontend.mjs \
  --source-frontend /path/to/frontend \
  --output /private/tmp/puddingharness-frontend-stage \
  --reuse-node-modules /path/to/frontend/node_modules \
  --typecheck --build
```

`--reuse-node-modules` creates a developer-only symlink and is recorded as such
in the manifest; it is not a portable installation. For an independent install,
omit that option and install from the copied lockfile inside the empty stage:

```bash
node packages/puddingharness-extraction/scripts/stage_frontend.mjs \
  --source-frontend /path/to/frontend \
  --output /private/tmp/puddingharness-frontend-stage \
  --install --typecheck --build
```

`--install` runs `npm ci` in the stage and cannot be combined with dependency
reuse. `--typecheck` runs the copied stage's TypeScript compiler, and `--build`
runs the copied `npm run build` with telemetry disabled and one Next build
worker. A requested check is recorded as `"passed"` only after its process
exits successfully; a failed check produces no manifest. Build output is
generated in the stage's cache directory and is not listed as a distributable
source artifact.

This remains a preparation/staging tool until the extracted repository owns a
complete frontend base tree and its dependency policy. It does not modify the
source checkout.

The staged build proves the copied frontend compiles and generates its pages;
it does not prove a live external MCP resource adapter. `SourcesPanel` keeps
generic source/citation display and opens HTTP or file resources when a
corresponding URL is present. Arbitrary MCP resource URI resolution still
requires a host connection/read-resource adapter and is deliberately not
claimed by this staging check.
