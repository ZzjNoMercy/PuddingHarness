import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import fsSync from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { admitHomeWrite, admissionNames } from "../src/home-admission.js";

const here = path.dirname(fileURLToPath(import.meta.url));

async function home() {
  const target = await fs.mkdtemp(path.join(os.tmpdir(), "puddingharness-admission-"));
  await fs.chmod(target, 0o700);
  return target;
}

test("successful write creates a ticket before work and cleans it after work", async () => {
  const target = await home();
  const events = [];
  await admitHomeWrite(target, "config-set", async () => {
    events.push(await fs.readdir(path.join(target, admissionNames.LEASES)));
    assert.equal((await fs.readdir(target)).includes(admissionNames.GATE), false);
  });
  assert.equal(events.length, 1);
  assert.deepEqual(await fs.readdir(path.join(target, admissionNames.LEASES)), []);
  assert.equal((await fs.readdir(target)).includes(admissionNames.GATE), false);
});

test("failed write retains its ticket and stale gate is never auto-cleaned", async () => {
  const target = await home();
  await assert.rejects(() => admitHomeWrite(target, "runtime-install", async () => {
    throw new Error("business failure");
  }), /business failure/);
  assert.equal((await fs.readdir(path.join(target, admissionNames.LEASES))).length, 1);
  await fs.mkdir(path.join(target, admissionNames.GATE), { mode: 0o700 });
  await assert.rejects(() => admitHomeWrite(target, "runtime-install", async () => {}), /busy|stale/);
  assert.ok((await fs.lstat(path.join(target, admissionNames.GATE))).isDirectory());
});

test("freeze marker and partial marker deny writes before ticket creation", async () => {
  for (const marker of [admissionNames.FREEZE, admissionNames.FREEZE_PART]) {
    const target = await home();
    await fs.writeFile(path.join(target, marker), "marker\n", { mode: 0o600 });
    await assert.rejects(() => admitHomeWrite(target, "init", async () => {}), /frozen/);
    assert.deepEqual(await fs.readdir(path.join(target, admissionNames.LEASES)).catch(() => []), []);
  }
});

test("frozen existing Home mode is observed without silent chmod", async () => {
  const target = await home();
  await fs.chmod(target, 0o755);
  const before = (await fs.stat(target)).mode & 0o777;
  await fs.writeFile(path.join(target, admissionNames.FREEZE), "marker\n", { mode: 0o600 });
  await assert.rejects(() => admitHomeWrite(target, "init", async () => {}), /frozen/);
  assert.equal((await fs.stat(target)).mode & 0o777, before);
});

test("nested same-Home writes share one ticket through AsyncLocalStorage", async () => {
  const target = await home();
  await admitHomeWrite(target, "init", async () => {
    await admitHomeWrite(target, "config-set", async () => {
      assert.equal((await fs.readdir(path.join(target, admissionNames.LEASES))).length, 1);
    });
  });
  assert.deepEqual(await fs.readdir(path.join(target, admissionNames.LEASES)), []);
});

test("completed async context is revoked and must re-check a newly published freeze", async () => {
  const target = await home();
  let delayed;
  let release;
  const later = new Promise(resolve => { release = resolve; });
  await admitHomeWrite(target, "init", async () => {
    delayed = later.then(() => admitHomeWrite(target, "config-set", async () => {}));
  });
  await fs.writeFile(path.join(target, admissionNames.FREEZE), "marker\n", { mode: 0o600 });
  release();
  await assert.rejects(delayed, /frozen/);
});

test("started but unawaited nested write keeps the outer ticket until it finishes", async () => {
  const target = await home();
  let release;
  let observed;
  let started;
  const nested = new Promise((resolve) => { release = resolve; });
  await admitHomeWrite(target, "init", async () => {
    started = admitHomeWrite(target, "config-set", async () => {
      observed = (await fs.readdir(path.join(target, admissionNames.LEASES))).length;
      return nested;
    });
    setTimeout(release, 25);
  });
  assert.equal(observed, 1);
  await started;
  assert.deepEqual(await fs.readdir(path.join(target, admissionNames.LEASES)), []);
});

test("killed writer leaves its ticket for freeze review", async () => {
  const target = await home();
  const modulePath = path.resolve(here, "../src/home-admission.js");
  const child = spawn(process.execPath, ["--input-type=module", "-e", `
    import { admitHomeWrite } from ${JSON.stringify(modulePath)};
    const hold = setInterval(() => {}, 1000);
    await admitHomeWrite(${JSON.stringify(target)}, "agent-run", async () => new Promise(() => {}));
    clearInterval(hold);
  `], { stdio: "ignore" });
  await new Promise((resolve, reject) => {
    const timer = setInterval(async () => {
      try {
        const entries = await fs.readdir(path.join(target, ".installation-cli-leases"));
        if (entries.length) { clearInterval(timer); resolve(); }
      } catch {}
    }, 10);
    child.once("error", reject);
    setTimeout(() => { clearInterval(timer); reject(new Error("ticket timeout")); }, 2000);
  });
  assert.equal(child.kill("SIGKILL"), true);
  const exit = await new Promise((resolve) => child.once("exit", (code, signal) => resolve({ code, signal })));
  assert.equal(exit.signal, "SIGKILL");
  assert.equal((await fs.readdir(path.join(target, admissionNames.LEASES))).length, 1);
});


test("nested failure is reported and retains the outer ticket", async () => {
  const target = await home();
  await assert.rejects(admitHomeWrite(target, "init", async () => {
    await admitHomeWrite(target, "config-set", async () => { throw new Error("nested failure"); }).catch(() => {});
  }), /nested failure/);
  assert.equal((await fs.readdir(path.join(target, admissionNames.LEASES))).length, 1);
});

test("Windows write refusal is explicit and creates no Home", async () => {
  const parent = await home();
  const target = path.join(parent, "absent");
  const modulePath = path.resolve(here, "../src/home-admission.js");
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", `
    import { admitHomeWrite } from ${JSON.stringify(modulePath)};
    Object.defineProperty(process, "platform", {value:"win32"});
    try { await admitHomeWrite(${JSON.stringify(target)}, "init", async () => { throw new Error("callback ran"); }); }
    catch(error) { if(error.code === "installation_admission_unsupported") process.exit(0); throw error; }
    process.exit(1);
  `], {encoding:"utf8"});
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(await fs.readdir(parent), []);
});
