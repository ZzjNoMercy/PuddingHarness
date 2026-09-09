const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const Module = require('node:module');
const net = require('node:net');
const test = require('node:test');
const { assertPortAvailable, isPortOccupied } = require('../managers/port-guard');

function loadElectronManagers() {
  const originalLoad = Module._load;
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === 'electron') return { app: { isPackaged: false } };
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    const cliPath = require.resolve('../managers/cli');
    const backendPath = require.resolve('../managers/backend');
    delete require.cache[cliPath];
    delete require.cache[backendPath];
    return {
      cli: require(cliPath),
      backend: require(backendPath),
    };
  } finally {
    Module._load = originalLoad;
  }
}

test('Electron and embedded CLI share Harness Home and configured ports', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'puddingharness-electron-'));
  const runtimeRoot = path.join(home, 'runtime-root');
  fs.mkdirSync(path.join(runtimeRoot, 'backend'), { recursive: true });
  fs.mkdirSync(path.join(home, 'runtime'), { recursive: true });
  fs.writeFileSync(path.join(home, 'deploy.json'), JSON.stringify({
    initialized: true,
    runtime: { python: { command: process.execPath } },
    server: { backend_port: 8899, frontend_port: 3011 },
  }));
  fs.writeFileSync(path.join(home, 'runtime', 'active.json'), JSON.stringify({ path: runtimeRoot }));

  const previousHarnessHome = process.env.PUDDINGHARNESS_HOME;
  const previousLegacyHome = process.env.PUDDINGCLAW_HOME;
  process.env.PUDDINGHARNESS_HOME = home;
  delete process.env.PUDDINGCLAW_HOME;
  try {
    const { cli, backend } = loadElectronManagers();
    assert.equal(cli.getPuddingHarnessHome(), home);
    assert.deepEqual(cli.getConfiguredPorts(), { backendPort: 8899, frontendPort: 3011 });
    const environment = cli.getCliEnvironment();
    assert.equal(environment.PUDDINGHARNESS_HOME, home);
    assert.equal(environment.PUDDINGCLAW_HOME, undefined);
    assert.equal(backend.getStatus().url, 'http://127.0.0.1:8899');

    const prepared = cli.getPreparedBackendCommand();
    assert.ok(prepared);
    assert.equal(prepared.cwd, path.join(runtimeRoot, 'backend'));
    assert.equal(prepared.args.at(-1), '8899');
  } finally {
    if (previousHarnessHome === undefined) delete process.env.PUDDINGHARNESS_HOME;
    else process.env.PUDDINGHARNESS_HOME = previousHarnessHome;
    if (previousLegacyHome === undefined) delete process.env.PUDDINGCLAW_HOME;
    else process.env.PUDDINGCLAW_HOME = previousLegacyHome;
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('legacy PuddingClaw Home is never selected by the Harness desktop', () => {
  const legacyHome = fs.mkdtempSync(path.join(os.tmpdir(), 'puddingclaw-electron-'));
  const previousHarnessHome = process.env.PUDDINGHARNESS_HOME;
  const previousLegacyHome = process.env.PUDDINGCLAW_HOME;
  delete process.env.PUDDINGHARNESS_HOME;
  process.env.PUDDINGCLAW_HOME = legacyHome;
  try {
    const { cli } = loadElectronManagers();
    assert.notEqual(cli.getPuddingHarnessHome(), legacyHome);
    assert.equal(cli.getPuddingHarnessHome(), path.join(os.homedir(), '.puddingharness'));
    assert.equal(cli.getCliEnvironment().PUDDINGHARNESS_HOME, path.join(os.homedir(), '.puddingharness'));
    assert.equal(cli.getCliEnvironment().PUDDINGCLAW_HOME, undefined);
  } finally {
    if (previousHarnessHome === undefined) delete process.env.PUDDINGHARNESS_HOME;
    else process.env.PUDDINGHARNESS_HOME = previousHarnessHome;
    if (previousLegacyHome === undefined) delete process.env.PUDDINGCLAW_HOME;
    else process.env.PUDDINGCLAW_HOME = previousLegacyHome;
    fs.rmSync(legacyHome, { recursive: true, force: true });
  }
});

test('frontend port guard refuses an unrelated listener without killing it', async () => {
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  try {
    assert.equal(await isPortOccupied(port), true);
    await assert.rejects(
      assertPortAvailable(port, 'frontend'),
      /already in use; refusing to terminate or reuse an unmanaged process/,
    );
    assert.equal(await isPortOccupied(port), true);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
});

test('dev backend uses only the locked target dependency group', () => {
  const { backend } = loadElectronManagers();
  const command = backend.getBackendCommand(8899);
  assert.equal(command.cmd, 'uv');
  assert.deepEqual(command.args.slice(0, 4), ['run', '--locked', '--group', 'dev']);
  assert.equal(command.args.includes('--all-extras'), false);
  assert.equal(command.args.includes('deepagents-test'), false);
});

test('backend refuses to reuse an unrelated HTTP listener on its configured port', async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'puddingharness-backend-conflict-'));
  const server = require('node:http').createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end('{}');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  fs.writeFileSync(path.join(home, 'deploy.json'), JSON.stringify({
    server: { backend_port: port, frontend_port: 3000 },
  }));
  const previousHarnessHome = process.env.PUDDINGHARNESS_HOME;
  process.env.PUDDINGHARNESS_HOME = home;
  try {
    const { backend } = loadElectronManagers();
    const result = await backend.startBackend();
    assert.equal(result.status, 'error');
    assert.match(result.message, /unmanaged process; refusing to reuse it/);
    assert.equal(await isPortOccupied(port), true);
  } finally {
    if (previousHarnessHome === undefined) delete process.env.PUDDINGHARNESS_HOME;
    else process.env.PUDDINGHARNESS_HOME = previousHarnessHome;
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    fs.rmSync(home, { recursive: true, force: true });
  }
});

test('packaged main passes the configured numeric port to its real conflict guard', async () => {
  const vm = require('node:vm');
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'harness-main-contract-'));
  fs.writeFileSync(path.join(directory, 'server.js'), '');
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  const context = {
    require(name) {
      if (name === 'electron') return {
        app: { isPackaged: true, whenReady: () => ({ then() {} }), on() {} },
        ipcMain: { handle() {} },
      };
      if (name === './managers/backend') return {};
      if (name === './managers/cli') return { getConfiguredPorts: () => ({ frontendPort: port, backendPort: 18988 }) };
      if (name === './managers/paths') return { getFrontendStandaloneDir: () => directory };
      if (name === './managers/port-guard') return { assertPortAvailable };
      if (name === 'child_process') return { spawn() { throw new Error('must not spawn on an occupied port'); } };
      return require(name);
    },
    process, console, setInterval() {},
  };
  try {
    vm.createContext(context);
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../main.js'), 'utf8'), context);
    assert.equal(vm.runInContext('getFrontendPort()', context), port);
    await assert.rejects(vm.runInContext('startFrontendServer()', context), /already in use/);
    assert.equal(await isPortOccupied(port), true);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
