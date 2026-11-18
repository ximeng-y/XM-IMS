/* 共享组件：数学计算属性公式编辑器。
   SKU 公式直接组合本归属数值属性与 qty；SPU 外层可引用 SPU 数值属性、
   合计库存、SKU 数，并可将一个 SKU 子表达式作为 SUM 聚合项。 */
"use strict";

import { UI } from "xm/ui";

const SYMBOLS = [["+", "+"], ["-", "−"], ["*", "×"], ["/", "÷"], ["(", "("], [")", ")"]];
const SYMBOL_TEXT = { "+": "+", "-": "−", "*": "×", "/": "÷", "(": "(", ")": ")" };
const SYSTEMS = {
  sku: [["qty", "库存数量"]],
  spu: [["total_qty", "合计库存"], ["sku_count", "SKU 数"]],
};
const SYSTEM_LABEL = Object.assign({},
  Object.fromEntries(SYSTEMS.sku), Object.fromEntries(SYSTEMS.spu));
const NUMBER_RE = /^-?\d+(\.\d+)?$/;
const NUMBER_HINT = "只接受普通十进制整数或小数，例如 12、-3.5。";

function cloneTokens(tokens) {
  return (tokens || []).map((token) => token.kind === "aggregate"
    ? { ...token, tokens: cloneTokens(token.tokens) }
    : { ...token });
}

function versionOf(tokens) {
  return (tokens || []).some((token) => token.kind === "aggregate") ? 2 : 1;
}

function referable(fields, excludeUid) {
  return (fields || []).filter((field) => !field.system &&
    field.type !== "string" && field.type !== "bool" && field.uid !== excludeUid);
}

function formatTokens(tokens, fields = [], { excludeUid = null } = {}) {
  const names = {};
  referable(fields, excludeUid).forEach((field) => { names[field.uid] = field.name; });
  function label(token) {
    if (token.kind === "number") return token.value;
    if (token.kind === "field") return names[token.uid] || "已失效的属性";
    if (token.kind === "system") return SYSTEM_LABEL[token.key] || token.key;
    if (token.kind === "aggregate") {
      return `SUM( ${(token.tokens || []).map(label).join(" ")} )`;
    }
    return SYMBOL_TEXT[token.value] || token.value;
  }
  return (tokens || []).map(label).join(" ");
}

function create({
  fields = [], skuFields = [], excludeUid = null, tokens = [],
  mount = null, target = "sku", allowAggregate = null,
} = {}) {
  const canAggregate = allowAggregate === null ? target === "spu" : allowAggregate;
  const pickable = referable(fields, excludeUid);
  const systems = SYSTEMS[target] || SYSTEMS.sku;
  const nameOf = {};
  pickable.concat(referable(skuFields)).forEach((field) => { nameOf[field.uid] = field.name; });
  let state = cloneTokens(tokens);

  const wrap = document.createElement("div");
  wrap.className = "fx-editor";
  wrap.innerHTML = `
    <div class="fx-preview">
      <div class="fx-pane-title">公式预览</div>
      <div class="fx-preview-text" data-fx-preview></div>
    </div>
    <div class="fx-palette">
      <div class="fx-pane">
        <div class="fx-pane-title">可引用属性</div>
        <div class="fx-chips" data-fx-fields>
          ${pickable.map((field) => `<button type="button" class="fx-chip" data-fx-field="${UI.esc(field.uid)}">` +
            `${UI.esc(field.name)}${field.computed ? ` <span class="fx-tag">计算</span>` : ""}</button>`).join("") ||
            `<div class="hint">当前归属还没有可引用的数值属性</div>`}
        </div>
        <div class="fx-pane-title" style="margin-top:.6rem">系统属性</div>
        <div class="fx-chips">
          ${systems.map(([key, label]) =>
            `<button type="button" class="fx-chip" data-fx-sys="${key}">${UI.esc(label)}</button>`).join("")}
        </div>
      </div>
      <div class="fx-pane">
        <div class="fx-pane-title">运算符</div>
        <div class="fx-chips">
          ${SYMBOLS.map(([value, label]) =>
            `<button type="button" class="fx-chip fx-op" data-fx-sym="${value}">${label}</button>`).join("")}
        </div>
        <div class="fx-pane-title" style="margin-top:.6rem">数字常量</div>
        <div class="fx-num">
          <input data-fx-number autocomplete="off">
          <button type="button" class="btn-outline btn-sm" data-fx-addnum>加入数字</button>
        </div>
        <div class="hint" data-fx-numhint>${NUMBER_HINT}</div>
      </div>
    </div>
    <div class="fx-actions">
      <button type="button" class="btn-outline btn-sm" data-fx-clear>清空公式</button>
      ${canAggregate ? `<button type="button" class="btn-outline btn-sm" data-fx-sum>＋ 对子 SKU 求和</button>` : ""}
    </div>`;

  const preview = wrap.querySelector("[data-fx-preview]");
  const numInput = wrap.querySelector("[data-fx-number]");
  const numHint = wrap.querySelector("[data-fx-numhint]");

  function labelOf(token) {
    if (token.kind === "number") return token.value;
    if (token.kind === "field") return nameOf[token.uid] || "已失效的属性";
    if (token.kind === "system") return SYSTEM_LABEL[token.key] || token.key;
    if (token.kind === "aggregate") {
      return `SUM( ${(token.tokens || []).map(labelOf).join(" ")} )`;
    }
    return SYMBOL_TEXT[token.value] || token.value;
  }

  function textOf() {
    return state.map(labelOf).join(" ");
  }

  function renderPreview() {
    if (!state.length) {
      preview.innerHTML = `<span class="muted">（尚未加入任何内容：请从下方选择属性、系统属性、运算符或数字常量）</span>`;
      return;
    }
    preview.innerHTML = state.map((token, index) => {
      const edit = token.kind === "aggregate"
        ? `<button type="button" class="fx-tok-edit" data-fx-edit="${index}" title="编辑求和表达式">编辑</button>`
        : "";
      return `<span class="fx-tok-wrap"><span class="fx-tok">${UI.esc(labelOf(token))}</span>${edit}` +
        `<button type="button" class="fx-tok-x" data-fx-del="${index}" ` +
        `title="删除「${UI.esc(labelOf(token))}」" aria-label="删除此项">×</button></span>`;
    }).join("");
  }

  function add(token) {
    numHint.textContent = NUMBER_HINT;
    numHint.classList.remove("fx-warn");
    state.push(token);
    renderPreview();
  }

  function addNumber() {
    const raw = numInput.value.trim();
    if (!NUMBER_RE.test(raw)) {
      numHint.textContent = "数字常量只能是普通十进制整数或小数（如 12、-3.5），不支持科学计数法或算式";
      numHint.classList.add("fx-warn");
      numInput.focus();
      return;
    }
    add({ kind: "number", value: raw });
    numInput.value = "";
    numInput.focus();
  }

  function editSum(index = null) {
    return new Promise((resolve) => {
      const existing = index === null ? [] : state[index].tokens || [];
      const body = document.createElement("div");
      body.className = "dlg-form dlg-form-wide";
      body.innerHTML = `
        <div class="hint" style="margin-bottom:.5rem">对每个子 SKU 计算下列表达式后求和；可引用单品数值属性与库存数量。</div>
        <div data-sum-editor></div>
        <div data-sum-result></div>`;
      const { dlg, foot } = UI.baseDialog(index === null ? "对子 SKU 求和" : "编辑求和表达式", body);
      const nested = create({
        fields: skuFields, target: "sku", allowAggregate: false,
        tokens: existing, mount: body.querySelector("[data-sum-editor]"),
      });
      const cancel = UI.mkBtn("取消", "btn-outline");
      const confirm = UI.mkBtn(index === null ? "加入求和" : "保存求和");
      cancel.type = confirm.type = "button";
      foot.append(cancel, confirm);
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        resolve(value);
      };
      cancel.onclick = () => dlg.close();
      dlg.addEventListener("close", () => {
        dlg.remove();
        finish(null);
      });
      confirm.onclick = () => {
        if (nested.isEmpty()) {
          body.querySelector("[data-sum-result]").innerHTML =
            UI.warnHtml("求和表达式还是空的", "请至少加入一个单品属性、库存数量或数字。");
          return;
        }
        finish({ kind: "aggregate", op: "sum", tokens: nested.getTokens() });
        dlg.close();
      };
      dlg.showModal();
    });
  }

  preview.addEventListener("click", async (event) => {
    const del = event.target.closest("[data-fx-del]");
    if (del) {
      state.splice(Number(del.dataset.fxDel), 1);
      renderPreview();
      return;
    }
    const edit = event.target.closest("[data-fx-edit]");
    if (!edit) return;
    const index = Number(edit.dataset.fxEdit);
    const aggregate = await editSum(index);
    if (aggregate) {
      state[index] = aggregate;
      renderPreview();
    }
  });
  wrap.querySelector("[data-fx-fields]").addEventListener("click", (event) => {
    const button = event.target.closest("[data-fx-field]");
    if (button) add({ kind: "field", uid: button.dataset.fxField });
  });
  wrap.addEventListener("click", (event) => {
    const system = event.target.closest("[data-fx-sys]");
    if (system) {
      add({ kind: "system", key: system.dataset.fxSys });
      return;
    }
    const symbol = event.target.closest("[data-fx-sym]");
    if (symbol) {
      add({ kind: "symbol", value: symbol.dataset.fxSym });
      return;
    }
    if (event.target.closest("[data-fx-addnum]")) addNumber();
  });
  wrap.querySelector("[data-fx-clear]").addEventListener("click", () => {
    state = [];
    renderPreview();
  });
  const sumButton = wrap.querySelector("[data-fx-sum]");
  if (sumButton) {
    sumButton.addEventListener("click", async () => {
      const aggregate = await editSum();
      if (aggregate) add(aggregate);
    });
  }
  numInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    event.stopPropagation();
    addNumber();
  });

  renderPreview();
  if (mount) mount.appendChild(wrap);
  return {
    wrap,
    getTokens: () => cloneTokens(state),
    isEmpty: () => state.length === 0,
    text: textOf,
    version: () => versionOf(state),
  };
}

export const FormulaEditor = {
  create, referable, formatTokens, versionOf, SYMBOL_TEXT,
};
