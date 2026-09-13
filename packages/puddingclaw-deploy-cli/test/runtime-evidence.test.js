import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { verifyBuildEvidence } from "../scripts/runtime-evidence.mjs";

async function fixture() {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), "puddingharness-evidence-"));
  const evidence = {
    mode: "independent_source_stages", releaseable: false, source_revision: "a".repeat(40), source_clean: true,
    backend: { audit_status: "python_static_clean", raw_findings: [], blocking_findings: [], reviewed_findings: [], files: [{path:"app.py",sha256:"b".repeat(64)}], wheel: { name: "puddingharness-backend", version: "0.1.0", python_files_verified: 1 } },
    frontend: { checks: { install: "passed", typecheck: "passed", build: "passed" }, files: [{path:"package.json",sha256:"c".repeat(64)}], node_modules: { present: true, developerOnly: false, mode: "installed" } },
  };
  await fs.writeFile(path.join(root, "build-evidence.json"), `${JSON.stringify(evidence)}\n`);
  return { root, evidence, manifest: { files: { "build-evidence.json": "a".repeat(64) } } };
}

test("accepts independent source stage evidence only", async () => {
  const { root, evidence, manifest } = await fixture();
  assert.deepEqual(await verifyBuildEvidence(root, manifest), evidence);
});

for (const [name, mutate] of [
  ["unverified prebuilt", (e) => { e.mode = "unverified_prebuilt"; }],
  ["unknown mode", (e) => { e.mode = "unknown"; }],
  ["short revision", (e) => { e.source_revision = "abc"; }],
  ["dirty source", (e) => { e.source_clean = false; }],
  ["blocking finding", (e) => { e.backend.blocking_findings = ["x"]; }],
  ["unreviewed audit", (e) => { e.backend.audit_status = "unknown"; }],
  ["empty inventory", (e) => { e.backend.files = []; }],
  ["wrong wheel count", (e) => { e.backend.wheel.python_files_verified = 2; }],
  ["missing install", (e) => { delete e.frontend.checks.install; }],
  ["reused node_modules link", (e) => { e.frontend.node_modules.mode = "symlink"; }],
]) {
  test(`rejects ${name} evidence`, async () => {
    const { root, evidence, manifest } = await fixture();
    mutate(evidence);
    await fs.writeFile(path.join(root, "build-evidence.json"), JSON.stringify(evidence));
    await assert.rejects(() => verifyBuildEvidence(root, manifest), /build evidence invalid/);
  });
}

test("rejects missing or unlisted build evidence", async () => {
  const { root, manifest } = await fixture();
  await assert.rejects(() => verifyBuildEvidence(root, { files: {} }), /covered by manifest/);
  await fs.unlink(path.join(root, "build-evidence.json"));
  await assert.rejects(() => verifyBuildEvidence(root, manifest), /cannot be read/);
});
