import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { homePaths } from "../src/home.js";
import { inspectProfile, applyProfile } from "../src/profile-commands.js";

test("profile inspection exposes the single Harness profile", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "puddingharness-profile-"));
  try {
    const result = await inspectProfile("harness", homePaths(home));
    assert.equal(result.profile, "harness");
    assert.equal(result.initialized, false);
    assert.equal(JSON.stringify(result).includes("knowledge"), false);
    await assert.rejects(inspectProfile("full", homePaths(home)), /unknown profile/);
  } finally { await rm(home, { recursive: true, force: true }); }
});

test("profile apply writes only the Harness deployment contract", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "puddingharness-profile-"));
  try {
    const result = await applyProfile("harness", homePaths(home));
    assert.equal(result.profile, "harness");
    const config = JSON.parse(await readFile(path.join(home, "deploy.json"), "utf8"));
    assert.equal(Object.hasOwn(config, "extensions"), false);
    assert.equal(Object.hasOwn(config.infrastructure, "milvus"), false);
  } finally { await rm(home, { recursive: true, force: true }); }
});
