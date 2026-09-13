import test from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { resolveHome, homePaths } from "../src/home.js";
import { defaultConfig, loadConfig } from "../src/config.js";
import { buildInitPlan } from "../src/init-schema.js";
import { validateRuntimeManifest } from "../src/runtime-bundle.js";

const root = path.resolve(new URL("..", import.meta.url).pathname);
const cli = path.join(root, "src", "cli.js");
const version = createRequire(import.meta.url)("../package.json").version;

async function runCli(args, home) {
  const child = spawn(process.execPath, [cli, ...args], {
    env: { ...process.env, PUDDINGHARNESS_HOME: home },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stdout = ""; let stderr = "";
  child.stdout.on("data", (chunk) => { stdout += chunk; });
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [code] = await once(child, "close");
  return { code, stdout, stderr };
}

test("Harness identity and isolated Home do not use legacy environment", () => {
  const target = "/tmp/puddingharness-test-home";
  assert.equal(resolveHome({ PUDDINGHARNESS_HOME: target, PUDDINGCLAW_HOME: "/tmp/legacy" }), target);
  assert.match(resolveHome({ HOME: "/tmp/user" }), /\.puddingharness$/);
  assert.equal(homePaths(target).home, target);
});

test("default config contains only generic Harness deployment state", () => {
  const config = defaultConfig();
  assert.equal(config.profile, "harness");
  assert.equal(Object.hasOwn(config, "extensions"), false);
  assert.deepEqual(Object.keys(config.infrastructure), ["catalog"]);
  assert.equal(Object.hasOwn(config.infrastructure, "milvus"), false);
});

test("legacy service fields are dropped on config load", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "puddingharness-config-"));
  try {
    const file = path.join(home, "deploy.json");
    await writeFile(file, JSON.stringify({ ...defaultConfig(), extensions: { legacy: true }, infrastructure: { catalog: defaultConfig().infrastructure.catalog, milvus: { enabled: true } } }));
    const config = await loadConfig(file);
    assert.equal(Object.hasOwn(config, "extensions"), false);
    assert.equal(Object.hasOwn(config.infrastructure, "milvus"), false);
  } finally { await rm(home, { recursive: true, force: true }); }
});

test("init plan has no product service branches", () => {
  const plan = buildInitPlan("harness");
  assert.ok(plan.execution_order.includes("harness.subagents"));
  assert.equal(JSON.stringify(plan).includes("knowledge"), false);
  assert.equal(JSON.stringify(plan).includes("analytics"), false);
  assert.throws(() => buildInitPlan("full"), /unknown profile/);
});

test("CLI plan is read-only and accepts only Harness profile", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "puddingharness-cli-"));
  try {
    const result = await runCli(["init", "--profile", "harness", "--plan", "--json"], home);
    assert.equal(result.code, 0, result.stderr);
    assert.equal(JSON.parse(result.stdout).status, "plan");
    await assert.rejects(readFile(path.join(home, "deploy.json")), { code: "ENOENT" });
    const rejected = await runCli(["init", "--profile", "knowledge", "--plan", "--json"], home);
    assert.equal(rejected.code, 2);
  } finally { await rm(home, { recursive: true, force: true }); }
});

test("version exposes Harness runtime identity while retaining bin compatibility", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "puddingharness-version-"));
  try {
    const result = await runCli(["version", "--json"], home);
    assert.equal(result.code, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), { schema_version: "1", cli: "puddingharness", cli_version: version, agent_id: "puddingharness", protocol_version: "1" });
  } finally { await rm(home, { recursive: true, force: true }); }
});

test("runtime manifest requires the Harness contract", () => {
  const manifest = {
    schema_version: 1, release_version: "1.0.0", contracts: { harness_home: 1, dynamic_ports: 1, home_freeze: 1 }, files: { "bin/backend": "a".repeat(64), "bin/frontend": "b".repeat(64) },
    processes: { backend: { command: "bin/backend", args: [], cwd: "." }, frontend: { command: "bin/frontend", args: [], cwd: "." } },
  };
  assert.doesNotThrow(() => validateRuntimeManifest(manifest, "/tmp/runtime"));
  assert.throws(() => validateRuntimeManifest({...manifest, contracts:{harness_home:1,dynamic_ports:1}}, "/tmp/runtime"), /home_freeze/);
  assert.throws(() => validateRuntimeManifest({ ...manifest, contracts: { puddingclaw_home: 1, dynamic_ports: 1 } }, "/tmp/runtime"), /harness_home/);
});

test("worker manifest exposes generic Harness operations only", async () => {
  const manifest = JSON.parse(await readFile(path.join(root, "worker.manifest.json"), "utf8"));
  const pkg = JSON.parse(await readFile(path.join(root, "package.json"), "utf8"));
  assert.equal(manifest.id, "puddingharness");
  assert.deepEqual(Object.keys(pkg.bin), [manifest.transport.command]);
  assert.deepEqual(manifest.capabilities, ["agent.run", "agent.continue", "agent.respond", "agent.cancel", "hitl.permission"]);
  assert.equal(Object.hasOwn(manifest, "modelRouting"), false);
  assert.equal(JSON.stringify(manifest).includes("knowledge"), false);
  assert.equal(JSON.stringify(manifest).includes("analytics"), false);
});

test("runtime builder requires an explicit source root", async () => {
  const child = spawn(process.execPath, [path.join(root, "scripts", "build-embedded-runtime.mjs"), "--skip-build", "--output", "/tmp/runtime-bundle-contract-test"], {
    env: { ...process.env, PUDDINGHARNESS_SOURCE_ROOT: "" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const [code] = await once(child, "close");
  assert.notEqual(code, 0);
  assert.match(stderr, /PUDDINGHARNESS_SOURCE_ROOT/);
});
