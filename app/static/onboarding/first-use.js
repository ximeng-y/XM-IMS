/* 首启页仅组织首店与共享备份组件，不加载全站店铺选择器和业务初始化。 */
"use strict";
document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("firstUse");
  const key = "xmims.shopId";
  let state = null, name = "", requestKey = null, createdHere = false, backupActive = false, busy = false;
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
  function enterBusiness(shops, preferred) {
    if (!shops.length) return;
    const saved = preferred ?? localStorage.getItem(key);
    const shop = shops.find(s => String(s.id) === String(saved)) || shops[0];
    localStorage.setItem(key, String(shop.id));
    location.replace("/inventory");
  }
  function showComplete(preferred) {
    // 两个入口放进独立操作容器，由容器统一给出间距；全局按钮外边距已被清零
    const actions = node("div", undefined, "onboarding-actions");
    actions.append(
      button("开始使用", () => enterBusiness(state.shops, preferred), ""),
      button("配置远端备份（可稍后）", () => startBackup("backup")));
    root.replaceChildren(node("h2", "店铺已准备好"), node("p", "数据保存在本机。"), actions);
  }
  function showShop() {
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
        if (state.phase === "ready") { createdHere = true; showComplete(); return; }
        if (!["choose_source", "create_shop"].includes(state.phase)) { await refresh(); return; }
        if (!requestKey) requestKey = crypto.randomUUID();
        try {
          const result = await Sync.api("POST", "/onboarding/first-use/shop",
            { name: name.trim(), confirm: true, idempotency_key: requestKey });
          state = { phase: result.phase, shops: [result.shop], sync: result.sync };
          createdHere = true; showComplete(result.shop.id);
        } catch (error) {
          const actual = await Sync.api("GET", "/onboarding/first-use/status");
          state = actual;
          if (actual.phase === "ready") { createdHere = true; showComplete(); }
          else root.append(errorBox(error));
        }
      });
    };
    root.append(form);
    if (state.phase === "choose_source") root.append(button("返回开始选项", () => refresh()));
    input.focus();
  }
  async function startBackup(intent) {
    const settings = await Sync.api("GET", "/settings");
    backupActive = true;
    BackupOnboarding.mount({ container: root, intent, settings, request: Sync.api, completeLabel: "开始使用",
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
});
