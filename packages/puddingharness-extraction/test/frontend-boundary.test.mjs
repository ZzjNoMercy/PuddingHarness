import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const sidebarPath = path.join(
  repositoryRoot,
  "frontend/src/components/layout/Sidebar.tsx",
);
const source = fs.readFileSync(sidebarPath, "utf8");
const requireFromFrontend = createRequire(path.join(repositoryRoot, "frontend/package.json"));
const ts = requireFromFrontend("typescript");
const sourceFile = ts.createSourceFile(
  sidebarPath,
  source,
  ts.ScriptTarget.Latest,
  true,
  ts.ScriptKind.TSX,
);

function walk(node, visit) {
  visit(node);
  node.forEachChild((child) => walk(child, visit));
}

function jsxText(node) {
  if (ts.isJsxText(node)) return node.text.replace(/\s+/g, " ").trim();
  if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
    const children = ts.isJsxElement(node) ? node.children : [];
    return children.map(jsxText).filter(Boolean).join(" ");
  }
  return "";
}

function literalAttribute(element, name) {
  const attribute = element.attributes.properties.find(
    (property) => ts.isJsxAttribute(property) && property.name.text === name,
  );
  if (!attribute || !attribute.initializer) return null;
  if (ts.isStringLiteral(attribute.initializer)) return attribute.initializer.text;
  if (
    ts.isJsxExpression(attribute.initializer) &&
    attribute.initializer.expression &&
    ts.isStringLiteral(attribute.initializer.expression)
  ) {
    return attribute.initializer.expression.text;
  }
  return null;
}

const imports = [];
const identifiers = new Set();
const links = [];
walk(sourceFile, (node) => {
  if (ts.isImportDeclaration(node)) {
    imports.push(node.moduleSpecifier.text);
  }
  if (ts.isIdentifier(node)) identifiers.add(node.text);
  if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
    const opening = ts.isJsxElement(node) ? node.openingElement : node;
    const tagName = opening.tagName.getText(sourceFile);
    if (tagName === "Link" || tagName === "SidebarLink") {
      links.push({
        tagName,
        href: literalAttribute(opening, "href"),
        label: literalAttribute(opening, "label"),
        text: ts.isJsxElement(node) ? jsxText(node) : "",
      });
    }
  }
});

assert.equal(sourceFile.parseDiagnostics.length, 0, "Sidebar source must parse as TSX");

// This is a navigation boundary test, so inspect JSX and imports rather than
// snapshotting the complete component. Platform-only paths cannot return via
// a renamed label or a new conditional branch.
const forbiddenPath = /\/(?:knowledge|analytics|imports|sources|schema|read[-_]later)(?:\/|$)/i;
for (const link of links) {
  if (link.href) assert.equal(forbiddenPath.test(link.href), false, `platform route leaked: ${link.href}`);
}

const forbiddenImport = /(?:useRuntimeProfile|knowledge|analytics|vanna|read[-_]later)/i;
for (const moduleName of imports) {
  assert.equal(forbiddenImport.test(moduleName), false, `platform-only dependency leaked: ${moduleName}`);
}

const hrefs = new Set(links.map((link) => link.href).filter(Boolean));
assert.ok(hrefs.has("/extension/connectors"), "generic MCP management must remain reachable");
assert.ok(hrefs.has("/evaluation/datasets"), "generic Harness evaluation must remain reachable");
assert.ok(hrefs.has("/settings"), "generic settings must remain reachable");

const visibleLabels = links.flatMap((link) => [link.label, link.text]).filter(Boolean).join(" ");
assert.match(visibleLabels, /MCP/);
assert.match(visibleLabels, /评估/);
assert.match(visibleLabels, /设置/);
assert.match(visibleLabels, /定时任务/);

// These identifiers are the host contracts that keep Chat, project/workspace,
// and Session behavior intact in the overlay. Their implementations are host
// dependencies until the extracted Harness supplies generic ports.
for (const contract of [
  "setWorkspaceView",
  "setSessionId",
  "SessionItem",
  "ProjectItem",
  "SessionSearchDialog",
  "useApp",
]) {
  assert.ok(identifiers.has(contract), `generic host behavior was removed: ${contract}`);
}

console.log("frontend boundary: passed");
