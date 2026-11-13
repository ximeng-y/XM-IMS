/* 依赖安装弹窗：Git 与登录组件共用。

   输入为环境快照里的 installation（模式、目标根、是否需要重新选路径），
   输出只通过回调通知调用方，不修改调用方数据，也不直接操作文件。
   桌面模式始终提供软件内、自选目录、系统位置三种选择；路径是否含中文由后端
   判定，前端只展示后端对已保存目标的判断，不对新选目录另行断言。 */
"use strict";

import { UI } from "xm/ui";
import { Desktop } from "xm/runtime/desktop";

const PATH_MESSAGE = "当前安装位置包含中文，请选择不含中文的文件夹，或安装到系统位置。";

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
 * @param {object} options.installation     后端返回的该组件安装目标快照
 * @param {(body: object) => Promise<object>} options.install
 *        执行安装的回调，参数为 {component, mode, path}，返回新的环境快照
 * @param {(env: object) => Promise<void>|void} [options.onDone] 安装成功后的通知，会被等待
 */
function open({ component, installation, install, onDone }) {
  const title = component === "gcm" ? "安装登录组件" : "安装 Git";
  const wrap = node("div");
  const { dlg, foot } = UI.baseDialog(title, wrap);
  const desktop = !!installation;
  let mode = installation?.mode === "global" ? "global" : "local";
  let chosenPath = null;
  let busy = false;

  const targetLine = node("p");
  const status = node("div");
  const actions = node("div");

  function render() {
    // 只对已保存目标使用后端的含中文判断；新选目录交给安装接口校验。
    const recorded = mode === installation?.mode && !chosenPath;
    const needsChoice = desktop && mode !== "global" && recorded && !!installation?.requires_path_choice;
    wrap.replaceChildren();
    status.replaceChildren();
    actions.replaceChildren();
    if (needsChoice) wrap.append(note(PATH_MESSAGE));
    else wrap.append(note("安装包来自官方来源，下载和安装可能需要几分钟。"));
    targetLine.textContent = mode === "global"
      ? "安装位置：系统默认位置（可能需要管理员确认）"
      : `安装位置：${chosenPath || (mode === installation?.mode ? installation?.root : "") || "软件目录"}`;
    wrap.append(targetLine, status, actions);

    if (desktop) {
      const choices = node("div", undefined, "sync-onboarding-options");
      const option = (value, text, disabled) => {
        const label = node("label", undefined, "sync-onboarding-option"), radio = node("input");
        radio.type = "radio"; radio.name = "dependency-mode"; radio.checked = mode === value;
        radio.disabled = busy || disabled;
        radio.onchange = () => { if (radio.checked && !busy) { mode = value; chosenPath = null; render(); } };
        label.append(radio, node("span", text));
        if (disabled) label.title = "当前环境无法打开文件夹选择窗口";
        choices.append(label);
      };
      option("local", "安装到软件目录", false);
      option("custom", "选择其他文件夹", !installation?.can_select_directory);
      option("global", "安装到系统位置", false);
      wrap.append(choices);
      if (mode === "custom") {
        const pick = UI.mkBtn("选择文件夹", "btn-outline");
        pick.type = "button";
        pick.disabled = busy;
        pick.onclick = async () => {
          const folder = await Desktop.selectDependencyDirectory();
          if (!folder) return; // 取消选择不发安装请求，也不改变已保存的位置
          chosenPath = folder;
          render();
        };
        actions.append(pick);
      }
    }

    const installBtn = UI.mkBtn("安装");
    installBtn.type = "button";
    installBtn.disabled = busy || (desktop && mode === "custom" && !chosenPath);
    installBtn.onclick = () => run(installBtn);
    actions.append(installBtn);

    const cancel = UI.mkBtn("取消", "btn-outline");
    cancel.type = "button";
    cancel.disabled = busy;
    cancel.onclick = () => dlg.close();
    foot.replaceChildren(cancel);
  }

  async function run(button) {
    busy = true;
    button.disabled = true;
    button.textContent = "安装中…";
    let installed = null;
    try {
      const body_ = { component, mode };
      if (mode === "custom" && chosenPath) body_.path = chosenPath;
      const env = await install(body_);
      const error = node("div", undefined, "sync-onboarding-error");
      if (env?.error) {
        error.append(node("strong", env.error.user_message || "安装没有完成"));
        if (env.error.user_hint) error.append(node("p", env.error.user_hint));
        status.append(error);
        return;
      }
      status.replaceChildren(node("div", "安装完成。", "sync-onboarding-ok"));
      installed = env;
    } catch (failure) {
      const error = node("div", undefined, "sync-onboarding-error");
      error.append(node("strong", failure?.display || failure?.user_message || "安装没有完成"));
      if (failure?.displayHint || failure?.user_hint) error.append(node("p", failure.displayHint || failure.user_hint));
      status.append(error);
      return;
    } finally {
      busy = false;
      button.disabled = false;
      button.textContent = "安装";
    }
    // 安装已经成功；后续动作失败与安装失败分别表达。
    try {
      await onDone?.(installed);
      dlg.close();
    } catch (failure) {
      const error = node("div", undefined, "sync-onboarding-error");
      error.append(node("strong", failure?.user_message || "安装已完成，但后续操作没有完成"));
      if (failure?.user_hint) error.append(node("p", failure.user_hint));
      status.append(error);
    }
  }

  render();
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
  return dlg;
}

export const DependencyInstall = { open };
