"use strict";
/* contextBridge 暴露的窄操作表：不暴露 ipcRenderer、Node 对象、任意 channel 或文件操作。 */
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("xmDesktop", {
  /** 原生文件夹选择窗口；返回所选父目录，取消时为 null。 */
  selectDependencyDirectory: () => ipcRenderer.invoke("xm:select-dependency-directory"),
  /** 检查桌面版更新；force=true 时忽略主进程缓存。 */
  checkUpdate: force => ipcRenderer.invoke("xm:check-update", force === true),
  /** 启动准备作业；只接受版本 tag，下载与解压由主进程在后台完成。 */
  installUpdate: tag => ipcRenderer.invoke("xm:install-update", String(tag || "")),
  /** 当前更新进度快照（只读）。 */
  getUpdateProgress: () => ipcRenderer.invoke("xm:update-progress"),
  /** 订阅进度快照；返回退订函数，回调只接收数据副本。 */
  subscribeUpdateProgress: callback => {
    if (typeof callback !== "function") return () => {};
    const listener = (_event, value) => { callback(value); };
    ipcRenderer.on("xm:update-progress-event", listener);
    return () => ipcRenderer.removeListener("xm:update-progress-event", listener);
  },
  /** 重启到已准备好的版本；不接受版本、路径或命令。 */
  restartToPreparedUpdate: () => ipcRenderer.invoke("xm:restart-update"),
  /** 首屏本地初始化完成确认；启动标识与目标均由主进程确定。 */
  confirmDesktopPageReady: () => ipcRenderer.invoke("xm:confirm-page-ready"),
});
