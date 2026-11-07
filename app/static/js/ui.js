/* 通用展示层：选择器、格式化、toast、对话框、错误展示、按钮忙碌态与分页。
   不导入 API 客户端或具体页面，也不判断页面名称；店铺、同步与启动编排在 runtime 与 shell。 */
"use strict";

export const $ = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];

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
// 容器在首次使用时才查找：首启页没有 #toasts，模块求值阶段不能绑定不存在的节点。
let toastDialogs = [];
let toastBox = null;
function toastHost() {
  if (!toastBox) toastBox = document.getElementById("toasts");
  if (!toastBox) return document.body;
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
  ITEM_NAME_EXISTS: "单品名重复",
  ITEM_SPU_REQUIRED: "未指定 SPU",
  SPU_NOT_FOUND: "找不到 SPU", SPU_NAME_INVALID: "SPU 名有误",
  SPU_NAME_EXISTS: "SPU 名重复",
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

export const UI = {
  $, $$, esc, fmtTime, fmtTimeShort, fmtDelta, fmtMoney, fmtVal,
  toast, ok, fail, apiFail, apiFailHtml, warnHtml, okHtml, withBusy, codeTitle,
  baseDialog, promptDialog, confirmDialog, mkBtn, resultBox, renderPager,
};
