const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const electronRoot = path.resolve(__dirname, '..');
const read = (relativePath) => fs.readFileSync(path.join(electronRoot, relativePath), 'utf8');

test('desktop boundary has only generic Harness process and onboarding surfaces', () => {
  const main = read('main.js');
  const preload = read('preload.js');
  const types = read('electron.d.ts');
  const backend = read('managers/backend.js');
  const cli = read('managers/cli.js');
  const packageJson = JSON.parse(read('package.json'));

  for (const source of [main, preload, types, backend, cli, JSON.stringify(packageJson)]) {
    assert.doesNotMatch(
      source,
      /knowledge|analytics|gbrain|milvus|mineru|select-knowledge|start-infra|stop-infra|get-infra|infra-status/i,
    );
  }
  assert.doesNotMatch(backend, /PUDDINGCLAW_(PROFILE|EXTENSIONS)|MINERU_URL/);
  assert.match(main, /managers\/backend/);
  assert.match(main, /startFrontendServer/);
  assert.match(main, /getConfiguredPorts\(\)\.backendPort/);
  assert.match(main, /getConfiguredPorts\(\)\.frontendPort/);
  assert.doesNotMatch(main, /lsof|xargs\s+kill|execSync/);
  assert.match(preload, /startBackend/);
  assert.match(preload, /getOnboardingState/);
  assert.equal(packageJson.build.productName, 'PuddingHarness');
  assert.equal(packageJson.build.appId, 'com.puddingai.puddingharness');
  assert.equal(fs.existsSync(path.join(electronRoot, 'managers', 'docker.js')), false);
  assert.equal(packageJson.build.extraResources.some((item) => String(item.from || '').includes('docker')), false);
});

test('packaged backend selection contains target generic roots only', () => {
  const resources = JSON.parse(read('package.json')).build.extraResources;
  const backend = resources.find((item) => item.to === 'backend');
  assert.ok(backend);
  assert.ok(backend.filter.includes('harness/**/*'));
  assert.ok(backend.filter.includes('runtime_identity/**/*'));
  assert.ok(backend.filter.includes('services/**/*'));
  assert.equal(backend.filter.some((item) => /knowledge|analytics|vanna|docker-compose/i.test(item)), false);
});

test('packaged frontend uses the CLI bundle with runtime API rewrites', () => {
  const vm = require('node:vm');
  const context = {
    require(name) {
      if (name === 'electron') return { app: { isPackaged: true } };
      return require(name);
    },
    process: { resourcesPath: '/app/resources' },
    module: { exports: {} },
  };
  vm.runInNewContext(read('managers/paths.js'), context);
  assert.equal(context.module.exports.getFrontendStandaloneDir(), '/app/resources/cli/runtime-bundle/web');
  const packageJson = JSON.parse(read('package.json'));
  assert.equal(packageJson.build.extraResources.some((item) => item.to === 'frontend/.next-build/standalone'), false);
  const cliResource = packageJson.build.extraResources.find((item) => item.to === 'cli');
  assert.ok(cliResource.filter.includes('runtime-bundle/**/*'));
  assert.equal(packageJson.scripts.prebuild, 'npm run verify:runtime');
});
