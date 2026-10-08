/* 自动更新提示：页面加载后延迟检查 /update/status，发现新版本时弹窗询问
   - 同步页（数据安全操作）不打扰
   - 检查类网络/接口错误一律静默（console.warn），不弹错误 toast */
"use strict";

window.addEventListener("load", () => {
  setTimeout(async () => {
    if (document.body.dataset.page === "sync") return;
    let st;
    try {
      st = await XM.get("/update/status");
    } catch (e) {
      console.warn("[update] 检查更新失败（静默）", e);
      return;
    }
    if (!st || !st.has_update || !st.latest_tag) return;
    // 本次会话已提示过该版本则不再打扰
    if (sessionStorage.getItem("xmims.updatePrompted") === st.latest_tag) return;

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
  }, 800);
});
