import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const overlayPath = path.join(
  repositoryRoot,
  "packages/puddingharness-extraction/overlays/frontend/src/lib/api.ts",
);
const source = fs.readFileSync(overlayPath, "utf8");
const requireFromFrontend = createRequire(path.join(repositoryRoot, "frontend/package.json"));
const ts = requireFromFrontend("typescript");
const sourceFile = ts.createSourceFile(
  overlayPath,
  source,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TS,
);

assert.equal(sourceFile.parseDiagnostics.length, 0, "Harness API overlay must parse as TypeScript");

function exportedNames(file) {
  const names = new Set();
  for (const statement of file.statements) {
    if (!statement.modifiers?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)) {
      continue;
    }
    if (statement.name) names.add(statement.name.text);
    if (statement.declarationList) {
      for (const declaration of statement.declarationList.declarations) {
        names.add(declaration.name.getText(file));
      }
    }
  }
  return names;
}

const exports = exportedNames(sourceFile);
for (const name of [
  "streamAgent",
  "createSession",
  "listSessions",
  "getSessionHistory",
  "readFile",
  "saveFile",
  "listProjects",
  "getMcpConfig",
  "getMcpServersStatus",
  "getRunReviewStatus",
  "getSessionHarnessState",
  "uploadAgentAttachments",
]) {
  assert.ok(exports.has(name), "generic Harness API export was removed: " + name);
}

const forbiddenExport = /(?:Knowledge|Analytics|Semantic|SqlGuardrail|ReadLater|Vanna|LlmWiki|Gbrain|TableAsset|DatabaseQuery|TaskCenter|DocumentParser)/i;
for (const name of exports) {
  assert.equal(forbiddenExport.test(name), false, "platform-only API export leaked: " + name);
}

// Endpoint and request DTO checks are structural enough to catch a renamed
// business route or a reintroduced active analytics selector.
assert.equal(/\/(?:knowledge|analytics|imports|sources|schema|read[-_]later)(?:[/?]|$)/i.test(source), false);
assert.equal(/(?:semantic-assets|sql-guardrails|analytics-models)/i.test(source), false);
assert.equal(/analytics_model_id|analyticsModelId/i.test(source), false);
assert.match(source, /Historical read-only compatibility/);
assert.match(source, /getSessionHistory/);

function walkFiles(directory) {
  const result = [];
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) result.push(...walkFiles(file));
    else if (/\.(ts|tsx)$/.test(entry.name)) result.push(file);
  }
  return result;
}

function importedApiNames(filePath) {
  const fileSource = fs.readFileSync(effectivePath(filePath), "utf8");
  const file = ts.createSourceFile(
    filePath,
    fileSource,
    ts.ScriptTarget.Latest,
    true,
    filePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const names = [];
  for (const statement of file.statements) {
    if (!ts.isImportDeclaration(statement) || statement.moduleSpecifier.text !== "@/lib/api") continue;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const element of bindings.elements) {
      names.push({ name: element.propertyName?.text || element.name.text, filePath });
    }
  }
  return names;
}

const frontendRoot = path.join(repositoryRoot, "frontend/src");
function effectivePath(filePath) {
  const overlay = path.join(repositoryRoot, "packages/puddingharness-extraction/overlays", path.relative(repositoryRoot, filePath));
  return fs.existsSync(overlay) ? overlay : filePath;
}
const missing = walkFiles(frontendRoot)
  .flatMap(importedApiNames)
  .filter(({ name }) => !exports.has(name))
  .map(({ name, filePath }) => ({
    name,
    file: path.relative(repositoryRoot, filePath),
  }));

// A missing generic export is an API-boundary regression. Removed Platform
// symbols are tracked separately so this test does not mislabel every caller
// as an excluded page: several retained mixed components still need migration.
const removedPlatformApiName = /(?:Knowledge|Analytics|Semantic|SqlGuardrail|ReadLater|Vanna|LlmWiki|Gbrain|TableAsset|DatabaseQuery|DatabaseSql|TaskCenter|TaskNotification|TaskJob|DocumentParser|Dimension|LogicalDataset|AssetRelation|ConcatDataset|Brain|StagedKnowledge|RawKnowledge|searchKnowledge|KnowledgeFile|KnowledgeImport|TableEntity)/i;
const unexpectedMissing = missing.filter(({ name }) => !removedPlatformApiName.test(name));
assert.deepEqual(
  unexpectedMissing,
  [],
  "generic API imports became unresolved:\n" + JSON.stringify(unexpectedMissing, null, 2),
);

const excludedBusinessFile = /frontend\/src\/(?:app\/(?:analytics|knowledge)(?:\/|$)|components\/knowledge\/|components\/citations\/SourcesPanel\.tsx$|components\/settings\/DocumentParserSettings\.tsx$)/;
const retainedMixedFile = /frontend\/src\/(?:app\/settings\/page\.tsx$|components\/chat\/(?:ChatInput|ChatMessage)\.tsx$|components\/layout\/Navbar\.tsx$)/;
const excludedBusiness = missing.filter(({ file }) => excludedBusinessFile.test(file));
const retainedMixed = missing.filter(({ file }) => retainedMixedFile.test(file));
const unclassified = missing.filter(
  (item) => !excludedBusinessFile.test(item.file) && !retainedMixedFile.test(item.file),
);
assert.deepEqual(unclassified, [], "unclassified unresolved frontend callers:\n" + JSON.stringify(unclassified, null, 2));

console.log("API export boundary: passed");
console.log(
  "Static API check (full build verified separately); retained unresolved imports: " +
    retainedMixed.length,
);
console.log("  excluded business-page callers: " + excludedBusiness.length);
if (process.env.HARNESS_AUDIT_VERBOSE) {
  for (const item of excludedBusiness) console.log("    " + item.file + ": " + item.name);
}
console.log("  retained mixed callers: " + retainedMixed.length);
for (const item of retainedMixed) console.log("    " + item.file + ": " + item.name);

// One generic caller still has the retired selector in its positional stream
// call. Keep this as an explicit migration record rather than retaining a
// dead parameter in the extracted client.
const storePath = path.join(
  repositoryRoot,
  "packages/puddingharness-extraction/overlays/frontend/src/lib/store.tsx",
);
const storeSource = fs.readFileSync(storePath, "utf8");
const storeAnalyticsBreakpoint = /streamAgent\([\s\S]{0,900}runOptions\.analyticsModelId/.test(storeSource);
console.log(
  storeAnalyticsBreakpoint
    ? "  effective store overlay still passes runOptions.analyticsModelId"
    : "  effective store overlay analyticsModelId migration is closed",
);
