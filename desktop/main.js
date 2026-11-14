"use strict";
/* Electron 外壳：生命周期、窗口、托盘与 IPC 装配。

   - 业务数据写在固定安装根 data 下；Electron 自身的 profile 也放这里，不写默认 AppData
   - 单实例锁：第二次启动显示并聚焦已有窗口，不重复启动后端
   - 关闭窗口隐藏到托盘，服务继续运行；托盘「退出」才停止服务
   - 不加载外部页面，不打开浏览器；外部说明链接交给系统浏览器
   - 更新下载与解压都在后台进行，不关闭当前程序；只有用户请求重启才写 pending
     并退出，由固定根启动器应用新版本
   - 首次启动（启动器传入本次标识）只用于确认：服务与首屏就绪后等待启动器
     提交数据指针并放行，期间窗口保持隐藏 */
const { app, BrowserWindow, dialog, ipcMain, Menu, nativeImage, screen, shell, Tray } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const paths = require("./paths");
const { Backend } = require("./backend");
const updater = require("./update");
const store = require("./version-store");
const pkg = require("./package.json");

const APP_NAME = "XM-IMS";
const APP_USER_MODEL_ID = "com.ximeng.xmims";
const CONTROL_POLL_MS = 500;
// 服务开始监听后确定的前端地址；窗口导航与 IPC 校验都以它为唯一来源。
const frontend = { origin: null };
const backend = new Backend();
let mainWindow = null;
let tray = null;
let quitting = false;

// 本次首次启动：只有启动器资料、选择阶段与自身载荷标识都一致时才启用
const attempt = store.readAttempt(process.env.XM_DESKTOP_ATTEMPT);
const ownSlot = store.currentSlot();
const trial = attempt && ownSlot && attempt.toSlot === ownSlot ? attempt : null;
const trialState = { firstScreenReady: false };

function prepareDirectories() {
  for (const folder of [paths.dataRoot, paths.tempRoot, paths.desktopProfile]) {
    fs.mkdirSync(folder, { recursive: true });
  }
}

function createWindow(url, { hidden = false } = {}) {
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
  if (!hidden) mainWindow.once("ready-to-show", () => mainWindow.show());
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

/** 唯一停止流程：退出与重启更新共用，避免重复停止。 */
async function shutdown(code = 0) {
  if (quitting) return;
  quitting = true;
  if (trial) reportTrial("failed", "stopped");
  if (tray) tray.destroy();
  try {
    await backend.stop();
  } finally {
    app.exit(code);
  }
}

function quitApplication() {
  shutdown();
}

/** 首次启动阶段上报：只写本次资料，启动器按同一标识等待。 */
function reportTrial(phase, extra = "") {
  if (!trial) return;
  try {
    if (phase === "copying") {
      const [done, total] = extra.split(" ").map(Number);
      store.writeStatus(trial, phase, { progress: done, total });
      return;
    }
    store.writeStatus(trial, phase);
  } catch (_) {
    // 阶段资料写入失败不阻断启动：启动器按超时处理本次未确认失败
  }
}

/** 首屏本地初始化完成后由 renderer 确认；输入全部由主进程确定。 */
async function confirmFirstScreen() {
  if (!trial || trialState.firstScreenReady) return false;
  trialState.firstScreenReady = true;
  reportTrial("commit_ready");
  return true;
}

/** 轮询本次控制资料，把启动器的命令送入 API 管道。 */
function watchTrial() {
  if (!trial) return;
  const timer = setInterval(async () => {
    if (quitting) {
      clearInterval(timer);
      return;
    }
    const control = store.readControl(trial) || {};
    if (control.attempt !== trial.id) return;
    if (control.command === "commit") {
      if (trialState.firstScreenReady && backend.sendTrial("commit")) {
        clearInterval(timer);
        setTimeout(watchRelease, CONTROL_POLL_MS);
      }
    }
  }, CONTROL_POLL_MS);
  timer.unref?.();
}

function watchRelease() {
  const timer = setInterval(() => {
    if (quitting) {
      clearInterval(timer);
      return;
    }
    const control = store.readControl(trial) || {};
    if (control.attempt !== trial.id || control.command !== "release") return;
    if (backend.sendTrial("release")) {
      clearInterval(timer);
      // 放行后显示正常窗口；窗口与首屏已经就绪，直接展示
      if (mainWindow) mainWindow.show();
      if (tray) tray.setToolTip(APP_NAME);
    }
  }, CONTROL_POLL_MS);
  timer.unref?.();
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
    return updater.prepare(String(tag || ""));
  });

  ipcMain.handle("xm:update-progress", event => (fromOwnPage(event) ? updater.snapshot() : null));

  ipcMain.handle("xm:restart-update", async event => {
    if (!fromOwnPage(event)) return { ok: false, user_message: "请刷新页面后重试" };
    // 重启前确认本机同步状态可读且不 busy；读取失败保留候选并提示重试
    let status = null;
    try {
      const response = await fetch(`${frontend.origin}/api/v1/sync/status`, {
        headers: { "X-XM-Sync": "1" },
      });
      status = response.ok ? await response.json() : null;
    } catch (_) {
      status = null;
    }
    if (!status) return { ok: false, user_message: "暂时无法确认同步状态，请稍后重试" };
    if (status.busy) return { ok: false, user_message: "正在备份或同步，请等当前操作结束后再重启" };
    const marked = updater.requestRestart();
    if (!marked.ok) return marked;
    // 先启动固定根启动器并让它等待本进程退出：选择资料已进入 pending，
    // 启动器会等 /XM_PID 结束后再应用新版本，不会与本进程重叠
    try {
      const child = spawn(paths.rootLauncher, [`/XM_PID=${process.pid}`], {
        detached: true,
        stdio: "ignore",
        cwd: paths.installRoot,
        env: { ...process.env, XM_UPDATE_DIR: paths.installRoot },
      });
      child.unref();
    } catch (_) {
      return { ok: false, user_message: "无法启动更新程序，请稍后重新打开 XM-IMS" };
    }
    setTimeout(() => shutdown(), 300);
    return { ok: true, version: marked.version };
  });

  ipcMain.handle("xm:confirm-page-ready", async event => {
    if (!fromOwnPage(event)) return false;
    return confirmFirstScreen();
  });
}

/** 主进程是权威快照的唯一写入者：发布时推送给本窗口，页面自行按修订号处理。 */
function forwardProgress() {
  updater.subscribe(value => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    mainWindow.webContents.send("xm:update-progress-event", value);
  });
}

async function main() {
  // 单实例锁在 ready 之前取得；第二次启动只聚焦已有窗口。
  // 固定共享 profile 在取得锁之前设置：不同版本仍共用同一个实例锁。
  app.setPath("userData", paths.desktopProfile);
  app.setPath("sessionData", paths.desktopProfile);
  if (!app.requestSingleInstanceLock()) {
    app.quit();
    return;
  }
  app.on("second-instance", showWindow);
  app.setName(APP_NAME);
  app.setAppUserModelId(APP_USER_MODEL_ID);
  prepareDirectories();
  updater.setCurrentVersion(pkg.version);
  // 新进程实际版本已加载：核对上次接受的目标是否已安装到位，并取一次回退提示
  updater.confirmInstalledUpdate();
  updater.takeFallbackNotice();

  await app.whenReady();
  let url;
  try {
    backend.setAttempt(trial ? trial.id : "", (phase, extra) => {
      if (!trial) return;
      if (phase === "local_ready" || phase === "copying") {
        reportTrial(phase, extra.join(" "));
      } else if (phase === "data_committed" || phase === "failed" || phase === "released") {
        reportTrial(phase, extra.join(" "));
      }
    });
    ({ url } = await backend.start());
  } catch (error) {
    if (trial) reportTrial("failed", "start");
    else dialog.showErrorBox(APP_NAME, error && error.message ? error.message : "程序启动失败，请重新打开");
    await backend.stop();
    app.exit(1);
    return;
  }
  frontend.origin = new URL(url).origin;
  if (!trial) createTray();
  // 首次启动先开设置页：无需店铺上下文或同步就绪，首屏确认不等待远端更新检查
  createWindow(trial ? `${frontend.origin}/settings` : url, { hidden: !!trial });
  registerIpc();
  forwardProgress();
  if (trial) {
    watchTrial();
    reportTrial("booting");
  }
  app.on("window-all-closed", () => { /* 关窗只隐藏到托盘，退出由托盘决定 */ });
  app.on("before-quit", event => {
    if (quitting) return;
    event.preventDefault();
    quitApplication();
  });
}

main().catch(async error => {
  if (trial) reportTrial("failed", "start");
  else dialog.showErrorBox(APP_NAME, "程序启动失败，请重新打开");
  await backend.stop().catch(() => {});
  app.exit(1);
});
