import fs from "node:fs/promises";
import path from "node:path";

const SOURCE_REVISION = /^[0-9a-f]{40}$/i;
const AUDIT_STATUSES = new Set(["python_static_clean", "python_static_reviewed"]);
const TOP_LEVEL = new Set(["mode", "releaseable", "source_revision", "source_clean", "backend", "frontend"]);

function fail(message) {
  throw new Error(`build evidence invalid: ${message}`);
}

function object(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) fail(`${label} must be an object`);
  return value;
}

function allowedKeys(value, keys, label) {
  for (const key of Object.keys(value)) if (!keys.has(key)) fail(`${label}.${key} is unknown`);
}

function relativeFile(root, relative) {
  if (typeof relative !== "string" || path.isAbsolute(relative)) fail("build-evidence.json path is invalid");
  const resolved = path.resolve(root, relative);
  if (resolved !== path.resolve(root) && !resolved.startsWith(`${path.resolve(root)}${path.sep}`)) {
    fail("build-evidence.json escapes the runtime bundle");
  }
  return resolved;
}

function passedChecks(checks) {
  object(checks, "frontend.checks");
  allowedKeys(checks, new Set(["install", "typecheck", "build"]), "frontend.checks");
  for (const name of ["install", "typecheck", "build"]) {
    if (checks[name] !== "passed") fail(`frontend.checks.${name} must be passed`);
  }
}

export async function verifyBuildEvidence(runtimeRoot, manifest) {
  if (!manifest?.files || !Object.hasOwn(manifest.files, "build-evidence.json")) {
    fail("build-evidence.json must be covered by manifest.files");
  }
  const file = relativeFile(runtimeRoot, "build-evidence.json");
  let evidence;
  try {
    const stat = await fs.lstat(file);
    if (!stat.isFile() || stat.isSymbolicLink()) fail("build-evidence.json must be a regular file");
    evidence = JSON.parse(await fs.readFile(file, "utf8"));
  } catch (error) {
    if (error.message?.startsWith("build evidence invalid:")) throw error;
    fail(`build-evidence.json cannot be read: ${error.message}`);
  }
  object(evidence, "evidence");
  allowedKeys(evidence, TOP_LEVEL, "evidence");
  if (evidence.mode !== "independent_source_stages") fail("mode must be independent_source_stages");
  if (typeof evidence.source_revision !== "string" || !SOURCE_REVISION.test(evidence.source_revision)) {
    fail("source_revision must be a complete 40-hex revision");
  }
  if (evidence.source_clean !== true) fail("source_clean must be true");

  const backend = object(evidence.backend, "backend");
  allowedKeys(backend, new Set(["audit_status", "raw_findings", "blocking_findings", "reviewed_findings", "files", "wheel"]), "backend");
  if (!AUDIT_STATUSES.has(backend.audit_status)) fail("backend.audit_status is not verified");
  if (!Array.isArray(backend.blocking_findings) || backend.blocking_findings.length) {
    fail("backend.blocking_findings must be empty");
  }
  for (const key of ["raw_findings", "reviewed_findings"]) {
    if (!Array.isArray(backend[key])) fail(`backend.${key} must be an array`);
  }
  if (backend.audit_status === "python_static_clean" && (backend.raw_findings.length || backend.reviewed_findings.length)) fail("clean audit contains findings");
  if (backend.audit_status === "python_static_reviewed" && (!backend.reviewed_findings.length || backend.raw_findings.length !== backend.reviewed_findings.length)) fail("reviewed audit is inconsistent");
  const wheel = object(backend.wheel, "backend.wheel");
  allowedKeys(wheel, new Set(["name", "version", "python_files_verified"]), "backend.wheel");
  if (wheel.name !== "puddingharness-backend" || typeof wheel.version !== "string" || !wheel.version) {
    fail("backend.wheel identity is required");
  }
  if (!Number.isInteger(wheel.python_files_verified) || wheel.python_files_verified <= 0) {
    fail("backend.wheel.python_files_verified must be positive");
  }

  validateFiles(backend.files, "backend.files");
  if (backend.files.length !== wheel.python_files_verified) fail("wheel Python count differs from inventory");
  const frontend = object(evidence.frontend, "frontend");
  allowedKeys(frontend, new Set(["checks", "files", "node_modules"]), "frontend");
  validateFiles(frontend.files, "frontend.files");
  passedChecks(frontend.checks);
  const nodeModules = object(frontend.node_modules, "frontend.node_modules");
  allowedKeys(nodeModules, new Set(["present", "developerOnly", "mode", "source"]), "frontend.node_modules");
  if (nodeModules.mode === "symlink" || nodeModules.source) fail("frontend node_modules must not reuse a link");
  if (nodeModules.present !== true || nodeModules.developerOnly !== false || nodeModules.mode !== "installed") {
    fail("frontend node_modules must be installed locally");
  }
  return evidence;
}

function validateFiles(files, label) {
  if (!Array.isArray(files) || !files.length) fail(`${label} must be nonempty`);
  const paths = new Set();
  for (const item of files) {
    if (!item || typeof item.path !== "string" || !item.path || item.path.includes("\\") || item.path.split("/").some(part => !part || part === "." || part === "..") || paths.has(item.path) || !/^[a-f0-9]{64}$/.test(item.sha256)) fail(`${label} contains invalid or duplicate file`);
    paths.add(item.path);
  }
}
