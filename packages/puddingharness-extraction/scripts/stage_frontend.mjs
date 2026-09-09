#!/usr/bin/env node

/**
 * Build a self-contained effective frontend tree from a frontend checkout and
 * the target-only Harness overlays. The generated tree never imports files by
 * path from the source checkout; all source files needed by tsc/Next are
 * copied into the stage.
 */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import process from "node:process";
import { fileURLToPath } from "node:url";

const packageRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const overlayRoot = path.join(packageRoot, "overlays", "frontend");
const manifestName = "harness-frontend-artifact-manifest.json";

const overlayFiles = [
  "src/app/settings/page.tsx",
  "src/components/chat/ChatInput.tsx",
  "src/components/chat/ChatMessage.tsx",
  "src/components/citations/SourcesPanel.tsx",
  "src/components/extensions/McpCatalog.tsx",
  "src/components/layout/Navbar.tsx",
  "src/components/layout/Sidebar.tsx",
  "src/hooks/useEvalStream.ts",
  "src/lib/api.ts",
  "src/lib/store.tsx",
  "src/lib/useRuntimeProfile.ts",
];

const businessExclusions = [
  { type: "directory", path: "src/app/analytics" },
  { type: "directory", path: "src/app/knowledge" },
  { type: "directory", path: "src/components/knowledge" },
  { type: "file", path: "src/app/api/chat/route.ts" },
  { type: "file", path: "src/components/citations/SourcesPanel.tsx" },
  { type: "file", path: "src/components/settings/DocumentParserSettings.tsx" },
];

const cacheAndSecretRules = [
  "node_modules/",
  ".next/",
  ".next-*/",
  ".turbo/",
  ".cache/",
  ".git/",
  "coverage/",
  "dist/",
  "build/",
  ".env* (all environment files, including examples)",
  ".npmrc (registry credentials/config)",
  "*.tsbuildinfo",
  "*.log",
  "*.pem",
  "*.key",
  "*.p12",
  "*.pfx",
  "*.mobileprovision",
  "*.provisionprofile",
  ".DS_Store",
];

function usage(message) {
  if (message) console.error(`error: ${message}\n`);
  console.error(`Usage:
  node packages/puddingharness-extraction/scripts/stage_frontend.mjs \\
    --source-frontend /path/to/frontend \\
    --output /path/to/empty/stage \\
    [--reuse-node-modules /path/to/frontend/node_modules] \\
    [--install] [--typecheck] [--build]

The output must be absent or empty. --install and --reuse-node-modules are
mutually exclusive. --reuse-node-modules is developer-only and is recorded in
the manifest; it is never a distributable dependency layout.`);
  process.exit(2);
}

function parseArgs(argv) {
  const args = { install: false, typecheck: false, build: false };
  for (let index = 0; index < argv.length; index += 1) {
    const item = argv[index];
    if (item === "--source-frontend") args.sourceFrontend = argv[++index];
    else if (item === "--output") args.output = argv[++index];
    else if (item === "--reuse-node-modules") args.reuseNodeModules = argv[++index];
    else if (item === "--install") args.install = true;
    else if (item === "--typecheck") args.typecheck = true;
    else if (item === "--build") args.build = true;
    else if (item === "--help" || item === "-h") usage();
    else usage(`unknown option ${item}`);
  }
  if (!args.sourceFrontend) usage("--source-frontend is required");
  if (!args.output) usage("--output is required");
  if (args.install && args.reuseNodeModules) usage("--install cannot be combined with --reuse-node-modules");
  return args;
}

function normalizeRelative(value) {
  return value.split(path.sep).join("/");
}

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function realPathCandidate(candidate) {
  let existing = candidate;
  const suffix = [];
  while (!fs.existsSync(existing)) {
    suffix.unshift(path.basename(existing));
    const parent = path.dirname(existing);
    if (parent === existing) throw new Error(`cannot resolve parent for ${candidate}`);
    existing = parent;
  }
  return path.join(fs.realpathSync(existing), ...suffix);
}

function isExcluded(relativePath, includeTargetOverlays = true) {
  const normalized = normalizeRelative(relativePath);
  const name = path.posix.basename(normalized);
  if (normalized === manifestName || normalized.startsWith(`${manifestName}/`)) return true;
  if (name.startsWith(".env") || name === ".npmrc") return true;
  if (
    normalized.split("/").some((part) =>
      part === ".git" ||
      part === "node_modules" ||
      part === ".next" ||
      part.startsWith(".next-") ||
      [".turbo", ".cache", "coverage", "dist", "build"].includes(part)
    )
  ) return true;
  if (name === ".DS_Store" || name.endsWith(".tsbuildinfo") || name.endsWith(".log")) return true;
  if ([".pem", ".key", ".p12", ".pfx", ".mobileprovision", ".provisionprofile"].some((suffix) => name.endsWith(suffix))) return true;
  const isTargetOverlayPath = overlayFiles.includes(normalized);
  const containsTargetOverlay = overlayFiles.some((overlayPath) => overlayPath.startsWith(`${normalized}/`));
  const allowBusinessReplacement = includeTargetOverlays && (isTargetOverlayPath || containsTargetOverlay);
  if (!allowBusinessReplacement && businessExclusions.some((rule) => rule.type === "directory" && (normalized === rule.path || normalized.startsWith(`${rule.path}/`)))) return true;
  if (!allowBusinessReplacement && businessExclusions.some((rule) => rule.type === "file" && normalized === rule.path)) return true;
  if (!includeTargetOverlays && overlayFiles.includes(normalized)) return true;
  return false;
}

function copyBaseTree(source, output) {
  const selected = [];
  const visit = (sourcePath, relativePath) => {
    const normalized = normalizeRelative(relativePath);
    const stat = fs.lstatSync(sourcePath);
    if (stat.isSymbolicLink()) {
      throw new Error(`source frontend contains an unsupported symlink: ${normalized}`);
    }
    if (normalized && isExcluded(normalized, false)) return;
    const destination = path.join(output, relativePath);
    if (stat.isDirectory()) {
      fs.mkdirSync(destination, { recursive: true });
      for (const entry of fs.readdirSync(sourcePath).sort()) {
        visit(path.join(sourcePath, entry), path.join(relativePath, entry));
      }
      return;
    }
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    const sourceHash = sha256(sourcePath);
    fs.copyFileSync(sourcePath, destination);
    selected.push({ path: normalized, sha256: sourceHash, bytes: stat.size });
  };
  for (const entry of fs.readdirSync(source).sort()) {
    visit(path.join(source, entry), entry);
  }
  return selected;
}

function copyOverlays(output) {
  const selected = [];
  for (const relativePath of overlayFiles) {
    const source = path.join(overlayRoot, relativePath);
    if (!fs.existsSync(source)) throw new Error(`declared overlay is missing: ${relativePath}`);
    const sourceStat = fs.lstatSync(source);
    if (!sourceStat.isFile()) throw new Error(`declared overlay is not a regular file: ${relativePath}`);
    const destination = path.join(output, relativePath);
    if (!isWithin(output, destination)) throw new Error(`overlay escapes output: ${relativePath}`);
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.copyFileSync(source, destination);
    selected.push({ path: relativePath, sha256: sha256(source), bytes: sourceStat.size });
  }
  return selected;
}

function sha256(filePath) {
  return crypto.createHash("sha256").update(fs.readFileSync(filePath)).digest("hex");
}

function listArtifactFiles(output) {
  const files = [];
  const visit = (current, relative) => {
    if (relative && isExcluded(relative, true)) return;
    const stat = fs.lstatSync(current);
    if (stat.isDirectory()) {
      for (const entry of fs.readdirSync(current).sort()) visit(path.join(current, entry), path.join(relative, entry));
      return;
    }
    if (!stat.isFile()) throw new Error(`stage contains unsupported entry: ${relative}`);
    files.push({ path: normalizeRelative(relative), sha256: sha256(current), bytes: stat.size });
  };
  for (const entry of fs.readdirSync(output).sort()) visit(path.join(output, entry), entry);
  return files;
}

function verifySnapshot(root, snapshot, label) {
  for (const expected of snapshot) {
    const filePath = path.join(root, expected.path);
    if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
      throw new Error(`${label} file disappeared during staging: ${expected.path}`);
    }
    const actual = { sha256: sha256(filePath), bytes: fs.statSync(filePath).size };
    if (actual.sha256 !== expected.sha256 || actual.bytes !== expected.bytes) {
      throw new Error(`${label} changed during staging: ${expected.path}`);
    }
  }
}

function run(command, cwd, env = {}) {
  console.log(`$ ${command.join(" ")}`);
  const result = spawnSync(command[0], command.slice(1), {
    cwd,
    env: { ...process.env, ...env },
    stdio: "inherit",
  });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command.join(" ")} exited with ${result.status}`);
}

const args = parseArgs(process.argv.slice(2));
const sourceFrontend = path.resolve(args.sourceFrontend);
const output = path.resolve(args.output);
if (!fs.existsSync(sourceFrontend) || !fs.lstatSync(sourceFrontend).isDirectory()) usage(`source frontend is not a directory: ${sourceFrontend}`);
if (fs.lstatSync(sourceFrontend).isSymbolicLink()) usage("source frontend root must not be a symlink");
if (!fs.existsSync(overlayRoot) || !fs.lstatSync(overlayRoot).isDirectory()) usage(`overlay root is not a directory: ${overlayRoot}`);
if (fs.lstatSync(overlayRoot).isSymbolicLink()) usage("overlay root must not be a symlink");
if (fs.existsSync(output) && fs.lstatSync(output).isSymbolicLink()) usage("output root must not be a symlink");
const sourceReal = fs.realpathSync(sourceFrontend);
const overlayReal = fs.realpathSync(overlayRoot);
const outputCandidate = realPathCandidate(output);
if (isWithin(sourceReal, outputCandidate) || isWithin(outputCandidate, sourceReal)) usage("output must not be inside or contain the source frontend");
if (isWithin(overlayReal, outputCandidate) || isWithin(outputCandidate, overlayReal)) usage("output must not be inside or contain the overlay tree");
if (fs.existsSync(output)) {
  if (!fs.statSync(output).isDirectory()) usage(`output exists and is not a directory: ${output}`);
  if (fs.readdirSync(output).length > 0) usage(`output must be absent or empty: ${output}`);
} else {
  fs.mkdirSync(output, { recursive: true });
}
const outputReal = fs.realpathSync(output);
if (isWithin(sourceReal, outputReal) || isWithin(outputReal, sourceReal)) usage("output must not be inside or contain the source frontend");
if (isWithin(overlayReal, outputReal) || isWithin(outputReal, overlayReal)) usage("output must not be inside or contain the overlay tree");

const selectedBaseFiles = copyBaseTree(sourceFrontend, output);
const selectedOverlayFiles = copyOverlays(output);
verifySnapshot(sourceFrontend, selectedBaseFiles, "source frontend");
verifySnapshot(overlayRoot, selectedOverlayFiles, "overlay");

let nodeModulesMode = { present: false, developerOnly: false };
if (args.reuseNodeModules) {
  const modules = path.resolve(args.reuseNodeModules);
  if (!fs.existsSync(modules) || !fs.statSync(modules).isDirectory()) throw new Error(`node_modules directory not found: ${modules}`);
  const destination = path.join(output, "node_modules");
  // Resolve both sides before calculating the link. On macOS /tmp and /Users
  // can be symlinked through /private, making a lexical relative path invalid.
  const outputReal = fs.realpathSync(output);
  const modulesReal = fs.realpathSync(modules);
  fs.symlinkSync(path.relative(outputReal, modulesReal), destination, "dir");
  nodeModulesMode = { present: true, developerOnly: true, mode: "symlink", source: modulesReal };
}

if (args.install) {
  run(["npm", "ci"], output);
  nodeModulesMode = { present: true, developerOnly: false, mode: "installed" };
}
if (args.typecheck) run(["./node_modules/.bin/tsc", "--noEmit", "--pretty", "false", "--project", "tsconfig.json"], output);
if (args.build) run(["npm", "run", "build"], output, { NEXT_TELEMETRY_DISABLED: "1", NEXT_PRIVATE_BUILD_WORKER: "1" });
verifySnapshot(sourceFrontend, selectedBaseFiles, "source frontend");
verifySnapshot(overlayRoot, selectedOverlayFiles, "overlay");

const artifactFiles = listArtifactFiles(output);
const artifactByPath = new Map(artifactFiles.map((file) => [file.path, file]));
const overlayArtifacts = overlayFiles.map((relativePath) => {
  const artifact = artifactByPath.get(relativePath);
  if (!artifact) throw new Error(`overlay missing from artifact manifest: ${relativePath}`);
  return artifact;
});

const manifest = {
  schemaVersion: 1,
  kind: "puddingharness-effective-frontend",
  generatedBy: "packages/puddingharness-extraction/scripts/stage_frontend.mjs",
  sourceFrontend: { requested: sourceFrontend, resolved: sourceReal },
  overlayRoot: { requested: path.relative(process.cwd(), overlayRoot), resolved: overlayReal },
  output: { requested: output, resolved: outputReal },
  selection: {
    baseCopy: "recursive frontend tree with sorted traversal",
    businessExclusions,
    overlayFiles,
    cacheAndSecretRules,
    selectedBaseFileCount: selectedBaseFiles.length,
    selectedOverlayFileCount: selectedOverlayFiles.length,
  },
  sourceSnapshot: { base: selectedBaseFiles, overlays: selectedOverlayFiles },
  nodeModules: nodeModulesMode,
  checks: { typecheck: args.typecheck ? "passed" : "not-run", build: args.build ? "passed" : "not-run" },
  overlayArtifacts,
  files: artifactFiles,
};
fs.writeFileSync(path.join(output, manifestName), `${JSON.stringify(manifest, null, 2)}\n`);
console.log(`staged ${manifest.files.length} files at ${output}`);
console.log(`manifest: ${path.join(output, manifestName)}`);
