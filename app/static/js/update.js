/* 自动更新提示：页面加载后查询 /update/status，发现新版本时弹窗询问
   - 同步页与设置页（数据与更新管理页）不打扰
   - 关闭「启动时自动检查新版本」后不自动检查、不弹窗
   - 后台检查是启动时另起线程跑的 git ls-remote，可能比页面加载慢，
     因此 checked=false 时按间隔重试若干次，避免整次启动都不提示
   - 检查类网络/接口错误一律静默（console.warn），不弹错误 toast */
"use strict";

const UPDATE_FIRST_DELAY = 800;   // 首查延迟，避免抢首屏
const UPDATE_RETRY_MS = 3000;     // 后台检查未完成时的重试间隔
const UPDATE_MAX_RETRY = 25;      // 覆盖 origin 与两个镜像依次检查的超时窗口

// 更新弹窗：启动提示与设置页的「检查更新」共用同一实现
window.UpdatePrompt = (() => {
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

  return { show };
})();

window.addEventListener("load", () => {
  const page = document.body.dataset.page;
  if (page === "sync" || page === "settings") return;
  setTimeout(() => check(0), UPDATE_FIRST_DELAY);

  async function check(tries) {
    // 用户关闭自动检查后不发起提示；读取失败按默认值继续，避免设置故障吞掉提醒
    let auto = true;
    try {
      const settings = await XM.get("/update/settings");
      auto = settings.auto_check !== false;
    } catch (_) { /* 读取失败时保持默认开启 */ }
    if (!auto) return;
    let st;
    try {
      st = await XM.get("/update/status");
    } catch (e) {
      console.warn("[update] 检查更新失败（静默）", e);
      return;
    }
    if (st && st.checked === false && tries < UPDATE_MAX_RETRY) {
      setTimeout(() => check(tries + 1), UPDATE_RETRY_MS);
      return;
    }
    if (!st || !st.has_update || !st.latest_tag) return;
    // 本次会话已提示过该版本则不再打扰
    if (sessionStorage.getItem("xmims.updatePrompted") === st.latest_tag) return;
    UpdatePrompt.show(st);
  }
});
