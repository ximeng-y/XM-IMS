"use strict";
/* 安装布局解析：安装根（xm-ims.exe 所在目录）、资源根、Python、数据与临时路径。
   其他桌面模块只从这里取路径，不各自拼路径。 */
const path = require("node:path");

const installRoot = path.dirname(process.execPath);
const resourcesRoot = process.resourcesPath;
const appRoot = path.join(resourcesRoot, "app");
const backendRoot = path.join(resourcesRoot, "backend");
const pythonRoot = path.join(resourcesRoot, "python");
const iconsRoot = path.join(resourcesRoot, "icons");

const dataRoot = path.join(installRoot, "data");
const tempRoot = path.join(installRoot, ".XMTEMP");
// 最近一次被接受的更新请求；与源码版共用同一数据根下的相对位置
const updateRecord = path.join(dataRoot, "update", "last-update.json");

module.exports = {
  installRoot,
  resourcesRoot,
  appRoot,
  backendRoot,
  pythonRoot,
  iconsRoot,
  dataRoot,
  tempRoot,
  updateRecord,
  pythonExe: path.join(pythonRoot, "python.exe"),
  backendRunner: path.join(backendRoot, "backend_runner.py"),
  programManifest: path.join(installRoot, "installed-program-files.txt"),
  iconPng: path.join(iconsRoot, "icon.png"),
  iconIco: path.join(iconsRoot, "icon.ico"),
  desktopProfile: path.join(dataRoot, "desktop", "profile"),
  updateStaging: version => path.join(tempRoot, "desktop-update", version),
};
