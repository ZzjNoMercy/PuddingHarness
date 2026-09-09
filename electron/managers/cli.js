const { app } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { getRepoRoot, getBackendDir } = require('./paths');

function getPuddingHarnessHome() {
  return process.env.PUDDINGHARNESS_HOME
    || path.join(os.homedir(), '.puddingharness');
}

function getPuddingClawHome() {
  // Keep the internal export name for existing Electron callers. The runtime
  // contract is the Harness Home, with the old env var accepted as input only.
  return getPuddingHarnessHome();
}

function getConfiguredPorts() {
  try {
    const config = JSON.parse(fs.readFileSync(path.join(getPuddingHarnessHome(), 'deploy.json'), 'utf8'));
    const backendPort = Number(config.server?.backend_port);
    const frontendPort = Number(config.server?.frontend_port);
    return {
      backendPort: Number.isInteger(backendPort) && backendPort >= 1 && backendPort <= 65535 ? backendPort : 8888,
      frontendPort: Number.isInteger(frontendPort) && frontendPort >= 1 && frontendPort <= 65535 ? frontendPort : 3000,
    };
  } catch {
    return { backendPort: 8888, frontendPort: 3000 };
  }
}

function getCliEntry() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'cli', 'src', 'cli.js');
  }
  return path.join(getRepoRoot(), 'packages', 'puddingclaw-deploy-cli', 'src', 'cli.js');
}

function getCliEnvironment() {
  const home = getPuddingHarnessHome();
  const environment = {
    ...process.env,
    ELECTRON_RUN_AS_NODE: '1',
    PUDDINGHARNESS_HOME: home,
    PUDDINGCLAW_DESKTOP_PACKAGED: app.isPackaged ? '1' : '0',
  };
  delete environment.PUDDINGCLAW_HOME;
  if (!app.isPackaged && !environment.PUDDINGCLAW_DEPLOY_PYTHON) {
    const python = process.platform === 'win32'
      ? path.join(getBackendDir(), '.venv', 'Scripts', 'python.exe')
      : path.join(getBackendDir(), '.venv', 'bin', 'python');
    if (fs.existsSync(python)) environment.PUDDINGCLAW_DEPLOY_PYTHON = python;
  }
  return environment;
}

function runCli(args, { timeoutMs = 20_000 } = {}) {
  const cliEntry = getCliEntry();
  return new Promise((resolve, reject) => {
    if (!fs.existsSync(cliEntry)) {
      reject(new Error(`未找到客户端运行组件: ${cliEntry}`));
      return;
    }
    const child = spawn(process.execPath, [cliEntry, ...args, '--json'], {
      cwd: path.dirname(cliEntry),
      env: getCliEnvironment(),
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      child.kill('SIGTERM');
      reject(new Error('环境检测超时'));
    }, timeoutMs);
    child.stdout.on('data', (chunk) => { stdout += chunk.toString(); });
    child.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
    child.once('error', (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.once('close', (code) => {
      clearTimeout(timer);
      let payload;
      try {
        payload = JSON.parse(stdout.trim() || '{}');
      } catch {
        reject(new Error(`客户端运行组件返回了无效响应: ${(stderr || stdout).slice(-500)}`));
        return;
      }
      if (code !== 0 || payload.status === 'error') {
        reject(new Error(payload.error || stderr.trim() || `客户端运行组件退出码 ${code}`));
        return;
      }
      resolve(payload);
    });
  });
}

async function getOnboardingState() {
  try {
    const status = await runCli(['status']);
    const profile = status.profile === 'harness' ? 'harness' : null;
    return {
      available: true,
      initialized: Boolean(status.initialized && profile),
      profile,
      extensions: { headless_worker: true },
      home: status.home || getPuddingClawHome(),
    };
  } catch (error) {
    return {
      available: false,
      initialized: false,
      profile: null,
      extensions: { headless_worker: true },
      home: getPuddingClawHome(),
      error: error.message,
    };
  }
}

function inspectProfile(profile) {
  assertHarnessProfile(profile);
  return runCli(['profile', 'inspect', profile], { timeoutMs: 30_000 });
}

function applyProfile(profile) {
  assertHarnessProfile(profile);
  return runCli(['profile', 'apply', profile], { timeoutMs: 30_000 });
}

function assertHarnessProfile(profile) {
  if (profile !== 'harness') {
    throw new Error('Only the Harness runtime profile is available in this desktop build.');
  }
}

async function ensurePreparedRuntime() {
  const inspected = await runCli(['runtime', 'inspect']);
  let installed = inspected;
  if (inspected.status !== 'installed') {
    installed = await runCli(['runtime', 'install', 'bundled'], { timeoutMs: 120_000 });
  }
  const prepared = await runCli(['runtime', 'prepare'], { timeoutMs: 20 * 60_000 });
  return { installed, prepared };
}

function getPreparedBackendCommand() {
  try {
    const home = getPuddingHarnessHome();
    const config = JSON.parse(fs.readFileSync(path.join(home, 'deploy.json'), 'utf8'));
    const active = JSON.parse(fs.readFileSync(path.join(home, 'runtime', 'active.json'), 'utf8'));
    const python = String(config.runtime?.python?.command || '');
    const root = String(active.path || '');
    if (!path.isAbsolute(python) || !fs.existsSync(python) || !path.isAbsolute(root)) return null;
    const ports = getConfiguredPorts();
    return {
      cmd: python,
      args: ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(ports.backendPort)],
      cwd: path.join(root, 'backend'),
    };
  } catch {
    return null;
  }
}

module.exports = {
  applyProfile,
  ensurePreparedRuntime,
  getConfiguredPorts,
  getCliEnvironment,
  getPreparedBackendCommand,
  getOnboardingState,
  getPuddingHarnessHome,
  getPuddingClawHome,
  inspectProfile,
  runCli,
};
