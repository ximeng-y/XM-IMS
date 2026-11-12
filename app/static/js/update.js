/* 自动更新提示：页面加载后查询更新状态，发现新版本时弹窗询问
   - 同步页与设置页（数据与更新管理页）由页面描述关闭此能力
   - 关闭「启动时自动检查新版本」后不自动检查、不弹窗
   - 源码模式的检查是启动时另起线程跑的 git ls-remote，可能比页面加载慢，
     因此 checked=false 时按间隔重试若干次，避免整次启动都不提示
   - 桌面安装版走 Electron bridge：检查与安装都由桌面外壳完成，源码端点不参与
   - 检查类网络/接口错误一律静默（console.warn），不弹错误 toast */
"use strict";

import { XM } from "xm/api";
import { UI } from "xm/ui";
import { Desktop } from "xm/runtime/desktop";

const UPDATE_FIRST_DELAY = 800;   // 首查延迟，避免抢首屏
const UPDATE_RETRY_MS = 3000;     // 后台检查未完成时的重试间隔
const UPDATE_MAX_RETRY = 25;      // 覆盖 origin 与两个镜像依次检查的超时窗口

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
  dlg.addEventListener("close", () => dlg.remove());

  const showResult = (html) => { body.innerHTML = html; };

  install.onclick = async () => {
    install.disabled = true;
    install.textContent = "更新中…";
    if (Desktop.available()) {
      // 桌面安装版：下载、校验与交接安装程序都在外壳内完成
      const r = await Desktop.installUpdate(st.latest_tag);
      if (r && r.ok) {
        showResult("<div class='result-box'>安装程序将继续完成更新，请按提示关闭本窗口</div>");
        later.remove();
        install.textContent = "知道了";
        install.disabled = false;
        install.onclick = () => dlg.close();
      } else {
        showResult("<div class='result-box error-box'>更新未完成：" +
          UI.esc((r && r.user_message) || "请稍后重试") + "</div>");
        install.disabled = false;
        install.textContent = "立即更新";
      }
      return;
    }
    try {
      const r = await XM.post("/update/install", { tag: st.latest_tag });
      if (r && r.ok) {
        showResult("<div class='result-box'>更新完成，请关闭本页并重新启动程序</div>");
        later.remove();
        install.textContent = "知道了";
        install.disabled = false;
        install.onclick = () => dlg.close();
      } else {
        showResult(
          "<div class='result-box error-box'>更新失败：" +
          UI.esc((r && r.message) || "未知错误") + "</div>");
        install.disabled = false;
        install.textContent = "立即更新";
      }
    } catch (e) {
      console.warn("[update] 安装更新请求失败（静默）", e);
      showResult(
        "<div class='result-box error-box'>更新失败：" +
        UI.esc((e && e.message) || String(e)) + "</div>");
      install.disabled = false;
      install.textContent = "立即更新";
    }
  };

  later.onclick = () => {
    sessionStorage.setItem("xmims.updatePrompted", st.latest_tag);
    dlg.close();
  };
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
  // 新版本存在但没有可安装的安装包时只作状态展示，不弹出无法执行的安装
  if (Desktop.available() && st && st.availability !== "available") {
    if (st.has_update && st.latest_tag &&
        sessionStorage.getItem("xmims.updatePrompted") !== st.latest_tag) {
      sessionStorage.setItem("xmims.updatePrompted", st.latest_tag);
      UI.toast(desktopLabel(st) || "暂时无法获取版本信息", { title: "发现新版本" });
    }
    return;
  }
  if (!st || !st.has_update || !st.latest_tag) return;
  // 本次会话已提示过该版本则不再打扰
  if (sessionStorage.getItem("xmims.updatePrompted") === st.latest_tag) return;
  show(st);
}

// 启动入口按页面描述调用：保持 load 之后的原始延迟；已过 load 时同样只延迟一次
export function startAutoCheck() {
  setTimeout(() => check(0), UPDATE_FIRST_DELAY);
}

// 桌面与源码两种模式的状态展示差异：暂无发布版本 / 该版本缺安装包不算网络故障
function desktopLabel(st) {
  if (st.availability === "no_release") return "暂无已发布版本";
  if (st.availability === "no_asset") return "该版本暂未提供 Windows 安装包";
  return null;
}

export const UpdatePrompt = { show, startAutoCheck, desktopLabel };
