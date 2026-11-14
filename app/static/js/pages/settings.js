/* 设置页：软件更新入口与更新准备状态
   - 「检查更新」每次强制向远端查询，不受后台检查结果或缓存影响
   - 「启动时自动检查新版本」持久化在本机数据目录，关闭后启动不再自动检查
   - 桌面安装版的检查与安装走桌面适配器；下载与安装准备在后台进行，本页展示
     真实进度与待重启状态，重启由用户确认后才发起
   - 首屏先取桌面快照与本地设置，不等待 GitHub Release 请求
   - 设置接口仅限本机使用，通过局域网访问时页面给出提示并停用控件 */
"use strict";

import { XM } from "xm/api";
import { UI, $ } from "xm/ui";
import { Sync } from "xm/sync";
import { UpdatePrompt } from "xm/update";
import { Desktop } from "xm/runtime/desktop";

export const page = {
  shell: true, shopContext: "none", requireSyncReady: false, autoUpdatePrompt: false,
  async init() {
    const versionEl = $("#currentVersion");
    const autoEl = $("#autoCheck");
    const btn = $("#checkNow");
    const result = $("#checkResult");
    const progressMount = $("#updateProgress");
    const restartBox = $("#updateRestart");

    // 版本号统一带 v 前缀展示；缺失值时显示占位符
    const ver = (v) => (v ? (/^v/i.test(String(v)) ? String(v) : "v" + v) : "—");

    function render(html, isError = false) {
      result.hidden = false;
      result.className = "result-box" + (isError ? " error-box" : "");
      result.innerHTML = html;
    }

    function renderLatest(st) {
      render(`<div class="headline"><span class="badge green">已是最新</span> 当前版本 ${UI.esc(ver(st.current_version))}</div>`);
    }

    function renderUpdate(st) {
      render(
        `<div class="headline"><span class="badge warn">发现新版本</span> ${UI.esc(ver(st.latest_version))}</div>` +
        `<div class="hint">当前版本 ${UI.esc(ver(st.current_version))}，更新会在后台准备，完成后可重启应用。</div>`
      );
    }

    function renderError() {
      render(
        `<div class="headline"><span class="badge red">检查未完成</span> 暂时无法获取版本信息</div>` +
        `<div class="hint">请确认网络可用后重试。</div>`,
        true
      );
    }

    // 还没有可安装的发布：既不是网络故障，也不能当作已是最新
    function renderUnavailable(text) {
      render(`<div class="headline"><span class="badge gray">暂无可安装版本</span> ${UI.esc(text)}</div>` +
        `<div class="hint">发布 Windows 更新包后即可在这里检查到。</div>`);
    }

    // 展示缓存中的检查结果；未检查过时只显示当前版本
    function showStatus(st) {
      if (!st) return;
      versionEl.textContent = ver(st.current_version);
      if (!st.checked) return;
      const unavailable = st.availability && UpdatePrompt.desktopLabel(st);
      if (unavailable) renderUnavailable(unavailable);
      else if (st.availability === "error" || st.error) renderError();
      else if (st.has_update && st.latest_tag) renderUpdate(st);
      else renderLatest(st);
    }

    /** 桌面准备状态：进度区 + 待重启操作区。 */
    function showDesktopState(st) {
      if (!st) return;
      if (st.fallback && !st.fallbackShown) {
        st.fallbackShown = true;
        UI.toast("新版启动失败，已恢复到上一个版本", { title: "已恢复" });
      }
      const preparing = !!st.preparing;
      const prepared = st.stage === "prepared";
      progressMount.hidden = !(preparing || prepared || st.last_error);
      if (!progressMount.hidden) {
        UpdatePrompt.mountProgress(progressMount, st);
      }
      const notice = UpdatePrompt.renderNotice(st);
      restartBox.hidden = !(prepared || (notice && !preparing));
      restartBox.innerHTML = "";
      if (notice) {
        const hint = document.createElement("div");
        hint.className = "muted";
        hint.textContent = notice;
        restartBox.appendChild(hint);
      }
      if (prepared && st.can_restart) {
        const now = UI.mkBtn("立即重启");
        const later = UI.mkBtn("稍后重启", "btn-outline");
        now.type = "button";
        later.type = "button";
        restartBox.append(later, now);
        later.onclick = () => { restartBox.hidden = true; };
        now.onclick = async () => {
          now.disabled = true;
          const r = await Desktop.restartToPreparedUpdate();
          if (r && r.ok) now.textContent = "正在重启…";
          else {
            UI.toast((r && r.user_message) || "重启没有完成，请稍后重试", { title: "无法重启" });
            now.disabled = false;
          }
        };
      }
    }

    async function checkDesktop() {
      const st = await Desktop.checkUpdate(true);
      showStatus(st);
      if (st?.has_update && st.latest_tag && st.availability === "available") UpdatePrompt.show(st);
    }

    autoEl.addEventListener("change", async () => {
      autoEl.disabled = true;
      try {
        const saved = await XM.patch("/update/settings", { auto_check: autoEl.checked });
        autoEl.checked = !!saved.auto_check;
        UI.ok(autoEl.checked ? "已开启自动检查更新" : "已关闭自动检查更新");
      } catch (e) {
        autoEl.checked = !autoEl.checked;
        UI.apiFail(e, "保存设置失败");
      } finally {
        autoEl.disabled = false;
      }
    });

    btn.addEventListener("click", () => UI.withBusy(btn, async () => {
      if (Desktop.available()) {
        try {
          await checkDesktop();
        } catch (_) {
          renderError();
        }
        return;
      }
      try {
        const st = await XM.get("/update/status?force=true");
        showStatus(st);
        // 发现新版本时打开与首页相同的更新弹窗，由用户决定是否下载
        if (st.has_update && st.latest_tag) UpdatePrompt.show(st);
      } catch (e) {
        UI.apiFail(e, "检查更新失败");
      }
    }));

    if (!Sync.local) {
      autoEl.disabled = true;
      btn.disabled = true;
      render(`<div class="headline"><span class="badge gray">仅限本机</span> 请在运行本程序的电脑上进行更新设置</div>`);
      return;
    }

    if (Desktop.available()) {
      // 桌面安装版：先接入桌面状态并取快照，再读本地设置；不等待远端 Release
      const unsubscribe = UpdatePrompt.onDesktopState(showDesktopState);
      UpdatePrompt.connectDesktopState();
      window.addEventListener("pagehide", () => unsubscribe(), { once: true });
      try {
        const settings = await XM.get("/update/settings");
        autoEl.checked = !!settings.auto_check;
      } catch (e) {
        UI.apiFail(e, "读取设置失败");
      }
      return;
    }

    try {
      const [settings, st] = await Promise.all([
        XM.get("/update/settings"),
        XM.get("/update/status"),
      ]);
      autoEl.checked = !!settings.auto_check;
      showStatus(st);
    } catch (e) {
      UI.apiFail(e, "读取设置失败");
    }
  },
};
