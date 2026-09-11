import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const scriptPath = path.join(repositoryRoot, "packages/puddingharness-extraction/scripts/stage_frontend.mjs");
const overlayRoot = path.join(repositoryRoot, "packages/puddingharness-extraction/overlays/frontend");
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), "puddingharness-staging-test-"));
const source = path.join(tempRoot, "frontend");
const stage = path.join(tempRoot, "stage");

function write(relativePath, content = "fixture") {
  const filePath = path.join(source, relativePath);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, content);
}

for (const name of ["src/components/citations/SourcesPanel.tsx", "src/components/extensions/McpCatalog.tsx", "src/lib/api.ts"]) {
  write(name, "independent source authority\n");
}
write("package.json", "{}\n");
write("package-lock.json", "{}\n");
write(".env.example", "PUBLIC_EXAMPLE=must-not-copy\n");
write(".env", "SECRET=must-not-copy\n");
write(".npmrc", "//registry.example/:_authToken=must-not-copy\n");
write(".git/config", "[remote]\nurl=secret\n");
write("src/app/page.tsx", "export default function Page() { return null; }\n");
write("src/app/analytics/page.tsx");
write("src/app/knowledge/page.tsx");
write("src/components/knowledge/Legacy.tsx");
write("src/app/api/chat/route.ts");
write("src/components/settings/DocumentParserSettings.tsx");
write("src/.cache/ignored.txt");
write("src/ignored.tsbuildinfo");
write("secret.pem", "private key material");
for (const cacheName of [".next-dev-3001", ".next-dev-4321", ".next-dev-4322", ".next-9876"]) {
  write(`${cacheName}/cache/BUILD_ID`, "must-not-copy");
}

execFileSync(process.execPath, [scriptPath, "--source-frontend", source, "--output", stage], { stdio: "pipe" });

for (const relativePath of [
  "src/app/analytics",
  "src/app/knowledge",
  "src/components/knowledge",
  "src/app/api/chat/route.ts",
  "src/components/settings/DocumentParserSettings.tsx",
  ".env",
  ".env.example",
  ".npmrc",
  ".git",
  "src/.cache",
  "src/ignored.tsbuildinfo",
  "secret.pem",
  ".next-dev-3001",
  ".next-dev-4321",
  ".next-dev-4322",
  ".next-9876",
]) {
  assert.equal(fs.existsSync(path.join(stage, relativePath)), false, `excluded path copied: ${relativePath}`);
}
for (const relativePath of [
  "src/components/citations/SourcesPanel.tsx",
  "src/components/extensions/McpCatalog.tsx",
  "src/lib/api.ts",
]) {
  assert.equal(fs.existsSync(path.join(stage, relativePath)), true, `source missing: ${relativePath}`);
  assert.deepEqual(
    fs.readFileSync(path.join(stage, relativePath)),
    fs.readFileSync(path.join(source, relativePath)),
    `stage must use independent source for ${relativePath}`,
  );
}
const manifest = JSON.parse(fs.readFileSync(path.join(stage, "harness-frontend-artifact-manifest.json"), "utf8"));
assert.equal(manifest.kind, "puddingharness-effective-frontend");
assert.equal(manifest.runtimeAuthority, "independent_repository_source");
assert.equal(manifest.releaseable, false);
assert.equal(manifest.selection.selectedOverlayFileCount, 0);
assert.equal(manifest.nodeModules.present, false);
assert.equal(manifest.checks.typecheck, "not-run");
assert.equal(manifest.checks.build, "not-run");
assert.equal(manifest.selection.selectedOverlayFileCount, manifest.selection.overlayFiles.length);
assert.equal(manifest.overlayArtifacts.length, manifest.selection.overlayFiles.length);
assert.equal(
  manifest.overlayArtifacts.every((item) => typeof item.sha256 === "string" && item.sha256.length === 64),
  true,
  "manifest must hash every delivered overlay",
);
assert.equal(manifest.sourceSnapshot.base.length, manifest.selection.selectedBaseFileCount);
assert.equal(manifest.sourceSnapshot.overlays.length, manifest.selection.selectedOverlayFileCount);
assert.equal(
  manifest.files.some((item) => item.path === "harness-frontend-artifact-manifest.json"),
  false,
  "manifest must not recursively hash itself",
);
assert.equal(
  manifest.files.some((item) => item.path.startsWith(".env") || item.path === ".npmrc" || item.path === ".git" || item.path.startsWith(".git/")),
  false,
  "artifact manifest must not include secret or checkout metadata files",
);
assert.deepEqual(manifest.selection.businessExclusions.map((item) => item.path), [
  "src/app/analytics",
  "src/app/knowledge",
  "src/components/knowledge",
  "src/app/api/chat/route.ts",
  "src/components/settings/DocumentParserSettings.tsx",
]);

const nonEmptyOutput = path.join(tempRoot, "non-empty");
fs.mkdirSync(nonEmptyOutput);
fs.writeFileSync(path.join(nonEmptyOutput, "keep.txt"), "keep");
assert.throws(
  () => execFileSync(process.execPath, [scriptPath, "--source-frontend", source, "--output", nonEmptyOutput], { stdio: "pipe" }),
  /status 2|output must be absent or empty/,
  "staging must refuse to overwrite a non-empty target",
);

const symlinkSource = path.join(tempRoot, "symlink-source");
fs.mkdirSync(symlinkSource);
fs.symlinkSync(source, path.join(symlinkSource, "frontend-link"), "dir");
assert.throws(
  () => execFileSync(process.execPath, [scriptPath, "--source-frontend", path.join(symlinkSource, "frontend-link"), "--output", path.join(tempRoot, "symlink-stage")], { stdio: "pipe" }),
  /symlink|not a directory/,
  "staging must reject a symlink source root",
);

assert.throws(
  () => execFileSync(process.execPath, [scriptPath, "--source-frontend", source, "--output", path.join(source, "nested-stage")], { stdio: "pipe" }),
  /inside or contain the source frontend/,
  "staging must reject an output nested inside the source",
);

const outputTarget = path.join(tempRoot, "output-target");
const outputLink = path.join(tempRoot, "output-link");
fs.mkdirSync(outputTarget);
fs.symlinkSync(outputTarget, outputLink, "dir");
assert.throws(
  () => execFileSync(process.execPath, [scriptPath, "--source-frontend", source, "--output", outputLink], { stdio: "pipe" }),
  /output root must not be a symlink/,
  "staging must reject a symlink output root",
);

assert.throws(
  () => execFileSync(process.execPath, [scriptPath, "--source-frontend", source, "--output", path.join(overlayRoot, ".staging-safety-test")], { stdio: "pipe" }),
  /inside or contain the packaging tree/,
  "staging must reject an output nested inside the overlay tree",
);

// Execute a relocated packager with no historical overlay tree, then a poisoned one.
const isolatedPackage = path.join(tempRoot, "packaging");
const isolatedScript = path.join(isolatedPackage, "scripts/stage_frontend.mjs");
fs.mkdirSync(path.dirname(isolatedScript), { recursive: true });
fs.copyFileSync(scriptPath, isolatedScript);
for (const state of ["absent", "poisoned"]) {
  if (state === "poisoned") {
    const poison = path.join(isolatedPackage, "overlays/frontend/src/lib/api.ts");
    fs.mkdirSync(path.dirname(poison), { recursive: true });
    fs.writeFileSync(poison, "export const legacyAnalytics = true;\n");
  }
  const destination = path.join(tempRoot, `isolated-${state}`);
  execFileSync(process.execPath, [isolatedScript, "--source-frontend", source, "--output", destination]);
  assert.deepEqual(fs.readFileSync(path.join(destination, "src/lib/api.ts")), fs.readFileSync(path.join(source, "src/lib/api.ts")));
  assert.equal(fs.existsSync(path.join(destination, "src/app/settings/page.tsx")), false,
    "missing source must not be supplied by historical overlay");
}
console.log("frontend staging selection and safety: passed");
