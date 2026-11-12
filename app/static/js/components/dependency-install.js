/* 依赖安装弹窗：Git 与登录组件共用。

   输入为环境快照里的 installation（模式、目标根、是否需要重新选路径），
   输出只通过回调通知调用方，不修改调用方数据，也不直接操作文件。
   路径是否含中文由后端判定，前端只展示后端给出的结果。 */
"use strict";

import { UI } from "xm/ui";
import { Desktop } from "xm/runtime/desktop";

const PATH_MESSAGE = "当前 Git 安装位置包含中文，请选择不含中文的文件夹，或安装到系统位置。";

const node = (tag, text, cls) => {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (cls) el.className = cls;
  return el;
};

function note(text) {
  return node("p", text, "sync-onboarding-note");
}

/**
 * 打开安装弹窗。
 * @param {object} options
 * @param {string} options.component        要安装的组件（git / gcm），仅用于标题与请求
 * @param {object} options.installation     后端返回的安装目标快照
 * @param {(body: object) => Promise<object>} options.install
 *        执行安装的回调，参数为 {component, mode, path}，返回新的环境快照
 * @param {(env: object) => void} [options.onDone] 安装成功后的通知
 */
function open({ component, installation, install, onDone }) {
  const title = component === "gcm" ? "安装登录组件" : "安装 Git";
  const wrap = node("div");
  const { dlg, foot } = UI.baseDialog(title, wrap);
  let mode = installation?.mode || "local";
  let chosenPath = null;
  let busy = false;

  const targetLine = node("p");
  const status = node("div");
  const actions = node("div");

  function render() {
    const needsChoice = mode !== "global" && !!installation?.requires_path_choice;
    wrap.replaceChildren();
    status.replaceChildren();
    actions.replaceChildren();
    if (needsChoice) wrap.append(note(PATH_MESSAGE));
    else wrap.append(note("安装包来自官方来源，下载和安装可能需要几分钟。"));
    targetLine.textContent = mode === "global"
      ? "安装位置：系统默认位置（可能需要管理员确认）"
      : `安装位置：${chosenPath ? chosenPath : installation?.root || "软件目录"}`;
    wrap.append(targetLine, status);

    const installBtn = UI.mkBtn("安装");
    installBtn.type = "button";
    installBtn.disabled = busy;
    installBtn.onclick = () => run(installBtn);
    actions.append(installBtn);

    if (needsChoice && installation?.can_select_directory) {
      const pickBtn = UI.mkBtn("选择其他文件夹", "btn-outline");
      pickBtn.type = "button";
      pickBtn.disabled = busy;
      pickBtn.onclick = async () => {
        const folder = await Desktop.selectDependencyDirectory();
        if (!folder) return; // 取消不发安装请求，也不改变已保存的位置
        mode = "custom";
        chosenPath = folder;
        render();
      };
      const systemBtn = UI.mkBtn("安装到系统位置", "btn-outline");
      systemBtn.type = "button";
      systemBtn.disabled = busy;
      systemBtn.onclick = () => { mode = "global"; chosenPath = null; render(); };
      actions.append(pickBtn, systemBtn);
    } else if (mode === "global") {
      const backBtn = UI.mkBtn("改为软件内安装", "btn-outline");
      backBtn.type = "button";
      backBtn.disabled = busy;
      backBtn.onclick = () => { mode = installation?.mode || "local"; chosenPath = null; render(); };
      actions.append(backBtn);
    }

    const cancel = UI.mkBtn("取消", "btn-outline");
    cancel.type = "button";
    cancel.disabled = busy;
    cancel.onclick = () => dlg.close();
    foot.replaceChildren(cancel);
    foot.append(actions);
  }

  async function run(button) {
    busy = true;
    button.disabled = true;
    button.textContent = "安装中…";
    try {
      const body_ = { component, mode };
      if (mode === "custom" && chosenPath) body_.path = chosenPath;
      const env = await install(body_);
      const error = node("div", undefined, "sync-onboarding-error");
      if (env?.error) {
        error.append(node("strong", env.error.user_message || "安装没有完成"));
        if (env.error.user_hint) error.append(node("p", env.error.user_hint));
        status.append(error);
      } else {
        status.append(node("div", "安装完成。", "sync-onboarding-ok"));
        onDone?.(env);
        dlg.close();
        return;
      }
    } catch (failure) {
      const error = node("div", undefined, "sync-onboarding-error");
      error.append(node("strong", failure?.display || "安装没有完成"));
      if (failure?.displayHint) error.append(node("p", failure.displayHint));
      status.append(error);
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = "安装";
    }
  }

  render();
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
  return dlg;
}

export const DependencyInstall = { open };
