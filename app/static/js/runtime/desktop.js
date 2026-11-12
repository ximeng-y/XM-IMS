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

/** 安装已确认的版本；返回值含 ok 与用户版提示。 */
async function installUpdate(tag) {
  if (!available()) return { ok: false, user_message: "当前不是桌面安装版，无法使用桌面更新" };
  try {
    return await bridge.installUpdate(tag);
  } catch (_) {
    return { ok: false, user_message: "更新没有完成，请稍后重试" };
  }
}

export const Desktop = { available, selectDependencyDirectory, checkUpdate, installUpdate };
