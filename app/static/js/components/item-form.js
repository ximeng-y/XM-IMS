/* 共享组件：单品（SKU）/ SPU 创建小窗
   供「库存」页操作区与单品页共用，避免字段值控件、SPU 可搜索组合框各写一份。
   SPU 列表由调用方传入数组（引用共享），会话内新建的 SPU 会直接进入该数组。

   数学计算字段：值由后端按公式算出，不可手填与清除。故创建表单不为其渲染输入控件，
   编辑对话框与列表只读呈现计算结果（calculations 给出格式化文本与单元格错误）。 */
"use strict";

const ItemForm = (() => {
  const esc = UI.esc;

  const isComputed = (f) => !!(f && f.computed);

  // ---------- 计算结果只读呈现 ----------
  // calc：后端 ItemOut.calculations 中的一项 {display, error}；缺失表示后端未给出该字段
  function calcHtml(calc) {
    if (!calc) return `<span class="muted">—</span>`;
    if (calc.error) {
      const tip = esc([calc.error.message, calc.error.hint].filter(Boolean).join("；"));
      return `<span class="badge red" title="${tip}">${esc(calc.error.code)}</span>`;
    }
    return `<span class="mono">${esc(calc.display)}</span>`;
  }

  // 编辑对话框中的计算字段只读区：结果正常时紧凑并排，出错时在下方展开消息与提示
  function computedRowHtml(f, calc) {
    const body = calc && calc.error
      ? `<div class="calc-block">${calcHtml(calc)}<span class="calc-msg">${
          esc([calc.error.message, calc.error.hint].filter(Boolean).join("；"))}</span></div>`
      : calcHtml(calc);
    return `<div>
      <label>${esc(f.name)} <span class="badge gray">计算</span></label>
      <div class="calc-cell">${body}</div>    </div>`;
  }

  // ---------- 自定义字段值输入控件 ----------
  function fieldInputHtml(f, value, { withClear = false } = {}) {
    const name = esc(f.name);
    let input;
    if (f.type === "string") {
      input = `<input data-fld="${name}" type="text" value="${esc(value ?? "")}" autocomplete="off">`;
    } else if (f.type === "bool") {
      const v = value === true ? "yes" : value === false ? "no" : "";
      input = `<select data-fld="${name}">
        <option value="" ${v === "" ? "selected" : ""}>（不设置）</option>
        <option value="yes" ${v === "yes" ? "selected" : ""}>是</option>
        <option value="no" ${v === "no" ? "selected" : ""}>否</option>
      </select>`;
    } else if (f.type === "int") {
      input = `<input data-fld="${name}" type="number" step="1" ${f.allow_negative ? "" : 'min="0"'} value="${value ?? ""}">`;
    } else if (f.type === "decimal") {
      const step = Math.pow(10, -f.scale);
      input = `<input data-fld="${name}" type="number" step="${step}" ${f.allow_negative ? "" : 'min="0"'} value="${value ?? ""}">`;
    } else { // percent：显示语义为百分数（25 = 25%）
      input = `<input data-fld="${name}" type="number" step="0.01" ${f.allow_negative ? "" : 'min="0"'} value="${value ?? ""}">`;
    }
    const typeHint = f.type === "percent" ? "（百分数，如 25）" :
                     f.type === "decimal" ? `（${f.scale} 位小数）` : "";
    const clear = withClear
      ? `<span class="form-check" style="padding:0"><input type="checkbox" data-clear="${name}"><label style="margin:0;font-size:.78rem">清除</label></span>`
      : "";
    return `<div>
      <label>${name}${typeHint ? ` <span class="muted" style="font-size:.75rem">${typeHint}</span>` : ""}</label>
      <div style="display:flex;gap:.5rem;align-items:center">${input}${clear}</div>
    </div>`;
  }

  // 从输入控件读值：返回 JS 值；空返回 undefined（表示不提交）
  function readFieldInput(f, el) {
    const raw = el.value.trim();
    if (raw === "") return undefined;
    if (f.type === "bool") return el.value === "yes";
    if (f.type === "string") return raw;
    const n = Number(raw);
    if (!Number.isFinite(n)) return null; // 交由后端校验报错，验证 422/400 结构
    return n;
  }

  // 新建表单的字段值区：只渲染可手填字段，计算字段不出现（没有可填的值）
  function fieldsGridHtml(fields) {
    const plain = fields.filter((f) => !isComputed(f));
    return plain.length
      ? plain.map((f) => fieldInputHtml(f, undefined)).join("")
      : `<div class="hint">该店铺还没有可填写的自定义字段；可先到「字段」页创建（如：成本 / 售价）</div>`;
  }

  // ---------- 新建 SPU 小窗 ----------
  // 成功返回 SPU 对象，取消或失败返回 null
  async function openCreateSpu({ initialName = "" } = {}) {
    const name = await UI.promptDialog({
      title: "新建 SPU", label: "SPU 名称", value: initialName,
      submitText: "创建", placeholder: "如：纯棉 T 恤", maxLength: 200,
    });
    if (name === null) return null;
    try {
      const spu = await XM.post("/spus", { name });
      UI.ok(`SPU「${name}」已创建（#${spu.id}）`, { title: "SPU_CREATED" });
      return spu;
    } catch (e) { UI.apiFail(e, "创建 SPU 失败"); return null; }
  }

  // ---------- SPU 可搜索组合框 ----------
  // 在容器内构建「输入框 + 下拉」；返回 { wrap, input, getSelected, destroy }
  // initialId 非空时预选该 SPU（库存页右键 SPU 行「在此 SPU 下新建单品」用）
  function buildSpuCombo(spus, { onSpuCreated, initialId = null } = {}) {
    const wrap = document.createElement("div");
    wrap.className = "spu-combo";
    wrap.innerHTML = `<input placeholder="点击展开全部 SPU，或输入名称即时过滤" autocomplete="off">
      <div class="spu-combo-list"></div>`;
    const input = wrap.querySelector("input");
    const list = wrap.querySelector(".spu-combo-list");
    let selected = null; // 组合框当前选中的 SPU

    // 渲染下拉列表：按输入子串过滤（不区分大小写）；首项固定为「新建 SPU」
    function renderList() {
      const raw = input.value.trim();
      const q = raw.toLowerCase();
      const hits = spus.filter((s) => s.name.toLowerCase().includes(q));
      list.innerHTML =
        `<div class="spu-combo-opt spu-new" data-new>＋ 新建 SPU${raw ? `「${esc(raw)}」` : ""}</div>` +
        hits.map((s) =>
          `<div class="spu-combo-opt" data-id="${s.id}">${esc(s.name)}<span class="muted mono"> #${s.id}</span></div>`).join("") +
        (hits.length ? "" :
          `<div class="spu-combo-opt" style="cursor:default;color:var(--ink-soft)">没有匹配的 SPU，可点击上方「新建 SPU」</div>`);
    }
    const openList = () => { renderList(); list.classList.add("open"); };
    const closeList = () => list.classList.remove("open");
    function choose(spu) {
      selected = spu;
      input.value = spu.name;
      closeList();
    }
    if (initialId !== null) {
      const preset = spus.find((s) => s.id === initialId);
      if (preset) choose(preset);
    }

    // 快捷新建 SPU：小窗内创建后立即选中，不离开当前页
    async function newSpuDialog() {
      const spu = await openCreateSpu({ initialName: input.value.trim() });
      if (!spu) return;
      spus.push(spu);
      if (onSpuCreated) onSpuCreated(spu);
      choose(spu);
    }

    input.addEventListener("focus", openList);
    input.addEventListener("input", () => { selected = null; openList(); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { closeList(); return; }
      if (e.key !== "Enter") return;
      // 回车在此输入框内一律阻止隐式整表提交：快捷新建 SPU 弹窗关闭后焦点会回落到
      // 此处，随后到来的回车（连击或再按一次）不应被当成「创建单品」表单提交
      e.preventDefault();
      if (!list.classList.contains("open")) return;
      // 已选 SPU 后重新聚焦时，回车只收起候选，不把选择改成首项
      if (selected) { closeList(); return; }
      const first = list.querySelector(".spu-combo-opt[data-id]");
      if (first) choose(spus.find((s) => s.id === Number(first.dataset.id)));
      else newSpuDialog();
    });
    list.addEventListener("click", (e) => {
      const opt = e.target.closest(".spu-combo-opt");
      if (!opt) return;
      if (opt.hasAttribute("data-new")) newSpuDialog();
      else choose(spus.find((s) => s.id === Number(opt.dataset.id)));
    });
    // 点击组合框外部收起下拉；小窗关闭时须调用 destroy 摘掉该全局监听
    const onDocClick = (e) => { if (!wrap.contains(e.target)) closeList(); };
    document.addEventListener("click", onDocClick);

    return {
      wrap, input,
      getSelected: () => selected,
      destroy: () => document.removeEventListener("click", onDocClick),
    };
  }

  // ---------- 新建单品（SKU）小窗 ----------
  // fields：自定义字段定义数组；spus：SPU 数组（引用共享）
  // onCreated(item)：创建成功后回调；onSpuCreated(spu)：窗内快捷新建 SPU 后回调
  function openCreateItem({ fields = [], spus = [], onCreated, onSpuCreated, initialSpuId = null } = {}) {
    const wrap = document.createElement("div");
    wrap.className = "dlg-form dlg-form-wide";
    wrap.innerHTML = `
      <div class="form-grid">
        <div>
          <label>SKU 名称 <span class="fieldreq">*</span></label>
          <input data-f="name" maxlength="200" placeholder="如：白 / L 码" autocomplete="off">
        </div>
        <div style="grid-column:1/-1">
          <label>SPU 名称 <span class="fieldreq">*</span></label>
          <div data-combo></div>
        </div>
      </div>
      <div class="form-grid" style="margin-top:.9rem" data-values></div>
      <div class="hint">留空的字段值不会提交${fields.some(isComputed)
        ? "；计算字段的值由公式自动算出，此处无需填写" : ""}</div>
      <div data-result></div>`;
    const { dlg, foot } = UI.baseDialog("新建单品", wrap);

    const nameEl = wrap.querySelector('[data-f="name"]');
    const valueGrid = wrap.querySelector("[data-values]");
    const resultBox = wrap.querySelector("[data-result]");
    valueGrid.innerHTML = fieldsGridHtml(fields);
    const combo = buildSpuCombo(spus, { onSpuCreated, initialId: initialSpuId });
    wrap.querySelector("[data-combo]").appendChild(combo.wrap);

    let done = false;
    const cancel = UI.mkBtn("取消", "btn-outline"), submit = UI.mkBtn("创建单品");
    cancel.type = submit.type = "button";
    foot.append(cancel, submit);
    cancel.onclick = () => { done = true; dlg.close(); };
    dlg.addEventListener("close", () => { done = true; combo.destroy(); dlg.remove(); });

    submit.onclick = () => {
      const name = nameEl.value.trim();
      if (!name) { UI.fail("请填写 SKU 名称"); return; }
      const spu = combo.getSelected();
      if (!spu) { UI.fail("请选择 SPU，或点击下拉首项「新建 SPU」快速创建"); return; }
      const body = { name, spu_id: spu.id, values: {} };
      let bad = false;
      UI.$$("[data-fld]", valueGrid).forEach((el) => {
        const f = fields.find((x) => x.name === el.dataset.fld);
        const v = readFieldInput(f, el);
        if (v === null) bad = true;
        else if (v !== undefined) body.values[f.name] = v;
      });
      if (bad) { UI.fail("字段值格式不正确"); return; }
      UI.withBusy(submit, async () => {
        try {
          const it = await XM.post("/items", body);
          resultBox.className = "result-box";
          resultBox.innerHTML =
            `<div class="headline"><span class="badge">ITEM_CREATED</span> ` +
            `${esc(it.id)}「${esc(it.name)}」已创建，当前库存 ${it.qty}</div>`;
          // 清空名称与字段值，方便连续录入同 SPU 下的多个 SKU
          nameEl.value = "";
          valueGrid.innerHTML = fieldsGridHtml(fields);
          nameEl.focus();
          if (onCreated) onCreated(it);
        } catch (e) {
          resultBox.className = "result-box error-box";
          resultBox.innerHTML = UI.apiFailHtml(e);
        }
      });
    };

    dlg.showModal();
    setTimeout(() => nameEl.focus(), 50);
  }

  return { fieldInputHtml, readFieldInput, fieldsGridHtml, calcHtml, computedRowHtml,
           isComputed, openCreateSpu, openCreateItem };
})();
