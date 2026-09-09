import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const overlaySourceRoot = path.join(repositoryRoot, "packages/puddingharness-extraction/overlays/frontend/src");
const apiPath = path.join(overlaySourceRoot, "lib/api.ts");
const storePath = path.join(overlaySourceRoot, "lib/store.tsx");
const chatInputPath = path.join(overlaySourceRoot, "components/chat/ChatInput.tsx");
const chatMessagePath = path.join(overlaySourceRoot, "components/chat/ChatMessage.tsx");
const navbarPath = path.join(overlaySourceRoot, "components/layout/Navbar.tsx");
const sourcesPanelPath = path.join(overlaySourceRoot, "components/citations/SourcesPanel.tsx");
const mcpCatalogPath = path.join(overlaySourceRoot, "components/extensions/McpCatalog.tsx");
const settingsPath = path.join(overlaySourceRoot, "app/settings/page.tsx");
const evalHookPath = path.join(overlaySourceRoot, "hooks/useEvalStream.ts");
const requireFromFrontend = createRequire(path.join(repositoryRoot, "frontend/package.json"));
const ts = requireFromFrontend("typescript");

function parse(filePath) {
  const source = fs.readFileSync(filePath, "utf8");
  const file = ts.createSourceFile(
    filePath,
    source,
    ts.ScriptTarget.Latest,
    true,
    filePath.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  assert.equal(file.parseDiagnostics.length, 0, filePath + " must parse as TypeScript");
  return { file, source };
}

function walk(node, visit) {
  visit(node);
  node.forEachChild((child) => walk(child, visit));
}

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

function namedApiImports(file) {
  const names = [];
  for (const statement of file.statements) {
    if (!ts.isImportDeclaration(statement)) continue;
    if (statement.moduleSpecifier.text !== "@/lib/api") continue;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const element of bindings.elements) {
      names.push(element.propertyName?.text || element.name.text);
    }
  }
  return names;
}

const { file: apiFile } = parse(apiPath);
const { file: storeFile, source: storeSource } = parse(storePath);
const { file: chatInputFile, source: chatInputSource } = parse(chatInputPath);
const { file: chatMessageFile, source: chatMessageSource } = parse(chatMessagePath);
const { file: navbarFile, source: navbarSource } = parse(navbarPath);
const { file: sourcesPanelFile, source: sourcesPanelSource } = parse(sourcesPanelPath);
const { file: mcpCatalogFile, source: mcpCatalogSource } = parse(mcpCatalogPath);
const { file: settingsFile, source: settingsSource } = parse(settingsPath);
parse(evalHookPath);
const apiExports = exportedNames(apiFile);

for (const file of [storeFile, chatInputFile, chatMessageFile, navbarFile, sourcesPanelFile, mcpCatalogFile, settingsFile]) {
  for (const name of namedApiImports(file)) {
    assert.ok(apiExports.has(name), "overlay imports missing API export: " + name);
  }
}

let streamAgentDeclaration;
walk(apiFile, (node) => {
  if (
    ts.isFunctionDeclaration(node) &&
    node.name?.text === "streamAgent"
  ) {
    streamAgentDeclaration = node;
  }
});
assert.ok(streamAgentDeclaration, "target API must declare streamAgent");
const streamAgentParameters = streamAgentDeclaration.parameters.map((parameter) => parameter.name.getText(apiFile));

const streamAgentCalls = [];
walk(storeFile, (node) => {
  if (
    ts.isCallExpression(node) &&
    ts.isIdentifier(node.expression) &&
    node.expression.text === "streamAgent"
  ) {
    streamAgentCalls.push(node);
  }
});
assert.equal(streamAgentCalls.length, 1, "store overlay must have one ordinary streamAgent call");
const streamAgentCall = streamAgentCalls[0];
assert.equal(
  streamAgentCall.arguments.length,
  streamAgentParameters.length,
  "store streamAgent positional arity must match the target API",
);
const normalizedArguments = streamAgentCall.arguments.map((argument) =>
  argument.getText(storeFile).replace(/\s+/g, " ").trim(),
);
assert.deepEqual(
  normalizedArguments,
  [
    "processedText",
    "sendSessionId",
    "runOptions.projectId",
    "controller.signal",
    "userId",
    "attachments",
    "goalModeForRun",
    "options.goalControlAction === \"start\" ? goalForRun?.goal_id || null : null",
    "contextGoalIdForRun",
    "options.goalControlAction || null",
    "options.skillHints",
    "runOptions.llmSelection.modelId",
    "runOptions.llmSelection.thinkingLevel",
    "runOptions.llmSelection.credentialName",
    "goalModeForRun ? \"off\" : runOptions.requestedRunReviewPolicy",
  ],
  "store streamAgent call must preserve the target positional contract without an analytics selector",
);

assert.equal(
  /analytics|Analytics|analytics_model_id|plus-model|分析模型/i.test(storeSource + "\n" + chatInputSource),
  false,
  "store and ChatInput overlays must not retain analytics-model state, UI, or API plumbing",
);
assert.equal(
  /resolve(?:DatabaseSqlRevision|DimensionBuildRule|LogicalDataset)|(?:DatabaseSqlRevision|DimensionBuildRule|LogicalDataset)Request|listTaskNotifications|markTaskNotificationRead|getSemanticDimensionBuildJob/i.test(
  chatMessageSource + "\n" + navbarSource + "\n" + settingsSource,
  ),
  false,
  "effective generic overlays must not import Platform-only message, notification, or settings APIs",
);
assert.equal(
  /rawKnowledgeFileUrl|knowledge:\/\/|\/knowledge\/|knowledge_image|sourcePortableEvidence|isPortableEvidence/i.test(sourcesPanelSource),
  false,
  "SourcesPanel overlay must keep generic resource handling without Knowledge-only URL/evidence plumbing",
);
assert.equal(
  /gbrain|Gbrain/.test(mcpCatalogSource),
  false,
  "McpCatalog overlay must not retain built-in gbrain lifecycle state or special casing",
);
assert.equal(
  /activeCategory\s*===\s*"(?:knowledge|rag|databaseQa)"/.test(settingsSource),
  false,
  "Harness settings overlay must not render Platform-owned settings categories",
);
for (const symbol of [
  "PermissionRequestCard",
  "UserInputRequestCard",
  "SkillSecretRequestCard",
  "KernelFallbackRequestCard",
  "AssistantAttachmentList",
]) {
  assert.match(chatMessageSource, new RegExp("\\b" + symbol + "\\b"), "ChatMessage lost generic behavior: " + symbol);
}
for (const symbol of [
  "SourceItem",
  "SourcesCard",
  "PermissionGrant",
  "listSessionPermissions",
  "GoalCard",
  "ArtifactsCard",
]) {
  assert.match(sourcesPanelSource, new RegExp("\\b" + symbol + "\\b"), "SourcesPanel lost generic behavior: " + symbol);
}
for (const symbol of ["normalizeConfig", "getMcpServersStatus", "McpServerModal", "NewMcpServerModal"]) {
  assert.match(mcpCatalogSource, new RegExp("\\b" + symbol + "\\b"), "McpCatalog lost generic MCP behavior: " + symbol);
}
for (const symbol of [
  "SettingsNavigation",
  "MemoryEditor",
  "HeadlessActivityPanel",
  "CapabilitiesStatus",
  "activeCategory === \"harness\"",
]) {
  assert.ok(settingsSource.includes(symbol), "settings overlay lost generic behavior: " + symbol);
}
for (const symbol of [
  "goalModeEnabled",
  "setGoalModeEnabled",
  "activeGoal",
  "cancelActiveGoal",
  "PermissionRequest",
  "UserInputRequest",
  "SkillSecretRequest",
  "KernelFallbackRequest",
]) {
  assert.match(storeSource, new RegExp("\\b" + symbol + "\\b"), "store lost generic Goal/HITL behavior: " + symbol);
}
for (const symbol of [
  "sendMessage",
  "uploadAgentAttachments",
  "goalModeEnabled",
  "setApprovalMode",
]) {
  assert.match(chatInputSource, new RegExp("\\b" + symbol + "\\b"), "ChatInput lost generic behavior: " + symbol);
}

// Parse-only checks cannot catch an OpenPopover union drift or an API type
// mismatch. Compile the effective overlay graph with the frontend tsconfig:
// @/ imports prefer a target overlay and fall back to the legacy frontend
// module when that module has not been extracted yet.
const frontendRoot = path.join(repositoryRoot, "frontend");
const legacyFrontendSrc = path.join(frontendRoot, "src");
const overlayFrontendSrc = path.join(repositoryRoot, "packages/puddingharness-extraction/overlays/frontend/src");
const tsconfigPath = path.join(frontendRoot, "tsconfig.json");
const config = ts.readConfigFile(tsconfigPath, ts.sys.readFile);
assert.equal(config.error, undefined, "frontend tsconfig must be readable");
const parsedConfig = ts.parseJsonConfigFileContent(config.config, ts.sys, frontendRoot);
const compilerOptions = {
  ...parsedConfig.options,
  noEmit: true,
  incremental: false,
};
const baseCompilerHost = ts.createCompilerHost(compilerOptions, true);

function overlayForLegacy(filePath) {
  const relative = path.relative(legacyFrontendSrc, filePath);
  if (relative.startsWith(".." + path.sep)) return filePath;
  const candidate = path.join(overlayFrontendSrc, relative);
  return fs.existsSync(candidate) ? candidate : filePath;
}

function legacyForOverlay(filePath) {
  const relative = path.relative(overlayFrontendSrc, filePath);
  if (relative.startsWith(".." + path.sep)) return filePath;
  return path.join(legacyFrontendSrc, relative);
}

const compilerHost = { ...baseCompilerHost };
compilerHost.resolveModuleNames = (moduleNames, containingFile) =>
  moduleNames.map((moduleName) => {
    const legacyContainingFile = legacyForOverlay(containingFile);
    if (moduleName.startsWith("@/")) {
      const legacyAliasPath = path.join(legacyFrontendSrc, moduleName.slice(2));
      const overlayAliasPath = overlayForLegacy(legacyAliasPath);
      if (fs.existsSync(overlayAliasPath)) {
        return {
          resolvedFileName: overlayAliasPath,
          extension: ts.extensionFromPath(overlayAliasPath),
        };
      }
    }
    const resolved = ts.resolveModuleName(
      moduleName,
      legacyContainingFile,
      compilerOptions,
      baseCompilerHost,
    ).resolvedModule;
    if (!resolved) return undefined;
    const effectiveFileName = overlayForLegacy(resolved.resolvedFileName);
    return effectiveFileName === resolved.resolvedFileName
      ? resolved
      : { ...resolved, resolvedFileName: effectiveFileName };
  });

const semanticRoots = [
  path.join(frontendRoot, "next-env.d.ts"),
  path.join(frontendRoot, "src/types/electron.d.ts"),
  apiPath,
  storePath,
  chatInputPath,
  chatMessagePath,
  navbarPath,
  settingsPath,
  evalHookPath,
];
const semanticProgram = ts.createProgram(semanticRoots, compilerOptions, compilerHost);
const semanticDiagnostics = ts.getPreEmitDiagnostics(semanticProgram);
const overlayPaths = new Set([
  apiPath,
  storePath,
  chatInputPath,
  chatMessagePath,
  navbarPath,
  settingsPath,
  evalHookPath,
]);
const overlayDiagnostics = semanticDiagnostics.filter(
  (diagnostic) => diagnostic.file && overlayPaths.has(diagnostic.file.fileName),
);
assert.deepEqual(
  overlayDiagnostics,
  [],
  "effective API/store/ChatInput overlays must have zero TypeScript diagnostics:\n" +
    overlayDiagnostics.map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n")).join("\n"),
);
console.log("effective overlay TypeScript check: passed");

console.log("store/ChatInput effective overlay boundary: passed");
