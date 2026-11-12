"use strict";
/* Electron 外壳：生命周期、窗口、托盘与 IPC 装配。

   - 业务数据写在安装根 data 下；Electron 自身的 profile 也放这里，不写默认 AppData
   - 单实例锁：第二次启动显示并聚焦已有窗口，不重复启动后端
   - 关闭窗口隐藏到托盘，服务继续运行；托盘「退出」才停止服务
   - 不加载外部页面，不打开浏览器；外部说明链接交给系统浏览器 */
const { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, screen, shell, Tray } = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const paths = require("./paths");
const { Backend } = require("./backend");
const updater = require("./update");
const pkg = require("./package.json");

const APP_NAME = "XM-IMS";
const APP_USER_MODEL_ID = "com.ximeng.xmims";
// 服务开始监听后确定的前端地址；窗口导航与 IPC 校验都以它为唯一来源。
const frontend = { origin: null };
const backend = new Backend();
let mainWindow = null;
let tray = null;
let quitting = false;
let updateInFlight = false;

function prepareDirectories() {
  for (const folder of [paths.dataRoot, paths.tempRoot, paths.desktopProfile]) {
    fs.mkdirSync(folder, { recursive: true });
  }
}

function createWindow(url) {
  const workArea = screen.getPrimaryDisplay().workAreaSize;
  mainWindow = new BrowserWindow({
    width: Math.min(1280, workArea.width),
    height: Math.min(800, workArea.height),
    minWidth: 960,
    minHeight: 640,
    show: false,
    title: APP_NAME,
    icon: paths.iconIco,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
    },
  });
  mainWindow.removeMenu();
  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.on("close", event => {
    if (quitting) return;
    event.preventDefault();
    mainWindow.hide();
  });
  mainWindow.on("closed", () => { mainWindow = null; });

  const origin = new URL(url).origin;
  const sameOrigin = target => {
    try { return new URL(target).origin === origin; } catch (_) { return false; }
  };
  // 窗口只留在本次服务页面；外部说明链接交给系统浏览器，不创建外站窗口。
  mainWindow.webContents.on("will-navigate", (event, target) => {
    if (!sameOrigin(target)) {
      event.preventDefault();
      if (/^https?:/i.test(target)) shell.openExternal(target);
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:/i.test(target)) shell.openExternal(target);
    return { action: "deny" };
  });
  mainWindow.loadURL(url);
  return mainWindow;
}

function showWindow() {
  if (!mainWindow) return;
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  tray = new Tray(nativeImage.createFromPath(paths.iconIco));
  tray.setToolTip(APP_NAME);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "显示 XM-IMS", click: showWindow },
    { type: "separator" },
    { label: "退出", click: () => quitApplication() },
  ]));
  tray.on("click", showWindow);
}

/** 唯一停止流程：退出与更新安装共用，避免重复停止。 */
async function shutdown() {
  if (quitting) return;
  quitting = true;
  if (tray) tray.destroy();
  try {
    await backend.stop();
  } finally {
    app.exit(0);
  }
}

function quitApplication() {
  shutdown();
}

function registerIpc() {
  // 只接受本窗口当前本机页面主框架发起的调用。
  const fromOwnPage = event => {
    const frame = event.senderFrame;
    if (!frame || frame !== event.sender.mainFrame) return false;
    try { return new URL(frame.url).origin === frontend.origin; } catch (_) { return false; }
  };

  ipcMain.handle("xm:select-dependency-directory", async event => {
    if (!fromOwnPage(event)) return null;
    const result = await dialog.showOpenDialog(mainWindow, {
      title: "选择依赖工具的安装文件夹",
      buttonLabel: "使用这个文件夹",
      properties: ["openDirectory", "createDirectory"],
    });
    return result.canceled || !result.filePaths.length ? null : result.filePaths[0];
  });

  ipcMain.handle("xm:check-update", async (event, force) => {
    if (!fromOwnPage(event)) return null;
    return updater.check(force === true);
  });

  ipcMain.handle("xm:install-update", async (event, tag) => {
    if (!fromOwnPage(event)) return { ok: false, user_message: "请刷新页面后重试" };
    if (updateInFlight) return { ok: false, user_message: "更新正在准备中，请稍候" };
    updateInFlight = true;
    try {
      // 安装是用户明确动作，强制取一次最新状态，避免用缓存里可能已下线的 tag
      const state = await updater.check(true);
      if (!state || state.availability !== "available" || state.latest_tag !== tag) {
        return { ok: false, user_message: "暂时无法确认这个版本，请重新检查更新" };
      }
      const downloaded = await updater.download();
      if (!downloaded.ok) return downloaded;
      // 同步状态只读取现有公开接口；忙时先让用户完成当前备份 / 同步。
      let status = null;
      try {
        const response = await fetch(`${frontend.origin}/api/v1/sync/status`, {
          headers: { "X-XM-Sync": "1" },
        });
        status = response.ok ? await response.json() : null;
      } catch (_) {
        status = null;
      }
      if (status && status.busy) {
        return { ok: false, user_message: "正在备份或同步，请等当前操作结束后再更新" };
      }
      await backend.stop();
      updater.launchInstaller(downloaded.path);
      quitting = true;
      setTimeout(() => app.exit(0), 200);
      return { ok: true, user_message: "安装程序将继续完成更新" };
    } finally {
      updateInFlight = false;
    }
  });
}

async function main() {
  // 单实例锁在 ready 之前取得；第二次启动只聚焦已有窗口。
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  app.on("second-instance", showWindow);
  app.setName(APP_NAME);
  app.setAppUserModelId(APP_USER_MODEL_ID);
  app.setPath("userData", paths.desktopProfile);
  app.setPath("sessionData", paths.desktopProfile);
  prepareDirectories();
  updater.setCurrentVersion(pkg.version);

  await app.whenReady();
  let url;
  try {
    ({ url } = await backend.start());
  } catch (error) {
    dialog.showErrorBox(APP_NAME, error && error.message ? error.message : "程序启动失败，请重新打开");
    await backend.stop();
    app.exit(1);
    return;
  }
  frontend.origin = new URL(url).origin;
  createTray();
  createWindow(url);
  registerIpc();
  app.on("window-all-closed", () => { /* 关窗只隐藏到托盘，退出由托盘决定 */ });
  app.on("before-quit", event => {
    if (quitting) return;
    event.preventDefault();
    quitApplication();
  });
}

main().catch(async error => {
  dialog.showErrorBox(APP_NAME, "程序启动失败，请重新打开");
  await backend.stop().catch(() => {});
  app.exit(1);
});
