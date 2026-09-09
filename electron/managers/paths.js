const path = require('path');
const { app } = require('electron');

function getRepoRoot() {
  // 开发模式：基于当前文件位置往上两级（electron/managers -> electron -> repo）
  if (!app.isPackaged) {
    return path.resolve(__dirname, '../..');
  }

  // 生产模式：extraResources 会被放到 process.resourcesPath
  return process.resourcesPath;
}

function getBackendDir() {
  return path.join(getRepoRoot(), 'backend');
}

function getFrontendStandaloneDir() {
  if (app.isPackaged) return path.join(process.resourcesPath, 'cli', 'runtime-bundle', 'web');
  return path.join(getRepoRoot(), 'frontend', '.next-build', 'standalone');
}

module.exports = {
  getRepoRoot,
  getBackendDir,
  getFrontendStandaloneDir,
};
