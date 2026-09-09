const { spawn } = require('child_process');
const http = require('http');
const { app } = require('electron');
const { getBackendDir } = require('./paths');
const cliManager = require('./cli');
const { isPortOccupied } = require('./port-guard');

const BACKEND_DIR = getBackendDir();

let backendProcess = null;
let backendStatus = 'stopped'; // stopped | starting | running | error
let backendError = null;

function getBackendPort() {
  return cliManager.getConfiguredPorts().backendPort;
}

function getBackendCommand(port) {
  const isDev = !app.isPackaged;

  if (isDev) {
    return {
      cmd: 'uv',
      args: [
        'run', '--locked', '--group', 'dev',
        'python', '-m', 'uvicorn', 'app:app',
        '--host', '127.0.0.1',
        '--port', String(port),
        '--reload',
        // Provider registry and other shared backend modules live at the
        // backend root, not only beneath API/graph/tool folders.
        '--reload-dir', '.',
        '--reload-exclude', '.venv',
        '--reload-exclude', '__pycache__',
        '--reload-include', '*.py',
        '--log-level', 'info',
      ],
      cwd: BACKEND_DIR,
    };
  }

  // 生产模式只使用 CLI 在用户目录准备的受管理 Runtime，不修改签名后的 App Resources。
  const prepared = cliManager.getPreparedBackendCommand();
  if (!prepared) throw new Error('客户端运行环境尚未准备完成');
  return prepared;
}

async function checkBackendStatus(port = getBackendPort()) {
  const backendUrl = `http://127.0.0.1:${port}/api/capabilities`;
  return new Promise((resolve) => {
    const req = http.get(backendUrl, { timeout: 2000 }, (res) => {
      resolve(res.statusCode === 200 ? 'running' : 'error');
    });
    req.on('error', () => resolve('stopped'));
    req.on('timeout', () => {
      req.destroy();
      resolve('stopped');
    });
  });
}

async function startBackend() {
  const port = getBackendPort();
  const current = await checkBackendStatus(port);
  if (current === 'running') {
    if (!backendProcess) {
      backendStatus = 'error';
      backendError = `backend port ${port} is already in use by an unmanaged process; refusing to reuse it`;
      return { status: 'error', message: backendError };
    }
    backendStatus = 'running';
    return { status: 'running', message: 'backend 已经在运行' };
  }

  // A non-Harness listener may not expose /api/capabilities. Check the port
  // itself before spawning so uvicorn cannot race with or mask that conflict.
  if (await isPortOccupied(port)) {
    backendStatus = 'error';
    backendError = `backend port ${port} is already in use by an unmanaged process; refusing to terminate or reuse it`;
    return { status: 'error', message: backendError };
  }

  if (backendProcess) {
    return { status: backendStatus, message: 'backend 正在启动中' };
  }

  backendStatus = 'starting';
  backendError = null;

  try {
    const isDev = !app.isPackaged;
    if (!isDev) await cliManager.ensurePreparedRuntime();

    const { cmd, args, cwd } = getBackendCommand(port);

    const environment = {
      ...process.env,
      // Backend credentials and Provider Registry are user-local, never
      // stored in the packaged repository. Electron owns the cross-platform
      // userData path (including Windows APPDATA handling).
      PUDDINGHARNESS_HOME: cliManager.getPuddingHarnessHome(),
      VIRTUAL_ENV: '',
    };
    delete environment.PUDDINGCLAW_HOME;

    backendProcess = spawn(cmd, args, {
      cwd,
      env: environment,
      stdio: 'pipe',
    });

    backendProcess.stdout.on('data', (data) => {
      console.log('[backend stdout]', data.toString().trim());
    });

    backendProcess.stderr.on('data', (data) => {
      console.error('[backend stderr]', data.toString().trim());
    });

    backendProcess.on('error', (err) => {
      backendStatus = 'error';
      backendError = err.message;
      backendProcess = null;
    });

    backendProcess.on('exit', (code) => {
      backendStatus = code === 0 ? 'stopped' : 'error';
      if (code !== 0 && code !== null) {
        backendError = `backend 进程退出，退出码: ${code}`;
      }
      backendProcess = null;
    });

    // 等待 backend ready
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 1000));
      const status = await checkBackendStatus(port);
      if (status === 'running') {
        backendStatus = 'running';
        return { status: 'running', message: 'backend 启动成功' };
      }
    }

    backendStatus = 'error';
    backendError = 'backend 未在 30 秒内就绪';
    return { status: 'error', message: backendError };
  } catch (err) {
    backendStatus = 'error';
    backendError = err.message;
    return { status: 'error', message: err.message };
  }
}

async function stopBackend() {
  if (!backendProcess) {
    backendStatus = 'stopped';
    return { status: 'stopped', message: 'backend 未运行' };
  }

  backendProcess.kill('SIGTERM');

  setTimeout(() => {
    if (backendProcess && !backendProcess.killed) {
      backendProcess.kill('SIGKILL');
    }
  }, 5000);

  backendProcess = null;
  backendStatus = 'stopped';
  backendError = null;
  return { status: 'stopped', message: 'backend 已停止' };
}

function getStatus() {
  return {
    status: backendStatus,
    error: backendError,
    url: `http://127.0.0.1:${getBackendPort()}`,
  };
}

module.exports = {
  startBackend,
  stopBackend,
  checkBackendStatus,
  getBackendCommand,
  getStatus,
};
