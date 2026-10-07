/* 共享组件：数学计算字段的公式编辑器
   字段页「新建字段」与「编辑公式」小窗共用。编辑器只负责把用户点击拼成结构化 token
   序列（number / field / system / symbol），语法、引用与循环校验一律交给后端：保存失败
   时调用方保留当前内容并只更新错误区域，编辑器本身不重建。
   token 形如 {kind:"field",uid:"…"}、{kind:"symbol",value:"*"}、{kind:"number",value:"3"}。 */
"use strict";

const FormulaEditor = (() => {
  const esc = UI.esc;
  const SYMBOLS = [["+", "+"], ["-", "−"], ["*", "×"], ["/", "÷"], ["(", "("], [")", ")"]];
  const SYMBOL_TEXT = { "+": "+", "-": "−", "*": "×", "/": "÷", "(": "(", ")": ")" };
  const SYSTEM = [["qty", "库存数量"], ["spu_id", "SPU ID"]];
  const NUMBER_RE = /^-?\d+(\.\d+)?$/;

  // 可引用字段：数值型自定义字段（int/decimal/percent）与其它计算字段；
  // 编辑自身时排除自己，直接自引用由后端循环检测拦下
  function referable(fields, excludeUid) {
    return (fields || []).filter((f) => !f.system && f.type !== "string" &&
      f.type !== "bool" && f.uid !== excludeUid);
  }

  function create({ fields = [], excludeUid = null, tokens = [], mount = null } = {}) {
    const pickable = referable(fields, excludeUid);
    const nameOf = {};
    pickable.forEach((f) => { nameOf[f.uid] = f.name; });
    const sysName = Object.fromEntries(SYSTEM);
    let state = (tokens || []).map((t) => ({ ...t }));

    const wrap = document.createElement("div");
    wrap.className = "fx-editor";
    wrap.innerHTML = `
      <div class="fx-preview">
        <div class="fx-pane-title">公式预览</div>
        <div class="fx-preview-text" data-fx-preview></div>
      </div>
      <div class="fx-palette">
        <div class="fx-pane">
          <div class="fx-pane-title">可引用字段</div>
          <div class="fx-chips" data-fx-fields>
            ${pickable.map((f) => `<button type="button" class="fx-chip" data-fx-field="${esc(f.uid)}">` +
              `${esc(f.name)}${f.computed ? ` <span class="fx-tag">计算</span>` : ""}</button>`).join("") ||
              `<div class="hint">该店铺还没有可引用的数值字段；请先创建整数／小数／百分比字段</div>`}
          </div>
          <div class="fx-pane-title" style="margin-top:.6rem">系统字段</div>
          <div class="fx-chips">
            ${SYSTEM.map(([k, n]) => `<button type="button" class="fx-chip" data-fx-sys="${k}">${esc(n)}</button>`).join("")}
          </div>
        </div>
        <div class="fx-pane">
          <div class="fx-pane-title">运算符</div>
          <div class="fx-chips">
            ${SYMBOLS.map(([v, t]) => `<button type="button" class="fx-chip fx-op" data-fx-sym="${v}">${t}</button>`).join("")}
          </div>
          <div class="fx-pane-title" style="margin-top:.6rem">数字常量</div>
          <div class="fx-num">
            <input data-fx-number placeholder="如 12 或 -3.5" autocomplete="off">
            <button type="button" class="btn-outline btn-sm" data-fx-addnum>加入数字</button>
          </div>
          <div class="hint" data-fx-numhint>只接受普通十进制整数或小数</div>
        </div>
      </div>
      <div class="fx-actions">
        <button type="button" class="btn-outline btn-sm" data-fx-clear>清空公式</button>
      </div>`;

    const preview = wrap.querySelector("[data-fx-preview]");
    const numInput = wrap.querySelector("[data-fx-number]");
    const numHint = wrap.querySelector("[data-fx-numhint]");

    // 人类可读的公式文本：与后端 SYMBOL_TEXT 保持同一套符号呈现
    function labelOf(t) {
      if (t.kind === "number") return t.value;
      if (t.kind === "field") return nameOf[t.uid] || `（未知字段 ${String(t.uid).slice(0, 8)}）`;
      if (t.kind === "system") return sysName[t.key] || t.key;
      return SYMBOL_TEXT[t.value] || t.value;
    }
    function textOf() {
      return state.map(labelOf).join(" ");
    }
    // 预览区按 token 渲染独立组件块（与 issue 约定的交互一致）：每块右上角带 ×，
    // 可单独删除中间任意一项。块外层再包一层容器，避免「块内标签换行时 × 被别的块
    // 的地板压住」——容器只按块分配行内空间，× 仍定位于块本身
    function renderPreview() {
      if (!state.length) {
        preview.innerHTML = `<span class="muted">（尚未加入任何内容：请从下方选择字段、系统字段、运算符或数字常量）</span>`;
        return;
      }
      preview.innerHTML = state.map((t, i) =>
        `<span class="fx-tok-wrap"><span class="fx-tok">${esc(labelOf(t))}</span>` +
        `<button type="button" class="fx-tok-x" data-fx-del="${i}"` +
        ` title="删除「${esc(labelOf(t))}」" aria-label="删除此项">×</button></span>`).join("");
    }
    function add(token) {
      numHint.textContent = "只接受普通十进制整数或小数";
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

    preview.addEventListener("click", (e) => {
      const del = e.target.closest("[data-fx-del]");
      if (!del) return;
      state.splice(Number(del.dataset.fxDel), 1);
      renderPreview();
    });
    wrap.querySelector("[data-fx-fields]").addEventListener("click", (e) => {
      const b = e.target.closest("[data-fx-field]");
      if (b) add({ kind: "field", uid: b.dataset.fxField });
    });
    wrap.addEventListener("click", (e) => {
      const sys = e.target.closest("[data-fx-sys]");
      if (sys) { add({ kind: "system", key: sys.dataset.fxSys }); return; }
      const sym = e.target.closest("[data-fx-sym]");
      if (sym) { add({ kind: "symbol", value: sym.dataset.fxSym }); return; }
      if (e.target.closest("[data-fx-addnum]")) addNumber();
    });
    wrap.querySelector("[data-fx-clear]").addEventListener("click", () => {
      state = [];
      renderPreview();
    });
    numInput.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      // 回车在此输入框内一律不冒泡到外层表单，避免误触发「创建」提交
      e.preventDefault();
      e.stopPropagation();
      addNumber();
    });

    renderPreview();
    if (mount) mount.appendChild(wrap);
    return {
      wrap,
      getTokens: () => state.map((t) => ({ ...t })),
      isEmpty: () => state.length === 0,
      // 纯文本形态（与预览同一套符号呈现），供字段列表等只读场合复用
      text: () => textOf(),
    };
  }

  return { create, referable, SYMBOL_TEXT };
})();
