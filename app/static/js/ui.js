/* UI 基础层：店铺上下文、toast、通用对话框、格式化与页面启动 */
"use strict";

const UI = (() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const pad = (n) => String(n).padStart(2, "0");
  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return iso;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
           `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }
  function fmtTimeShort(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? iso : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
                            `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  // 带符号数量：入库绿 +、出库红 −
  function fmtDelta(n) {
    const cls = n > 0 ? "pos" : (n < 0 ? "neg" : "muted");
    const sign = n > 0 ? "+" : (n < 0 ? "−" : "");
    return `<span class="${cls}">${sign}${Math.abs(n)}</span>`;
  }
  function fmtMoney(v) {
    return v === null || v === undefined ? "—" : "¥" + Number(v).toFixed(2);
  }
  const fmtVal = (v) => v === null || v === undefined || v === "" ? "—" : esc(v);

  // ---------- toast ----------
  // 小窗用 showModal() 打开后进入浏览器 top layer，普通定位的 #toasts 无论 z-index
  // 多高都会被 dialog::backdrop 盖住。有小窗打开时把 toast 挂进该 dialog，让它与小窗
  // 同层（自然位于遮罩之上）；没有小窗时仍挂在页面右下角的 #toasts 里。
  function toastHost() {
    return document.querySelector("dialog[open]") || $("#toasts");
  }

  function toast(message, { title = "", hint = "", error = false, ms = 4200 } = {}) {
    const box = toastHost();
    const t = document.createElement("div");
    t.className = "toast" + (error ? " error" : "");
    t.innerHTML =
      (title ? `<div class="t-title">${esc(title)}</div>` : "") +
      `<div>${esc(message)}</div>` +
      (hint ? `<div class="t-hint">${esc(hint)}</div>` : "");
    box.appendChild(t);
    setTimeout(() => { t.classList.add("fade-out"); setTimeout(() => t.remove(), 320); }, ms);
  }
  const ok = (m, o) => toast(m, o);
  const fail = (m, o) => toast(m, { ...o, error: true, ms: 6500 });
  // ApiFail → toast（结构化错误 code + message + hint）
  const apiFail = (e, prefix = "") =>
    fail((prefix ? prefix + "：" : "") + e.message,
         { title: e.code, hint: e.hint || ("HTTP " + e.status) });
  // ApiFail → HTML 片段（用于表单内联错误框）
  function apiFailHtml(e) {
    return `<div class="result-box error-box"><div class="headline">` +
      `<span class="badge red">${esc(e.code)}</span> ${esc(e.message)}</div>` +
      (e.hint ? `<div class="hint">${esc(e.hint)}</div>` : "") + `</div>`;
  }
  // 操作前警告 → HTML 片段（用于「需再次确认」类的风险提示）
  function warnHtml(title, detail = "") {
    return `<div class="result-box warn-box"><div class="headline">` +
      `<span class="badge warn">注意</span> ${esc(title)}</div>` +
      (detail ? `<div class="hint">${esc(detail)}</div>` : "") + `</div>`;
  }

  // 按钮忙碌态包装：执行期间禁用按钮并显示「处理中」
  async function withBusy(btn, fn) {
    if (!btn || btn.disabled) return;
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = "处理中…";
    try { return await fn(); }
    finally { btn.disabled = false; btn.textContent = old; }
  }

  // ---------- 通用对话框 ----------
  function baseDialog(headText, bodyNode, footBtns) {
    const dlg = document.createElement("dialog");
    const head = document.createElement("div");
    head.className = "dlg-head";
    head.textContent = headText;
    const body = document.createElement("div");
    body.className = "dlg-body";
    if (bodyNode) body.appendChild(bodyNode);
    const foot = document.createElement("div");
    foot.className = "dlg-foot";
    dlg.append(head, body, foot);
    document.body.appendChild(dlg);
    return { dlg, body, foot };
  }

  // 单值输入对话框；resolve(用户输入) 或 resolve(null) 取消
  function promptDialog({ title, label, value = "", submitText = "确定", placeholder = "", maxLength }) {
    return new Promise((resolve) => {
      let done = false;
      const wrap = document.createElement("div");
      wrap.innerHTML =
        `<label>${esc(label)}</label>
         <input name="v" value="${esc(value)}" ${maxLength ? `maxlength="${maxLength}"` : ""}
                placeholder="${esc(placeholder)}" autocomplete="off">`;
      const { dlg, foot } = baseDialog(title, wrap);
      const input = wrap.querySelector("input");
      const cancel = mkBtn("取消", "btn-outline"), submit = mkBtn(submitText);
      cancel.type = "button"; submit.type = "button";
      foot.append(cancel, submit);
      const finish = (v) => { if (!done) { done = true; resolve(v); dlg.close(); } };
      cancel.onclick = () => finish(null);
      submit.onclick = () => {
        const v = input.value.trim();
        if (!v) { input.focus(); return; }
        finish(v);
      };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit.onclick(); });
      dlg.addEventListener("close", () => { finish(null); dlg.remove(); });
      dlg.showModal();
      setTimeout(() => { input.focus(); input.select(); }, 50);
    });
  }

  // 确认对话框；bodyHtml 可包含受信 HTML（调用方自行转义动态内容）
  function confirmDialog({ title, bodyHtml, confirmText = "确认", danger = false }) {
    return new Promise((resolve) => {
      let done = false;
      const wrap = document.createElement("div");
      wrap.innerHTML = bodyHtml;
      const { dlg, foot } = baseDialog(title, wrap);
      const cancel = mkBtn("取消", "btn-outline");
      const submit = mkBtn(confirmText, danger ? "btn-danger-outline" : "");
      foot.append(cancel, submit);
      const finish = (v) => { if (!done) { done = true; resolve(v); dlg.close(); } };
      cancel.onclick = () => finish(false);
      submit.onclick = () => finish(true);
      dlg.addEventListener("close", () => { finish(false); dlg.remove(); });
      dlg.showModal();
      setTimeout(() => submit.focus(), 50);
    });
  }

  function mkBtn(text, cls = "") {
    const b = document.createElement("button");
    b.textContent = text;
    if (cls) b.className = cls;
    return b;
  }

  // ---------- 结果面板 ----------
  function resultBox(sel, { error = false, html }) {
    const box = $(sel);
    if (!box) return;
    box.className = "result-box" + (error ? " error-box" : "");
    box.innerHTML = html;
  }

  // ---------- 分页 ----------
  function renderPager(sel, { offset, limit, total }, onGo) {
    const box = $(sel);
    if (!box) return;
    const from = total === 0 ? 0 : offset + 1;
    const to = Math.min(offset + limit, total);
    const prev = offset > 0, next = offset + limit < total;
    box.innerHTML =
      `<span>共 ${total} 条，第 ${from}–${to} 条</span>` +
      `<button class="btn-outline btn-sm" data-go="${Math.max(0, offset - limit)}" ${prev ? "" : "disabled"}>← 上一页</button>` +
      `<button class="btn-outline btn-sm" data-go="${offset + limit}" ${next ? "" : "disabled"}>下一页 →</button>`;
    $$("button[data-go]", box).forEach((b) =>
      b.addEventListener("click", () => onGo(Number(b.dataset.go))));
  }

  // ---------- 店铺上下文 ----------
  const SHOP_KEY = "xmims.shopId";

  function applyNoShop(hasShop) {
    const box = $("#noShopBox");
    if (box) box.hidden = hasShop;
    // 无店铺时禁用页面内全部操作按钮，避免无效请求
    $$("main.content button").forEach((b) => { b.disabled = !hasShop; });
  }

  async function initShop() {
    const sel = $("#shopSelect");
    try {
      const data = await XM.get("/shops");
      XM.state.shops = data.shops;
    } catch (e) {
      apiFail(e, "无法获取店铺列表");
      XM.state.shops = [];
    }
    sel.innerHTML = XM.state.shops
      .map((s) => `<option value="${s.id}">${esc(s.name)}（#${s.id}）</option>`).join("");
    const saved = localStorage.getItem(SHOP_KEY);
    let shop = XM.state.shops.find((s) => String(s.id) === saved);
    if (!shop && XM.state.shops.length) shop = XM.state.shops[0];
    XM.state.shopId = shop ? shop.id : null;
    if (shop) sel.value = String(shop.id);
    applyNoShop(!!shop);
    sel.addEventListener("change", () => {
      localStorage.setItem(SHOP_KEY, sel.value);
      // 切换店铺后整页刷新，确保所有数据与新店铺上下文一致
      window.location.reload();
    });
  }

  async function createShopFlow() {
    const name = await promptDialog({
      title: "新建店铺", label: "店铺名称（全局唯一）",
      placeholder: "如：主店", submitText: "创建", maxLength: 100,
    });
    if (name === null) return;
    try {
      const shop = await XM.post("/shops", { name });
      localStorage.setItem(SHOP_KEY, String(shop.id));
      ok(`店铺「${shop.name}」已创建`, { title: "SHOP_CREATED" });
      setTimeout(() => window.location.reload(), 600);
    } catch (e) { apiFail(e, "创建店铺失败"); }
  }

  // ---------- 单品下拉选项（库存操作 / 流水筛选共用） ----------
  // 单品标识形如 1-2（SPU 号-组内序号），直接作为选项值与展示文本
  async function fillItemOptions(select, { allLabel = null } = {}) {
    const data = await XM.get("/items?limit=200");
    let html = allLabel ? `<option value="">${esc(allLabel)}</option>` : "";
    html += data.items.map((i) =>
      `<option value="${esc(i.id)}">${esc(i.id)} ${esc(i.name)}（库存 ${i.qty}）</option>`).join("");
    select.innerHTML = html || `<option value="">（该店铺还没有单品）</option>`;
    return data.items;
  }

  // ---------- 启动 ----------
  document.addEventListener("DOMContentLoaded", async () => {
    $("#btnNewShop").addEventListener("click", createShopFlow);
    // 侧栏当前页高亮
    const page = document.body.dataset.page;
    $$(".sidenav a").forEach((a) => {
      if (a.dataset.nav === page) a.classList.add("active");
    });
    if (page === "sync") {
      $(".shop-box").hidden = true;
      if (typeof window.PAGE_INIT === "function") await window.PAGE_INIT();
      return;
    }
    try { if (!await Sync.ensureReady()) return; }
    catch (e) { fail("无法检查同步状态：" + e.message); return; }
    await initShop();
    if (typeof window.PAGE_INIT === "function") {
      try { await window.PAGE_INIT(); }
      catch (e) { console.error(e); fail("页面初始化失败：" + (e.message || e)); }
    }
  });

  return {
    $, $$, esc, fmtTime, fmtTimeShort, fmtDelta, fmtMoney, fmtVal,
    toast, ok, fail, apiFail, apiFailHtml, warnHtml, withBusy,
    baseDialog, promptDialog, confirmDialog, mkBtn, resultBox, renderPager, fillItemOptions,
    requireShop: () => XM.state.shopId !== null,
  };
})();

// 页面脚本共用的快捷选择器
window.$ = UI.$;
window.$$ = UI.$$;
