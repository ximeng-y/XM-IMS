"use strict";
/* contextBridge 暴露的窄操作表：只有三个能力，不暴露 ipcRenderer、Node 或任意 IPC。 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("xmDesktop", {
  /** 原生文件夹选择窗口；返回所选父目录，取消时为 null。 */
  selectDependencyDirectory: () => ipcRenderer.invoke("xm:select-dependency-directory"),
  /** 检查桌面版更新；force=true 时忽略主进程缓存。 */
  checkUpdate: force => ipcRenderer.invoke("xm:check-update", force === true),
  /** 安装主进程已确认的最新版本；只接受版本 tag。 */
  installUpdate: tag => ipcRenderer.invoke("xm:install-update", String(tag || "")),
});
