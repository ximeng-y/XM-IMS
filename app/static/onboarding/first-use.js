/* 首启页仅组织首店与共享备份组件，不加载全站店铺选择器和业务初始化。 */
"use strict";

import { Sync } from "xm/sync";
import { ShopContext } from "xm/shop-context";
import { BackupOnboarding } from "xm/onboarding/backup";
import { BackupTools } from "xm/components/backup-tools";

export const page = {
  shell: false, shopContext: "none", requireSyncReady: false, autoUpdatePrompt: false,
  init: async () => {
    const root = document.getElementById("firstUse");
    let state = null, name = "", requestKey = null, createdHere = false, backupActive = false, busy = false;
    // 本页只在从零开始时准备一次工具，渲染或输入不重复打开弹窗。
    let toolsChecked = false, toolsSkipped = false, toolsEnv = null, backupRequestKey = null;
    // 本机数据是否存在以状态接口为准；创建成功与读回成功时同步更新。
    let hasLocalData = false;
    const node = (tag, text, cls) => {
      const el = document.createElement(tag);
      if (text !== undefined) el.textContent = text;
      if (cls) el.className = cls;
      return el;
    };
    function errorBox(error) {
      const box = node("div", undefined, "sync-onboarding-error");
      box.append(node("strong", error?.user_message || "这一步暂未完成，请重试"));
      if (error?.user_hint) box.append(node("p", error.user_hint));
      return box;
    }
    // 本地备份失败的提示挂在首启容器之外：向导就地替换容器内容时，原因与重试入口仍在。
    const backupNotice = node("div", undefined, "sync-onboarding onboarding-backup-notice");
    root.before(backupNotice);
    async function run(action) {
      if (busy) return;
      busy = true;
      root.querySelectorAll("button,input").forEach(el => {
        el.dataset.firstUseDisabled = String(el.disabled); el.disabled = true;
      });
      try { await action(); }
      catch (error) { root.append(errorBox(error)); }
      finally {
        busy = false;
        root.querySelectorAll("[data-first-use-disabled]").forEach(el => {
          el.disabled = el.dataset.firstUseDisabled === "true";
          delete el.dataset.firstUseDisabled;
        });
      }
    }
    function button(text, action, cls = "btn-outline") {
      const el = node("button", text, cls);
      el.type = "button"; el.onclick = () => run(action);
      return el;
    }
    // 本地首次准备：允许暂不备份；只有 ready 或明确跳过才继续创建首店。
    async function prepareTools() {
      if (toolsSkipped) return true;
      if (toolsChecked) return true;
      const result = await BackupTools.open({
        request: Sync.api, allowSkip: true, confirmLabel: "继续创建店铺" });
      toolsEnv = result.environment;
      if (result.status === "ready") { toolsChecked = true; return true; }
      if (result.status === "skipped") { toolsSkipped = true; return true; }
      return false; // 取消回到开始选项
    }
    // 本地备份失败的独立提示：原因与重试入口放在向导之外，向导替换内容时不会一并消失。
    // 重试只重发保存请求，成功后就地清除提示，不重新创建店铺。
    function backupFailureBox(error, host) {
      const box = node("section", undefined, "sync-onboarding-error");
      box.append(node("strong", error?.user_message || "本地备份暂未完成"));
      if (error?.user_hint) box.append(node("p", error.user_hint));
      const retry = node("button", "重试本地备份", "btn-outline");
      retry.type = "button";
      retry.onclick = async () => {
        retry.disabled = true;
        retry.textContent = "正在保存…";
        const result = await backupAfterCreate();
        if (result?.error) {
          host.replaceChildren(backupFailureBox(result.error, host));
          return;
        }
        // 条件已不再满足（关闭自动备份或工具不可用）时不报告已保存。
        host.replaceChildren(result ? node("p", "本地备份已保存。", "sync-onboarding-note") : node("div"));
      };
      box.append(retry);
      return box;
    }
    // 首店创建成功后：Git 可用且保存的 local_auto=true 时单独保存一次本地备份。
    // 没有可用工具或没有可用数据时不发请求。
    async function backupAfterCreate() {
      try {
        const settings = await Sync.api("GET", "/settings");
        if (!settings.local_auto) return null;
        if (!toolsEnv) toolsEnv = await Sync.api("GET", "/onboarding/environment");
        if (!toolsEnv.git?.available) return null;
        // 同一次保存的重试复用同一个幂等键；成功后清除。
        if (!backupRequestKey) backupRequestKey = crypto.randomUUID();
        const result = await Sync.api("POST", "/backups", { idempotency_key: backupRequestKey });
        backupRequestKey = null;
        return result;
      } catch (error) {
        return { error };
      }
    }
    function enterBusiness(shops, preferred) {
      if (!shops.length) return;
      const saved = preferred ?? localStorage.getItem(ShopContext.SHOP_KEY);
      const shop = shops.find(s => String(s.id) === String(saved)) || shops[0];
      ShopContext.rememberSelection(shop.id);
      location.replace("/inventory");
    }
    function showComplete(preferred, backupError) {
      // 两个入口放进独立操作容器，由容器统一给出间距；全局按钮外边距已被清零
      const actions = node("div", undefined, "onboarding-actions");
      actions.append(
        button("开始使用", () => enterBusiness(state.shops, preferred), ""),
        button("配置远端备份（可稍后）", () => startBackup("backup")));
      const children = [node("h2", "店铺已准备好"), node("p", "数据保存在本机。")];
      if (backupError) {
        children.push(node("p", "店铺已准备好，本地备份未完成。"));
        children.push(errorBox(backupError));
        actions.prepend(button("重试本地备份", () => retryBackup(), "btn-outline"));
      }
      children.push(actions);
      root.replaceChildren(...children);
    }
    async function retryBackup() {
      const result = await backupAfterCreate();
      if (result?.error) { showComplete(undefined, result.error); return; }
      if (result) root.replaceChildren(node("h2", "店铺已准备好"), node("p", "本地备份已保存。"),
        (() => { const actions = node("div", undefined, "onboarding-actions");
          actions.append(button("开始使用", () => enterBusiness(state.shops), ""),
            button("配置远端备份（可稍后）", () => startBackup("backup")));
          return actions; })());
    }
    // 首店已存在时的统一收尾：正常响应与两处成功读回都走这里，
    // 只补一次本地保存，不重新创建店铺。
    async function finishCreate(preferred) {
      createdHere = true;
      hasLocalData = true;
      const backup = await backupAfterCreate();
      showComplete(preferred, backup?.error);
    }
    async function showShop() {
      if (!(await prepareTools())) { await refresh(); return; }
      root.replaceChildren(node("h2", "创建第一间店铺"),
        node("p", "填写店铺名称，即可开始记录库存。"));
      const form = node("form"), label = node("label", "店铺名称"), input = node("input");
      input.maxLength = 100; input.required = true; input.autocomplete = "off";
      input.placeholder = "如：主店"; input.value = name;
      input.oninput = () => {
        if (name.trim() !== input.value.trim()) requestKey = null;
        name = input.value;
        submit.disabled = !name.trim();
      };
      label.append(input);
      const submit = node("button", "创建店铺");
      submit.type = "submit"; submit.disabled = !name.trim();
      form.append(label, submit);
      form.onsubmit = event => {
        event.preventDefault();
        if (!name.trim() || busy) return;
        run(async () => {
          // 重试先读回首店；成功回执丢失时也不会再创建第二间店铺。
          state = await Sync.api("GET", "/onboarding/first-use/status");
          hasLocalData = !!state.has_local_data;
          if (state.phase === "ready") { await finishCreate(); return; }
          if (!["choose_source", "create_shop"].includes(state.phase)) { await refresh(); return; }
          if (!requestKey) requestKey = crypto.randomUUID();
          try {
            const result = await Sync.api("POST", "/onboarding/first-use/shop",
              { name: name.trim(), confirm: true, idempotency_key: requestKey });
            state = { phase: result.phase, shops: [result.shop], sync: result.sync };
            // 店铺已创建：本地备份失败不撤销店铺，也不把创建按钮变回可重试状态。
            await finishCreate(result.shop.id);
          } catch (error) {
            const actual = await Sync.api("GET", "/onboarding/first-use/status");
            state = actual;
            // 服务端已建店（回执丢失或重试读回成功）时同样补一次本地保存。
            hasLocalData = !!actual.has_local_data;
            if (actual.phase === "ready") await finishCreate();
            else root.append(errorBox(error));
          }
        });
      };
      root.append(form);
      if (state.phase === "choose_source") root.append(button("返回开始选项", () => refresh()));
      input.focus();
    }
    // 远端向导入口：先完成不允许跳过的工具准备，再把环境快照交给原向导。
    // 远端前置准备后的本地保存由本页调用方执行，不放进 mount 或环境接口。
    async function startBackup(intent) {
      let tools;
      try {
        tools = await BackupTools.open({ request: Sync.api, confirmLabel: "继续配置远端备份" });
      } catch (error) { root.append(errorBox(error)); return; }
      if (tools.status !== "ready") return; // 取消前置准备时不打开仓库选择
      // 环境快照与工具是否就绪以本次准备结果为准，后续保存判断不再依赖旧的跳过状态。
      toolsEnv = tools.environment;
      backupNotice.replaceChildren();
      const settings = await Sync.api("GET", "/settings");
      if (intent !== "restore" && hasLocalData && toolsEnv?.git?.available && settings.local_auto) {
        // 已有可用本机数据且 local_auto=true：先补一次本地备份；失败在向导外保留原因与重试入口。
        const backup = await backupAfterCreate();
        if (backup?.error) backupNotice.append(backupFailureBox(backup.error, backupNotice));
      }
      backupActive = true;
      BackupOnboarding.mount({ container: root, intent, settings, request: Sync.api,
        environment: tools.environment, completeLabel: "开始使用",
        async onComplete() {
          backupActive = false;
          state = await Sync.api("GET", "/onboarding/first-use/status");
          if (state.phase === "ready") enterBusiness(state.shops);
          else await refresh();
        },
        async onCancel() { backupActive = false; await refresh(); },
      });
    }
    async function refresh() {
      if (backupActive) return;
      if (!Sync.local) {
        root.replaceChildren(node("p", "请在运行本程序的电脑上完成首次设置"));
        return;
      }
      try {
        state = await Sync.api("GET", "/onboarding/first-use/status");
        hasLocalData = !!state.has_local_data;
        if (backupActive) return;
        if (state.phase === "ready") {
          if (createdHere) showComplete();
          else enterBusiness(state.shops);
        } else if (state.phase === "create_shop") showShop();
        else if (state.phase === "choose_source") {
          const options = node("div", undefined, "onboarding-start");
          options.append(button("从零开始使用", showShop),
            button("在这台电脑使用已有备份", () => startBackup("restore")));
          root.replaceChildren(node("h2", "选择开始方式"), options);
          if (state.sync.legacy_present) root.append(node("p", "检测到旧数据库，原文件已保留。"));
        } else if (state.phase === "restore_pending") {
          root.replaceChildren(node("h2", "继续连接已有备份"),
            button("继续连接已有备份", () => startBackup("restore"), ""));
          if (state.sync.error) root.append(errorBox(state.sync.error));
        } else if (state.phase === "waiting") {
          root.replaceChildren(node("p", "正在准备数据，请稍候…"));
          setTimeout(refresh, 1500);
        } else {
          root.replaceChildren(node("h2", "继续准备数据"));
          if (state.sync.error) root.append(errorBox(state.sync.error));
          const link = node("a", "进入数据同步处理"); link.href = "/sync";
          root.append(link);
        }
      } catch (error) {
        root.replaceChildren(errorBox(error), button("重试", refresh));
      }
    }
    refresh();
  },
};
