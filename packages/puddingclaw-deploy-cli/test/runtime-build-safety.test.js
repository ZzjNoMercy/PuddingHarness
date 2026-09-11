import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const script = fileURLToPath(new URL('../scripts/build-embedded-runtime.mjs', import.meta.url));
test('embedded builder refuses existing bundles and source-tree outputs without deleting them', async () => {
 const tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'harness-build-safety-'));
 try {
  const source = path.join(tmp, 'source');
  await fs.mkdir(path.join(source, 'backend'), { recursive: true });
  await fs.mkdir(path.join(source, 'frontend'));
  const existing = path.join(tmp, 'runtime-bundle-existing');
  await fs.mkdir(existing);
  await fs.writeFile(path.join(existing, 'sentinel'), 'keep existing artifact');
  const env = {...process.env, PUDDINGHARNESS_SOURCE_ROOT: source};
  assert.throws(() => execFileSync(process.execPath, [script, '--output', existing], {env, stdio:'pipe'}), /EEXIST/);
  assert.equal(await fs.readFile(path.join(existing, 'sentinel'), 'utf8'), 'keep existing artifact');
  for (const directory of ['backend', 'frontend']) {
   const output = path.join(source, directory, 'runtime-bundle-nested');
   assert.throws(() => execFileSync(process.execPath, [script, '--output', output], {env, stdio:'pipe'}), /inside backend or frontend/);
   await assert.rejects(fs.stat(output), {code:'ENOENT'});
  }
 } finally { await fs.rm(tmp, {recursive:true, force:true}); }
});
