const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('path');
const { spawn } = require('child_process');
const backendManager = require('./managers/backend');
const cliManager = require('./managers/cli');
const { getFrontendStandaloneDir } = require('./managers/paths');
const { assertPortAvailable } = require('./managers/port-guard');

let mainWindow;
let frontendProcess = null;

function getFrontendUrl() {
  const port = getFrontendPort();
  return `http://localhost:${port}/app-control`;
}

function getFrontendPort() {
  if (!app.isPackaged) return Number(process.env.FRONTEND_DEV_PORT || '3000');
  return cliManager.getConfiguredPorts().frontendPort;
}

async function startFrontendServer() {
  const isDev = !app.isPackaged;
  if (isDev) {
    // 开发模式由外部脚本启动 frontend dev server
    return true;
  }

  const standaloneDir = getFrontendStandaloneDir();
  const serverJs = path.join(standaloneDir, 'server.js');
  const fs = require('fs');
  const frontendPort = getFrontendPort();
  const backendPort = cliManager.getConfiguredPorts().backendPort;

  if (!fs.existsSync(serverJs)) {
    throw new Error(`未找到 frontend standalone server: ${serverJs}`);
  }

  // A listener may belong to another Harness instance or an unrelated app.
  // Never kill by port; fail closed unless this process owns the listener.
  await assertPortAvailable(frontendPort, 'frontend');

  frontendProcess = spawn(process.execPath, ['server.js'], {
    cwd: standaloneDir,
    env: {
      ...process.env,
      ELECTRON_RUN_AS_NODE: '1',
      PORT: String(frontendPort),
      BACKEND_INTERNAL_URL: `http://localhost:${backendPort}`,
    },
    stdio: 'pipe',
  });

  let frontendSpawnError = null;
  frontendProcess.once('error', (error) => {
    frontendSpawnError = error;
    frontendProcess = null;
  });

  frontendProcess.stdout.on('data', (data) => {
    console.log('[frontend stdout]', data.toString().trim());
  });

  frontendProcess.stderr.on('data', (data) => {
    console.error('[frontend stderr]', data.toString().trim());
  });

  // 等待 frontend ready
  const http = require('http');
  for (let i = 0; i < 30; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    if (frontendSpawnError) {
      throw new Error(`frontend server 启动失败: ${frontendSpawnError.message}`);
    }
    const ready = await new Promise((resolve) => {
      const req = http.get(`http://localhost:${frontendPort}/app-control`, { timeout: 2000 }, (res) => {
        resolve(res.statusCode === 200);
      });
      req.on('error', () => resolve(false));
      req.on('timeout', () => { req.destroy(); resolve(false); });
    });
    if (ready) return true;
  }

  throw new Error('frontend server 未在 30 秒内就绪');
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 960,
    titleBarStyle: 'default',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(getFrontendUrl());

  const isDev = !app.isPackaged;
  if (isDev) {
    mainWindow.webContents.openDevTools();
  }
}

app.whenReady().then(async () => {
  try {
    if (process.platform === 'darwin' && app.dock) {
      app.dock.setIcon(path.join(__dirname, 'assets', 'icon.png'));
    }

    await startFrontendServer();
    createWindow();

    // 首次打开先由前端完成模式选择。已有 CLI 配置时才自动启动 Backend。
    const onboarding = await cliManager.getOnboardingState();
    if (onboarding.initialized) {
      const backendStatus = backendManager.getStatus();
      if (backendStatus.status !== 'running') {
        console.log('[auto] backend 未运行，开始自动启动...');
        backendManager.startBackend().catch((err) => {
          console.error('自动启动 backend 失败:', err);
        });
      }
    }
  } catch (err) {
    console.error('启动 frontend server 失败:', err);
    dialog.showErrorBox('启动失败', err.message);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    backendManager.stopBackend();
    if (frontendProcess) frontendProcess.kill();
    app.quit();
  }
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('before-quit', async () => {
  await backendManager.stopBackend();
  if (frontendProcess) frontendProcess.kill();
});

// IPC: 选择项目文件夹
ipcMain.handle('select-project-folder', async () => {
  const result = await dialog.showOpenDialog(mainWindow, {
    properties: ['openDirectory'],
    title: '选择项目文件夹',
    buttonLabel: '选择',
  });

  if (result.canceled || result.filePaths.length === 0) {
    return null;
  }

  return result.filePaths[0];
});

// IPC: Backend 管理
ipcMain.handle('start-backend', async () => {
  const result = await backendManager.startBackend();
  if (mainWindow) {
    mainWindow.webContents.send('backend-status-change', backendManager.getStatus());
  }
  return result;
});

ipcMain.handle('stop-backend', async () => {
  const result = await backendManager.stopBackend();
  if (mainWindow) {
    mainWindow.webContents.send('backend-status-change', backendManager.getStatus());
  }
  return result;
});

ipcMain.handle('get-backend-status', async () => {
  return backendManager.getStatus();
});

// IPC: 首次启动模式选择与 CLI 依赖探测
ipcMain.handle('get-onboarding-state', async () => cliManager.getOnboardingState());

ipcMain.handle('inspect-onboarding-profile', async (_event, profile) => {
  return cliManager.inspectProfile(profile);
});

ipcMain.handle('apply-onboarding-profile', async (_event, profile) => {
  await backendManager.stopBackend();
  const result = await cliManager.applyProfile(profile);
  const backend = await backendManager.startBackend();
  if (mainWindow) {
    mainWindow.webContents.send('backend-status-change', backendManager.getStatus());
  }
  return { ...result, backend };
});

// 定时刷新状态
setInterval(async () => {
  if (!mainWindow) return;

  try {
    const backendStatus = backendManager.getStatus();
    mainWindow.webContents.send('backend-status-change', backendStatus);
  } catch (err) {
    console.error('backend status update error:', err);
  }

}, 3000);
