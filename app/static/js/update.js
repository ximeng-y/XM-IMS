/* 自动更新提示与桌面更新进度：页面加载后查询更新状态，发现新版本时弹窗询问。

   - 设置页与同步页由页面描述关闭自动检查；关闭「启动时自动检查新版本」后
     不自动检查、不弹窗
   - 桌面安装版走 Electron bridge：检查、下载与解压都由外壳完成，页面只读进度快照
   - 同一页面只建立一次桌面状态连接，向弹窗与页面进度区分发只读状态；整页导航后
     重新建立并先读快照，renderer 内存不是唯一状态来源
   - 准备完成后提示“新版已准备就绪”，只有用户点击才请求重启
   - 源码模式的检查与安装保持原行为，不接入进度展示 */
"use strict";

import { XM } from "xm/api";
import { UI } from "xm/ui";
import { Desktop } from "xm/runtime/desktop";
import { createProgressView } from "xm/components/progress-view";

const UPDATE_FIRST_DELAY = 800;   // 首查延迟，避免抢首屏
const UPDATE_RETRY_MS = 3000;     // 后台检查未完成时的重试间隔
const UPDATE_MAX_RETRY = 25;      // 覆盖 origin 与两个镜像依次检查的超时窗口

// 本页面共享的桌面更新状态连接
const desktopState = {
  started: false,
  snapshot: null,
  listeners: new Set(),
};

function deliver(snapshot) {
  if (!snapshot) return;
  if (desktopState.snapshot && snapshot.revision < desktopState.snapshot.revision) return;
  desktopState.snapshot = snapshot;
  for (const listener of [...desktopState.listeners]) {
    try { listener(snapshot); } catch (_) { /* 单个监听失败不影响其他 */ }
  }
  refreshHost();
}

/** 建立本页面的桌面状态连接：先注册监听再读快照，按修订号处理旧快照。 */
async function connectDesktopState() {
  if (desktopState.started || !Desktop.available()) return;
  desktopState.started = true;
  Desktop.subscribeUpdateProgress(deliver);
  deliver(await Desktop.getUpdateProgress());
}

function onDesktopState(listener) {
  desktopState.listeners.add(listener);
  if (desktopState.snapshot) listener(desktopState.snapshot);
  return () => desktopState.listeners.delete(listener);
}

/** 桌面快照 → 进度组件的展示数据；下载完成保留 100%，安装单独推进。 */
function progressViewState(st) {
  const preparing = !!(st && st.preparing);
  const installing = !!st && st.stage === "installing";
  return {
    showDownload: preparing,
    showInstall: preparing || (!!st && st.stage === "prepared"),
    downloadDone: st ? st.download_done || 0 : 0,
    downloadTotal: st ? st.download_total : null,
    installDone: st ? st.install_done || 0 : 0,
    installTotal: st ? st.install_total : null,
    installComplete: !!st && st.stage === "prepared",
    downloadLabel: installing ? "下载完成" : "下载更新",
  };
}

/** 准备完成、准备中、失败与回退提示；业务文字只在这里决定。
    回退提示只在没有新作业状态时展示，避免覆盖当前的准备/就绪/失败文案。 */
function renderNotice(st) {
  if (!st) return "";
  if (st.stage === "prepared") return "新版已准备就绪，可重启应用更新";
  if (st.preparing) return "正在准备更新，可以继续使用";
  if (st.last_error) return "更新准备失败：" + st.last_error;
  if (st.fallback) return "新版启动失败，已恢复到上一个版本";
  return "";
}

// 页面级进度区域：优先挂到页面提供的 #updateProgress，否则使用简洁容器
const pageHost = { node: null, view: null, text: null, button: null };

function hostNode() {
  const mounted = UI.$("#updateProgress");
  if (mounted) return mounted;
  if (!pageHost.node || !pageHost.node.isConnected) {
    pageHost.node = document.createElement("div");
    pageHost.node.className = "update-progress-host";
    const anchor = UI.$("main .content") || document.body;
    anchor.appendChild(pageHost.node);
  }
  return pageHost.node;
}

function refreshHost() {
  const st = desktopState.snapshot;
  if (!st || !Desktop.available()) return;
  // 设置页有自己的进度挂载点，由该页自行渲染，避免重复区域
  if (UI.$("#updateProgress")) return;
  const node = hostNode();
  if (!pageHost.view || !node.contains(pageHost.view.root)) {
    pageHost.view = createProgressView();
    node.appendChild(pageHost.view.root);
    pageHost.text = document.createElement("div");
    pageHost.text.className = "progress-view-note";
    node.appendChild(pageHost.text);
  }
  const notice = renderNotice(st);
  const showProgress = st.preparing || st.stage === "prepared" || !!st.last_error;
  pageHost.view.root.hidden = !showProgress;
  pageHost.view.update(progressViewState(st));
  pageHost.text.hidden = !notice;
  pageHost.text.textContent = notice;
  refreshHostButton(node, st);
}

function refreshHostButton(node, st) {
  if (!st.can_restart) {
    if (pageHost.button) { pageHost.button.remove(); pageHost.button = null; }
    return;
  }
  if (pageHost.button && node.contains(pageHost.button)) return;
  const button = UI.mkBtn("立即重启", "btn-sm");
  button.type = "button";
  button.onclick = async () => {
    button.disabled = true;
    const r = await Desktop.restartToPreparedUpdate();
    if (!r || !r.ok) {
      UI.toast((r && r.user_message) || "重启没有完成，请稍后重试", { title: "无法重启" });
      button.disabled = false;
    }
  };
  pageHost.button = button;
  node.appendChild(button);
}

// 更新弹窗：启动提示与设置页的「检查更新」共用同一实现
function show(st) {
  const wrap = document.createElement("div");
  wrap.innerHTML =
    "<ul class='update-info'>" +
    "<li><span>当前版本</span><b>" + UI.esc(st.current_version) + "</b></li>" +
    "<li><span>最新版本</span><b>" + UI.esc(st.latest_version) + "</b></li>" +
    "</ul>";
  const { dlg, body, foot } = UI.baseDialog("发现新版本", wrap);
  const later = UI.mkBtn("稍后", "btn-outline");
  const install = UI.mkBtn("立即更新");
  later.type = "button";
  install.type = "button";
  foot.append(later, install);
  dlg.addEventListener("close", () => closeDialog());

  const parts = { progress: null, text: null, restart: null, postpone: null };
  let unsubscribe = null;
  function closeDialog() {
    if (unsubscribe) unsubscribe();
    unsubscribe = null;
    dlg.remove();
  }

  function renderResult(text, isError = false) {
    if (!parts.text) {
      parts.text = document.createElement("div");
      body.appendChild(parts.text);
    }
    parts.text.className = "result-box" + (isError ? " error-box" : "");
    parts.text.textContent = text;
    parts.text.hidden = !text;
  }

  function renderPrepared() {
    install.hidden = true;
    later.hidden = true;
    if (parts.restart) return;
    parts.postpone = UI.mkBtn("稍后重启", "btn-outline");
    parts.restart = UI.mkBtn("立即重启");
    parts.postpone.type = "button";
    parts.restart.type = "button";
    foot.append(parts.postpone, parts.restart);
    parts.postpone.onclick = () => dlg.close();
    parts.restart.onclick = async () => {
      parts.restart.disabled = true;
      const r = await Desktop.restartToPreparedUpdate();
      if (r && r.ok) parts.restart.textContent = "正在重启…";
      else {
        UI.toast((r && r.user_message) || "重启没有完成，请稍后重试", { title: "无法重启" });
        parts.restart.disabled = false;
      }
    };
  }

  function renderDesktop(snapshot) {
    if (!snapshot) return;
    if (!parts.progress) {
      parts.progress = createProgressView();
      body.appendChild(parts.progress.root);
    }
    const showProgress = snapshot.preparing || snapshot.stage === "prepared";
    parts.progress.root.hidden = !showProgress;
    parts.progress.update(progressViewState(snapshot));
    if (snapshot.stage === "prepared") renderPrepared();
    else {
      renderResult(renderNotice(snapshot), !!snapshot.last_error);
      if (snapshot.preparing) {
        install.disabled = true;
        install.textContent = "更新中…";
      } else if (snapshot.last_error) {
        install.disabled = false;
        install.textContent = "重试";
      }
    }
  }

  install.onclick = async () => {
    install.disabled = true;
    install.textContent = "更新中…";
    if (Desktop.available()) {
      // 桌面安装版：后台准备作业，完成状态由快照推进；弹窗可关闭并后台继续
      const r = await Desktop.installUpdate(st.latest_tag);
      if (!r || !r.ok) {
        install.disabled = false;
        install.textContent = "立即更新";
        renderResult("更新未完成：" + ((r && r.user_message) || "请稍后重试"), true);
      }
      return;
    }
    try {
      const r = await XM.post("/update/install", { tag: st.latest_tag });
      if (r && r.ok) {
        install.hidden = true;
        later.hidden = true;
        renderResult("更新完成，请关闭本页并重新启动程序");
      } else {
        renderResult("更新失败：" + ((r && r.message) || "未知错误"), true);
        install.disabled = false;
        install.textContent = "立即更新";
      }
    } catch (e) {
      console.warn("[update] 安装更新请求失败（静默）", e);
      renderResult("更新失败：" + ((e && e.message) || String(e)), true);
      install.disabled = false;
      install.textContent = "立即更新";
    }
  };

  later.onclick = () => {
    sessionStorage.setItem("xmims.updatePrompted", st.latest_tag);
    dlg.close();
  };

  if (Desktop.available()) unsubscribe = onDesktopState(renderDesktop);
  dlg.showModal();
}

async function check(tries) {
  // 用户关闭自动检查后不发起提示；读取失败按默认值继续，避免设置故障吞掉提醒
  let auto = true;
  try {
    const settings = await XM.get("/update/settings");
    auto = settings.auto_check !== false;
  } catch (_) { /* 读取失败时保持默认开启 */ }
  if (!auto) return;
  let st;
  if (Desktop.available()) {
    // 自动检查走桌面外壳的缓存，避免每次页面导航都消耗匿名接口配额
    await connectDesktopState();
    st = await Desktop.checkUpdate(false);
  } else {
    try {
      st = await XM.get("/update/status");
    } catch (e) {
      console.warn("[update] 检查更新失败（静默）", e);
      return;
    }
  }
  if (st && st.checked === false && tries < UPDATE_MAX_RETRY) {
    setTimeout(() => check(tries + 1), UPDATE_RETRY_MS);
    return;
  }
  // 新版本存在但没有可安装的更新包时只作状态展示，不弹出无法执行的安装
  // 去重键与可安装提示分开：缺包提醒不能占掉之后可安装提示的首次机会
  if (Desktop.available() && st && st.availability !== "available") {
    if (st.has_update && st.latest_tag &&
        sessionStorage.getItem("xmims.updateNoticed") !== st.latest_tag) {
      sessionStorage.setItem("xmims.updateNoticed", st.latest_tag);
      UI.toast(desktopLabel(st) || "暂时无法获取版本信息", { title: "发现新版本" });
    }
    return;
  }
  if (!st || !st.has_update || !st.latest_tag) return;
  // 已有待应用候选时只提示现有目标，不再弹出新的安装询问
  const snapshot = desktopState.snapshot;
  if (snapshot && (snapshot.can_restart || snapshot.preparing)) return;
  // 本次会话已提示过该版本则不再打扰
  if (sessionStorage.getItem("xmims.updatePrompted") === st.latest_tag) return;
  show(st);
}

// 启动入口按页面描述调用：保持 load 之后的原始延迟；已过 load 时同样只延迟一次
export function startAutoCheck() {
  // 先接上桌面状态并读快照：整页导航后立即恢复准备进度，不等远端检查
  if (Desktop.available()) connectDesktopState();
  setTimeout(() => check(0), UPDATE_FIRST_DELAY);
}

// 桌面与源码两种模式的状态展示差异：暂无发布版本 / 该版本缺安装包不算网络故障
function desktopLabel(st) {
  if (st.availability === "no_release") return "暂无已发布版本";
  if (st.availability === "no_asset") return "该版本暂未提供 Windows 安装包";
  return null;
}

export const UpdatePrompt = {
  show, startAutoCheck, desktopLabel, connectDesktopState, onDesktopState,
  progressViewState, renderNotice, mountProgress,
};

/** 在指定容器里渲染（或更新）进度区；页面只负责提供容器。 */
const mountedViews = new WeakMap();
function mountProgress(container, st) {
  let view = mountedViews.get(container);
  if (!view) {
    view = createProgressView();
    container.innerHTML = "";
    container.appendChild(view.root);
    mountedViews.set(container, view);
  }
  view.update(progressViewState(st));
}
