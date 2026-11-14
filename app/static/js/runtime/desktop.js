/* 桌面适配：把窄 Electron bridge（window.xmDesktop）包装成页面可直接用的能力。
   浏览器里没有 bridge 即源码模式，不模拟成功，也不建立任何业务状态。 */
"use strict";

const bridge = typeof window !== "undefined" ? window.xmDesktop : null;

/** 是否运行在桌面安装版中。 */
function available() {
  return !!(bridge && typeof bridge.checkUpdate === "function");
}

/** 原生文件夹选择窗口；返回父目录或 null（取消 / 不可用）。 */
async function selectDependencyDirectory() {
  if (!bridge || typeof bridge.selectDependencyDirectory !== "function") return null;
  try {
    const folder = await bridge.selectDependencyDirectory();
    return typeof folder === "string" && folder ? folder : null;
  } catch (_) {
    return null;
  }
}

/** 桌面更新状态；不可用时返回 null，让调用方保持源码模式。 */
async function checkUpdate(force = false) {
  if (!available()) return null;
  try {
    return await bridge.checkUpdate(!!force);
  } catch (_) {
    return { checked: true, availability: "error", error: "暂时无法检查更新" };
  }
}

/** 启动后台准备作业；返回值只表示请求是否被接受，完成状态从进度快照读取。 */
async function installUpdate(tag) {
  if (!available()) return { ok: false, user_message: "当前不是桌面安装版，无法使用桌面更新" };
  try {
    return await bridge.installUpdate(tag);
  } catch (_) {
    return { ok: false, user_message: "更新没有完成，请稍后重试" };
  }
}

/** 当前更新进度快照；非桌面环境返回 null。 */
async function getUpdateProgress() {
  if (!bridge || typeof bridge.getUpdateProgress !== "function") return null;
  try {
    return await bridge.getUpdateProgress();
  } catch (_) {
    return null;
  }
}

/** 订阅进度；返回退订函数。非桌面环境返回空退订，不建立状态。 */
function subscribeUpdateProgress(callback) {
  if (!bridge || typeof bridge.subscribeUpdateProgress !== "function") return () => {};
  try {
    const unsubscribe = bridge.subscribeUpdateProgress(callback);
    return typeof unsubscribe === "function" ? unsubscribe : () => {};
  } catch (_) {
    return () => {};
  }
}

/** 重启到已准备版本；不接受版本或路径。 */
async function restartToPreparedUpdate() {
  if (!bridge || typeof bridge.restartToPreparedUpdate !== "function") {
    return { ok: false, user_message: "当前不是桌面安装版，无法重启更新" };
  }
  try {
    return await bridge.restartToPreparedUpdate();
  } catch (_) {
    return { ok: false, user_message: "重启更新没有完成，请稍后重试" };
  }
}

/** 首屏本地初始化完成确认；只在桌面首次启动时有意义。 */
async function confirmPageReady() {
  if (!bridge || typeof bridge.confirmDesktopPageReady !== "function") return false;
  try {
    return await bridge.confirmDesktopPageReady();
  } catch (_) {
    return false;
  }
}

export const Desktop = {
  available, selectDependencyDirectory, checkUpdate, installUpdate,
  getUpdateProgress, subscribeUpdateProgress, restartToPreparedUpdate, confirmPageReady,
};
