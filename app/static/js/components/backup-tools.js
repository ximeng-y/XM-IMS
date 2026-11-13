/* 备份工具准备：检测、安装与重新检测 Git。

   只负责工具本身与环境快照：不访问店铺、自动开关或仓库草稿，也不直接调用
   本地备份端点；需要附带动作时由调用方通过 onReady 注入并等待完成。
   安装弹窗复用 DependencyInstall，路径判断仍由后端给出。 */
"use strict";

import { UI } from "xm/ui";
import { DependencyInstall } from "xm/components/dependency-install";

const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
};

function errorBox(error, fallback) {
  const box = node("div", undefined, "sync-onboarding-error");
  box.append(node("strong", error?.user_message || fallback));
  if (error?.user_hint) box.append(node("p", error.user_hint));
  return box;
}

/**
 * 准备备份工具。
 * @param {object} options
 * @param {(method: string, endpoint: string, body?: object) => Promise<object>} options.request
 * @param {boolean} [options.allowSkip]         是否允许「暂不备份，继续使用」
 * @param {string} [options.confirmLabel]       就绪后确认按钮文案
 * @param {boolean} [options.confirmWhenReady]  已有 Git 时仍展示状态并确认一次
 * @param {(env: object) => Promise<void>} [options.onReady] 确认后的动作，会被等待
 * @returns {Promise<{status: "ready"|"skipped"|"cancelled", environment: object|null}>}
 */
function open({ request, allowSkip = false, confirmLabel = "继续", confirmWhenReady = false, onReady }) {
  return new Promise((resolve) => {
    const wrap = node("div");
    const { dlg, foot } = UI.baseDialog("准备备份工具", wrap);
    let env = null;
    let busy = false;
    let settled = false;

    function settle(status) {
      if (settled) return;
      settled = true;
      const environment = env;
      dlg.close();
      resolve({ status, environment });
    }

    const note = (text) => node("p", text, "sync-onboarding-note");
    const ok = (text) => node("div", text, "sync-onboarding-ok");

    function render() {
      const ready = !!env?.git?.available;
      const body = node("div");
      body.append(note(ready ? "备份工具已准备好。" : "安装 Git 后即可保存本地备份。"));
      if (ready && env.git.version) body.append(note(env.git.version));
      if (confirmWhenReady) body.append(note("准备完成后保存一次本地备份。"));
      if (!ready) body.append(note("安装包来自官方来源，下载和安装可能需要几分钟。"));
      wrap.replaceChildren(body);

      foot.replaceChildren();
      const cancel = UI.mkBtn("取消", "btn-outline");
      cancel.type = "button";
      cancel.disabled = busy;
      cancel.onclick = () => settle("cancelled");
      foot.append(cancel);

      if (allowSkip) {
        const skip = UI.mkBtn("暂不备份，继续使用", "btn-outline");
        skip.type = "button";
        skip.disabled = busy;
        skip.onclick = () => settle("skipped");
        foot.append(skip);
      }

      if (ready) {
        // 独立准备入口需要确认一次手动保存；其余调用在就绪后点「继续」回到调用方。
        const go = UI.mkBtn(confirmWhenReady ? confirmLabel : "继续");
        go.type = "button";
        go.disabled = busy;
        go.onclick = () => proceed(go);
        foot.append(go);
        return;
      }

      const actions = node("div", undefined, "onboarding-actions");
      if (env?.one_click_install) {
        const install = UI.mkBtn("安装 Git");
        install.type = "button";
        install.disabled = busy;
        install.onclick = () => installGit();
        actions.append(install);
      }
      const site = node("a", "打开 Git 官网下载页");
      site.href = "https://git-scm.com/downloads";
      site.target = "_blank";
      site.rel = "noopener";
      const recheck = UI.mkBtn("重新检测", "btn-outline");
      recheck.type = "button";
      recheck.disabled = busy;
      recheck.onclick = () => recheckEnv(recheck);
      actions.append(site, recheck);
      wrap.append(actions);
    }

    function showError(error, fallback) {
      const box = errorBox(error, fallback);
      wrap.append(box);
    }

    async function load(refresh) {
      const query = refresh ? "?refresh=true" : "";
      try {
        env = await request("GET", "/onboarding/environment" + query);
        return true;
      } catch (_) {
        env = null;
        return false;
      }
    }

    async function recheckEnv(button) {
      if (busy) return;
      busy = true;
      button.disabled = true;
      button.textContent = "检测中…";
      const loaded = await load(true);
      busy = false;
      button.disabled = false;
      button.textContent = "重新检测";
      render();
      if (!loaded) showError(null, "暂时无法检查备份工具");
    }

    function installGit() {
      DependencyInstall.open({
        component: "git",
        installation: env?.installation,
        install: async (body) => request("POST", "/onboarding/install", { ...body, confirm: true }),
        onDone: async (next) => {
          env = next;
          render();
          // 首次准备不需要额外确认：安装成功后直接继续。
          if (env?.git?.available && !confirmWhenReady) await proceed(null);
        },
      });
    }

    // 工具就绪后的动作：成功才算完成；失败保留已准备状态与重试入口，不重新安装。
    async function proceed(button) {
      if (busy) return;
      busy = true;
      if (button) {
        button.disabled = true;
        button.textContent = "处理中…";
      }
      try {
        await onReady?.(env);
        settle("ready");
      } catch (failure) {
        wrap.append(ok("备份工具已准备好。"), errorBox(failure, "本地备份未完成"));
        if (button) {
          button.disabled = false;
          button.textContent = "重试保存";
        }
      } finally {
        busy = false;
      }
    }

    dlg.addEventListener("close", () => {
      dlg.remove();
      // Esc 或原生关闭：按取消结算，不等同于用户选择跳过。
      settle("cancelled");
    });

    load(false).then((loaded) => {
      if (!loaded) {
        wrap.append(note("暂时无法检查备份工具。"), (() => {
          const retry = UI.mkBtn("重试");
          retry.type = "button";
          retry.onclick = () => retryCheck(retry);
          const box = node("div", undefined, "onboarding-actions");
          box.append(retry);
          return box;
        })());
        const cancel = UI.mkBtn("取消", "btn-outline");
        cancel.type = "button";
        cancel.onclick = () => settle("cancelled");
        foot.replaceChildren(cancel);
        return;
      }
      if (env?.git?.available && !confirmWhenReady) {
        settle("ready");
        return;
      }
      render();
    });

    async function retryCheck(button) {
      button.disabled = true;
      button.textContent = "检测中…";
      const loaded = await load(false);
      if (!loaded) {
        button.disabled = false;
        button.textContent = "重试";
        return;
      }
      if (env?.git?.available && !confirmWhenReady) {
        settle("ready");
        return;
      }
      render();
    }

    // showModal 在 load 之后的第一次 render 才有内容；先打开以便展示「正在检查」。
    wrap.append(note("正在检查备份工具…"));
    const cancel = UI.mkBtn("取消", "btn-outline");
    cancel.type = "button";
    cancel.onclick = () => settle("cancelled");
    foot.replaceChildren(cancel);
    dlg.showModal();
  });
}

export const BackupTools = { open };
