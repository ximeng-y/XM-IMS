"use strict";
/* 安装布局解析：固定安装根、当前版本运行根、共享数据与更新资料路径。
   其他桌面模块只从这里取路径，不各自拼路径。

   - installRoot 始终是固定安装根（根 xm-ims.exe 启动器所在目录）
   - runtimeRoot 是当前版本载荷目录（versions\<版本>-<构建标识>）；便携版与
     旧平铺布局没有版本子目录，两者相同
   - 资源、Python、后端、版本程序清单归 runtimeRoot；数据、profile、工具、
     下载临时目录与更新资料归 installRoot */
const path = require("node:path");

const runtimeRoot = path.dirname(process.execPath);
// 版本载荷目录带 32 位构建标识；只有这种目录才位于 versions 之下
const SLOT_PATTERN = /^\d+\.\d+\.\d+-[0-9a-f]{32}$/;

function detectInstallRoot() {
  const injected = process.env.XM_INSTALL_ROOT;
  if (injected) return path.resolve(injected);
  const parent = path.dirname(runtimeRoot);
  if (path.basename(parent) === "versions") return path.dirname(parent);
  return runtimeRoot;
}

const installRoot = detectInstallRoot();
const resourcesRoot = process.resourcesPath;
const appRoot = path.join(resourcesRoot, "app");
const backendRoot = path.join(resourcesRoot, "backend");
const pythonRoot = path.join(resourcesRoot, "python");
const iconsRoot = path.join(resourcesRoot, "icons");

const dataRoot = path.join(installRoot, "data");
const tempRoot = path.join(installRoot, ".XMTEMP");
const versionsRoot = path.join(installRoot, "versions");
// 最近一次被接受的更新请求；与源码版共用同一数据根下的相对位置
const updateRecord = path.join(dataRoot, "update", "last-update.json");
// 程序选择资料与本次首次启动交接资料
const desktopUpdateRoot = path.join(dataRoot, "update", "desktop");
const selectionFile = path.join(desktopUpdateRoot, "selection.ini");
const attemptsRoot = path.join(desktopUpdateRoot, "attempts");

function attemptDir(attempt) {
  return path.join(attemptsRoot, String(attempt));
}

module.exports = {
  installRoot,
  runtimeRoot,
  slotPattern: SLOT_PATTERN,
  resourcesRoot,
  appRoot,
  backendRoot,
  pythonRoot,
  iconsRoot,
  versionsRoot,
  dataRoot,
  tempRoot,
  desktopUpdateRoot,
  selectionFile,
  attemptsRoot,
  attemptDir,
  updateRecord,
  pythonExe: path.join(pythonRoot, "python.exe"),
  backendRunner: path.join(backendRoot, "backend_runner.py"),
  updateExtract: path.join(backendRoot, "update_extract.py"),
  programManifest: path.join(runtimeRoot, "installed-program-files.txt"),
  versionManifest: path.join(runtimeRoot, "version-manifest.json"),
  // 固定根启动器与第一跳保留的旧程序入口
  rootLauncher: path.join(installRoot, "xm-ims.exe"),
  legacyExe: path.join(installRoot, "xm-ims-legacy.exe"),
  iconPng: path.join(iconsRoot, "icon.png"),
  iconIco: path.join(iconsRoot, "icon.ico"),
  desktopProfile: path.join(dataRoot, "desktop", "profile"),
  updateStaging: version => path.join(tempRoot, "desktop-update", version),
};
