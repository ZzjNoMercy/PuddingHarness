import test from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
const config = new URL('../next.config.mjs', import.meta.url).href;
function rewrites(address) {
  const env = { ...process.env };
  delete env.BACKEND_INTERNAL_URL;
  if (address !== undefined) env.BACKEND_INTERNAL_URL = address;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e',
    `const {default: config} = await import(${JSON.stringify(config)}); console.log(JSON.stringify(await config.rewrites()));`], { env, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}
test('unbound frontend cannot proxy to an existing local backend', () => {
  assert.deepEqual(rewrites(), []);
  assert.deepEqual(rewrites('   '), []);
});
test('explicit backend binding is preserved', () => {
  assert.deepEqual(rewrites('http://127.0.0.1:19081'), [{ source: '/api/:path*', destination: 'http://127.0.0.1:19081/api/:path*' }]);
});
