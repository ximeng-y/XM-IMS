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
  // 整体迁移 #toasts，保留固定右下角定位、间距和已有提示的消失计时。
  // modal 按实际打开顺序入栈；关闭最上层后回到下一层，全部关闭才回到页面。
  const toastBox = $("#toasts");
  let toastDialogs = [];
  function toastHost() {
    toastDialogs = toastDialogs.filter((dlg) => dlg.isConnected && dlg.matches(":modal"));
    const host = toastDialogs.at(-1) || document.body;
    if (toastBox.parentNode !== host) host.appendChild(toastBox);
    return toastBox;
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

  // 错误码 → 界面标题。错误码本身（INSUFFICIENT_STOCK 之类）是在代码与日志里使用的标识，
  // 不面向用户展示；未收录的码不显示标题，只在控制台留一条便于排查的记录。
  const CODE_TITLES = {
    NETWORK_ERROR: "无法连接", VALIDATION_ERROR: "填写有误",
    SHOP_HEADER_MISSING: "未选择店铺", SHOP_NOT_FOUND: "店铺不可用",
    ITEM_REF_INVALID: "单品编号无效", ITEM_NOT_FOUND: "找不到单品",
    ITEM_NAME_INVALID: "单品名有误", ITEM_HAS_MOVEMENTS: "单品不能删除",
    ITEM_SPU_REQUIRED: "未指定 SPU",
    SPU_NOT_FOUND: "找不到 SPU", SPU_NAME_INVALID: "SPU 名有误",
    SPU_HAS_ITEMS: "SPU 不能删除",
    FIELD_NOT_FOUND: "找不到字段", FIELD_NAME_INVALID: "字段名有误",
    FIELD_NAME_EXISTS: "字段名重复", FIELD_TYPE_INVALID: "字段类型有误",
    FIELD_VALUE_INVALID: "字段值有误", FIELD_READONLY: "字段为只读",
    FIELD_NO_CHANGE: "结构与目标相同", FIELD_REFERENCED: "字段被引用",
    SCALE_NOT_APPLICABLE: "小数位不适用", NEGATIVE_NOT_APPLICABLE: "不能为负",
    NEGATIVE_VALUES_EXIST: "存在负值", ROUNDING_REQUIRED: "需要选择取整方式",
    ROUNDING_INVALID: "取整方式有误", CONVERT_REQUIRES_CLEAR: "需要确认清空",
    CONFIRM_REQUIRED: "需要确认", UNKNOWN_FIELD: "存在未知字段",
    INSUFFICIENT_STOCK: "库存不足", MOVEMENT_TYPE_INVALID: "流水类型有误",
    FORMULA_INVALID: "公式有误", FORMULA_TYPE_INVALID: "公式结果类型有误",
    FORMULA_NOT_COMPUTED: "不是计算字段", FORMULA_REF_INVALID: "公式引用有误",
    FORMULA_CYCLE: "公式存在循环引用",
    SHOP_NAME_INVALID: "店铺名有误", SHOP_NAME_EXISTS: "店铺名重复",
    SHOP_DELETE_FAILED: "店铺未能删除",
    IDEMPOTENCY_CONFLICT: "重复提交",
  };
  const codeTitle = (code) => CODE_TITLES[code] || "";

  // ApiFail → toast（标题取中文说明，正文取用户版文案，末尾附可操作提示）
  const apiFail = (e, prefix = "") => {
    if (e && e.code) console.warn("[api]", e.code, e.message);
    const title = codeTitle(e.code);
    const msg = (prefix ? prefix + "：" : "") + (e.display || e.message);
    // 没有面向用户的提示时留空，不用 HTTP 状态码之类的实现细节填充
    const hint = e.displayHint || "";
    fail(msg, { title, hint });
  };
  // ApiFail → HTML 片段（用于表单内联错误框）
  function apiFailHtml(e) {
    if (e && e.code) console.warn("[api]", e.code, e.message);
    return `<div class="result-box error-box"><div class="headline">` +
      `<span class="badge red">${esc(codeTitle(e.code) || "操作未完成")}</span> ` +
      `${esc(e.display || e.message)}</div>` +
      (e.displayHint ? `<div class="hint">${esc(e.displayHint)}</div>` : "") + `</div>`;
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
    // 只包装本工厂创建的实例，不改全局原型或页面调用方式。
    const showModal = dlg.showModal, close = dlg.close;
    dlg.showModal = function (...args) {
      const wasModal = dlg.matches(":modal");
      const result = showModal.apply(dlg, args);
      if (!wasModal && dlg.matches(":modal")) {
        toastDialogs.push(dlg);
        toastHost();
      }
      return result;
    };
    dlg.close = function (...args) {
      const result = close.apply(dlg, args);
      toastHost();
      return result;
    };
    // Esc、原生表单关闭也会触发；先于调用方的 close 监听迁移，避免 remove() 丢提示。
    dlg.addEventListener("close", toastHost);
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

  // 成功结果块（统一结构：徽标 + 正文 + 可选提示）；成功不展示任何接口代号
  function okHtml(text, hint = "") {
    return `<div class="headline"><span class="badge fill">已完成</span> ${esc(text)}</div>` +
      (hint ? `<div class="hint">${esc(hint)}</div>` : "");
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
  // 排列顺序纯属本机偏好：只存在 localStorage，不写入数据库、配置、备份或同步快照
  const ORDER_KEY = "xmims.shopOrder";

  function readOrder() {
    try {
      const raw = JSON.parse(localStorage.getItem(ORDER_KEY) || "[]");
      return Array.isArray(raw) ? raw.map(Number).filter(Number.isInteger) : [];
    } catch (_) { return []; }
  }

  function saveShopOrder(ids) {
    localStorage.setItem(ORDER_KEY, JSON.stringify([...new Set(ids.map(Number))]));
  }

  // 按本地顺序排列：已记录的店铺按记录顺序在前，未记录的按服务器顺序追加到末尾。
  // 已保存顺序中不再存在的店铺自然被丢弃。
  function orderShops(list) {
    const order = readOrder();
    const left = new Map(list.map((s) => [s.id, s]));
    const ordered = [];
    order.forEach((id) => {
      const shop = left.get(id);
      if (shop) { ordered.push(shop); left.delete(id); }
    });
    list.forEach((s) => { if (left.has(s.id)) ordered.push(s); });
    return ordered;
  }

  // 排列结果落盘：同时记录当前完整顺序，供后续拖动与删除后重选使用
  function applyShopOrder(list) {
    const ordered = orderShops(list);
    saveShopOrder(ordered.map((s) => s.id));
    return ordered;
  }

  // 删除店铺后清理本地记录；删的是当前店铺时切到本地顺序中的第一个剩余店铺。
  // remaining 为删除后的完整店铺列表（已按本地顺序），避免从残留顺序里选出已删店铺。
  function forgetShop(id, remaining = []) {
    saveShopOrder(readOrder().filter((x) => x !== id));
    if (Number(localStorage.getItem(SHOP_KEY)) !== Number(id)) return;
    if (remaining.length) localStorage.setItem(SHOP_KEY, String(remaining[0].id));
    else localStorage.removeItem(SHOP_KEY);
  }

  function renderShopOptions() {
    const sel = $("#shopSelect");
    if (!sel) return;
    sel.innerHTML = XM.state.shops
      .map((s) => `<option value="${s.id}">${esc(s.name)}（#${s.id}）</option>`).join("");
    if (XM.state.shopId !== null) sel.value = String(XM.state.shopId);
  }

  // 零店铺时：不保留空的选择框，原位置换成「新建店铺」。店铺管理页本身不参与
  // 零店铺的整页禁用，否则空状态无法创建第一间店铺。
  // hasShop 为 null 表示列表读取失败、店铺数未知：此时隐藏选择框与空状态提示，
  // 避免把读取失败当成「当前还没有店铺」。
  function applyNoShop(hasShop) {
    const known = hasShop !== null;
    const box = $("#noShopBox");
    if (box) box.hidden = !known ? true : (hasShop || document.body.dataset.page === "shops");
    const sel = $("#shopSelect");
    if (sel) sel.hidden = !hasShop;
    const label = $(".shop-box label");
    if (label) label.hidden = !hasShop;
    const btn = $("#btnNewShop");
    if (btn) btn.hidden = hasShop !== false;
    if (document.body.dataset.page === "shops") return;
    $$("main.content button").forEach((b) => { b.disabled = !hasShop; });
  }

  async function initShop() {
    try {
      const data = await XM.get("/shops");
      XM.state.shops = applyShopOrder(data.shops);
    } catch (e) {
      apiFail(e, "无法获取店铺列表");
      XM.state.shops = [];
      XM.state.shopId = null;
      applyNoShop(null);
      return false;
    }
    renderShopOptions();
    const saved = localStorage.getItem(SHOP_KEY);
    let shop = XM.state.shops.find((s) => String(s.id) === saved);
    if (!shop && XM.state.shops.length) shop = XM.state.shops[0];
    XM.state.shopId = shop ? shop.id : null;
    const sel = $("#shopSelect");
    if (shop) sel.value = String(shop.id);
    applyNoShop(!!shop);
    sel.addEventListener("change", () => {
      localStorage.setItem(SHOP_KEY, sel.value);
      // 切换店铺后整页刷新，确保所有数据与新店铺上下文一致
      window.location.reload();
    });
    return true;
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
      // 新店铺默认追加到末尾：不写入本地顺序，orderShops 会自动把它排在已有店铺之后
      ok(`店铺「${shop.name}」已创建`);
      setTimeout(() => window.location.reload(), 600);
    } catch (e) { apiFail(e, "创建店铺失败"); }
  }

  // ---------- 单品下拉选项（库存操作 / 流水筛选共用） ----------
  // 展示用「SPU 名-SKU 名（库存 n）」，便于一眼看出单品归属哪个 SPU；
  // 选项值仍是外部标识（形如 1-2，SPU 号-组内序号），提交给接口的编号不受影响。
  // 补选项等其余拼装路径一律调这里，避免同一份下拉出现两种文案。
  function itemOptionLabel(i) {
    return `${i.spu_name}-${i.name}（库存 ${i.qty}）`;
  }

  async function fillItemOptions(select, { allLabel = null } = {}) {
    const data = await XM.get("/items?limit=200");
    let html = allLabel ? `<option value="">${esc(allLabel)}</option>` : "";
    html += data.items.map((i) =>
      `<option value="${esc(i.id)}">${esc(itemOptionLabel(i))}</option>`).join("");
    select.innerHTML = html || `<option value="">（该店铺还没有单品）</option>`;
    return data.items;
  }

  // ---------- 窄屏导航浮层与表格滑动提示 ----------
  // 断点与 CSS 保持一致：不超过 860px 视为窄屏（浮层导航 + 表格组件内滚动）
  const NARROW = window.matchMedia("(max-width: 860px)");
  const isNarrow = () => NARROW.matches;

  // 窄屏顶栏高度随内容变化（换行或店铺区显示与否）；浮层与遮罩都贴在其下沿，
  // 故把实测高度写进 --mobile-topbar-height 供两者定位
  function watchTopbar() {
    const bar = $(".topbar");
    if (!bar) return;
    const apply = () => {
      document.documentElement.style.setProperty(
        "--mobile-topbar-height", bar.getBoundingClientRect().height + "px");
    };
    apply();
    // 同步状态文案、店铺区域显示与否或视口变化都会改变顶栏尺寸
    new ResizeObserver(apply).observe(bar);
  }

  const navOpen = () => document.body.classList.contains("nav-open");

  // 浮层期间的背景隔离：CSS 的 pointer-events 只挡指针命中，Tab / Shift+Tab
  // 仍会走进被遮罩盖住的正文与顶栏控件（Enter 照常触发），故这里再给它们加 inert
  function setNavInert(on) {
    $$("main.content, .topbar > *:not(#navToggle)").forEach((el) => {
      el.toggleAttribute("inert", on);
    });
  }

  // 关闭浮层：清标记、撤遮罩与滚动锁，焦点可选地回到菜单按钮
  function setNavClosed({ focusBtn = false } = {}) {
    const wasOpen = navOpen();
    document.body.classList.remove("nav-open");
    // 先恢复交互再还原焦点：焦点不能落在 inert 元素内
    setNavInert(false);
    const backdrop = $("#navBackdrop");
    if (backdrop) backdrop.hidden = true;
    const btn = $("#navToggle");
    if (!btn) return;
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-label", "打开导航菜单");
    if (wasOpen && focusBtn) btn.focus();
  }

  function setNavOpen() {
    const nav = $("#primaryNav");
    if (!nav) return;
    document.body.classList.add("nav-open");
    setNavInert(true);
    const backdrop = $("#navBackdrop");
    if (backdrop) backdrop.hidden = false;
    const btn = $("#navToggle");
    if (btn) {
      btn.setAttribute("aria-expanded", "true");
      btn.setAttribute("aria-label", "关闭导航菜单");
    }
    // 焦点移到当前页面对应的可见导航项，键盘用户可直接继续操作
    const current = $$("a", nav).find((a) => !a.hidden && a.classList.contains("active"));
    if (current) current.focus();
  }

  // 从窄屏切回桌面时清掉浮层，不留遮罩、滚动锁与不可交互状态
  function onNarrowChange() {
    if (!isNarrow() && navOpen()) setNavClosed();
    scheduleHintUpdate();
  }

  function initNav() {
    const nav = $("#primaryNav"), backdrop = $("#navBackdrop"), btn = $("#navToggle");
    if (!nav || !btn || !backdrop) return;
    btn.addEventListener("click", () => {
      if (navOpen()) setNavClosed({ focusBtn: true });
      else setNavOpen();
    });
    backdrop.addEventListener("click", () => setNavClosed({ focusBtn: true }));
    // 选择页面后立即收起浮层
    nav.addEventListener("click", (e) => { if (e.target.closest("a")) setNavClosed(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && navOpen()) setNavClosed({ focusBtn: true });
    });
    NARROW.addEventListener("change", onNarrowChange);
    watchTopbar();
  }

  // ---------- 表格横向滑动提示 ----------
  // 每个滚动容器后跟一个提示节点（只创建一次，不放进滚动区域内）。
  // 仅窄屏且确实横向溢出时显示；尺寸变化用 rAF 合并，不做定时轮询。
  function ensureHints() {
    $$("main.content .table-scroll").forEach((scroller) => {
      const next = scroller.nextElementSibling;
      if (next && next.classList.contains("table-scroll-hint")) return;
      const hint = document.createElement("div");
      hint.className = "table-scroll-hint";
      hint.textContent = "左右滑动查看其余列";
      scroller.after(hint);
    });
  }

  function refreshHints() {
    const narrow = isNarrow();
    $$("main.content .table-scroll-hint").forEach((hint) => {
      const scroller = hint.previousElementSibling;
      const overflowing = !!scroller && scroller.scrollWidth > scroller.clientWidth + 1;
      hint.classList.toggle("show", narrow && overflowing);
    });
  }

  let hintFrame = 0;
  function scheduleHintUpdate() {
    if (hintFrame) return;
    hintFrame = requestAnimationFrame(() => { hintFrame = 0; refreshHints(); });
  }

  function watchHints() {
    ensureHints();
    const scrollers = $$("main.content .table-scroll");
    if (!scrollers.length) return;
    const ro = new ResizeObserver(scheduleHintUpdate);
    // 表格内容变化（数据渲染、展开行）会改变 scrollWidth，容器自身尺寸未必变
    const mo = new MutationObserver(scheduleHintUpdate);
    scrollers.forEach((s) => {
      ro.observe(s);
      mo.observe(s, { childList: true, subtree: true });
    });
    refreshHints();
  }

  // 导航与提示先就位：启动检查阶段（含数据同步页）也要能用菜单
  function initResponsive() {
    initNav();
    watchHints();
    window.addEventListener("resize", scheduleHintUpdate);
  }

  // ---------- 启动 ----------
  document.addEventListener("DOMContentLoaded", async () => {
    initResponsive();
    $("#btnNewShop").addEventListener("click", createShopFlow);
    // 侧栏当前页高亮
    const page = document.body.dataset.page;
    $$(".sidenav a").forEach((a) => {
      if (a.dataset.nav === page) {
        a.classList.add("active");
        a.setAttribute("aria-current", "page");
      }
    });
    // 同步与设置页不使用店铺上下文：零店铺时也要能用，顶栏店铺框一并隐藏
    if (page === "sync" || page === "settings") {
      $(".shop-box").hidden = true;
      if (typeof window.PAGE_INIT === "function") await window.PAGE_INIT();
      return;
    }
    try { if (!await Sync.ensureReady()) return; }
    catch (e) { fail("无法检查同步状态：" + e.message); return; }
    if (!await initShop()) return;
    if (typeof window.PAGE_INIT === "function") {
      try { await window.PAGE_INIT(); }
      catch (e) { console.error(e); fail("页面初始化失败：" + (e.message || e)); }
    }
  });

  return {
    $, $$, esc, fmtTime, fmtTimeShort, fmtDelta, fmtMoney, fmtVal,
    toast, ok, fail, apiFail, apiFailHtml, warnHtml, okHtml, withBusy, codeTitle,
    baseDialog, promptDialog, confirmDialog, mkBtn, resultBox, renderPager, fillItemOptions,
    itemOptionLabel, SHOP_KEY, readOrder, orderShops, applyShopOrder, saveShopOrder, forgetShop,
    renderShopOptions, applyNoShop, createShopFlow,
    requireShop: () => XM.state.shopId !== null,
  };
})();

// 页面脚本共用的快捷选择器
window.$ = UI.$;
window.$$ = UI.$$;
