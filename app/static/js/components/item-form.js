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
  function buildSpuCombo(spus, { onSpuCreated } = {}) {
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
    // 输入了完整名称但没点候选：按名称精确匹配已有 SPU，命中即选中
    function matchByName(raw) {
      const hit = spus.find((s) => s.name === raw);
      if (hit) choose(hit);
      return hit || null;
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
      getTypedName: () => input.value.trim(),
      matchByName,
      destroy: () => document.removeEventListener("click", onDocClick),
    };
  }

  // ---------- 编辑单品（SKU）小窗 ----------
  // PATCH：改名 / 换挂 SPU / 改值（null = 清除）。单品页与库存页（重名提示里的
  // 「编辑这个当前已有的 SKU」）共用同一实现，避免两处各写一份。
  // onSaved(updated)：保存成功后回调；onSpuCreated()：需要刷新调用方 SPU 列表时回调
  function openEditItem({ item, fields = [], spus = [], onSaved, onSpuCreated } = {}) {
    const valStr = (v) => v === true ? "yes" : v === false ? "no" : (v ?? "");
    const wrap = document.createElement("div");
    wrap.innerHTML = `
      <div class="form-grid" style="grid-template-columns:1fr 1fr">
        <div><label>SKU 名称</label>
          <input id="edName" value="${esc(item.name)}" maxlength="200"></div>
        <div><label>SPU 归属</label>
          <select id="edSpuMode">
            <option value="keep">（保持：${esc(item.spu_name)}）</option>
            <option value="pick">选择已有 SPU</option>
            <option value="name">输入名称（不存在则自动创建）</option>
          </select></div>
        <div id="edSpuPickBox" style="display:none"><label>已有 SPU</label>
          <select id="edSpuPick">${spus.map((s) =>
            `<option value="${s.id}" ${s.id === item.spu_id ? "selected" : ""}>${esc(s.name)}（#${s.id}）</option>`).join("")}
          </select></div>
        <div id="edSpuNameBox" style="display:none"><label>SPU 名称（新）</label>
          <input id="edSpuName" maxlength="200"></div>
      </div>
      <div class="hint" style="margin:.9rem 0 .4rem">自定义字段值（勾选「清除」= 提交 null 清除该值）</div>
      <div class="form-grid">
        ${fields.filter((f) => !isComputed(f))
            .map((f) => fieldInputHtml(f, item.values[f.name], { withClear: true })).join("") ||
          `<div class="hint">该店铺还没有可填写的自定义字段</div>`}
      </div>
      ${fields.some(isComputed) ? `
        <div class="hint" style="margin:.9rem 0 .4rem">计算字段（值由公式自动算出，不可手填或清除）</div>
        <div class="form-grid">
          ${fields.filter(isComputed)
              .map((f) => computedRowHtml(f, item.calculations?.[f.name])).join("")}
        </div>` : ""}`;
    const { dlg, foot } = UI.baseDialog(`编辑单品 ${item.id}`, wrap);

    const mode = $("#edSpuMode", wrap);
    mode.addEventListener("change", () => {
      $("#edSpuPickBox", wrap).style.display = mode.value === "pick" ? "" : "none";
      $("#edSpuNameBox", wrap).style.display = mode.value === "name" ? "" : "none";
    });

    const save = UI.mkBtn("保存修改"), cancel = UI.mkBtn("取消", "btn-outline");
    save.type = cancel.type = "button";
    foot.append(cancel, save);
    cancel.onclick = () => { dlg.close(); };
    dlg.addEventListener("close", () => dlg.remove());

    save.onclick = () => UI.withBusy(save, async () => {
      const body = {};
      const name = $("#edName", wrap).value.trim();
      if (name && name !== item.name) body.name = name;
      if (mode.value === "pick") body.spu_id = Number($("#edSpuPick", wrap).value);
      if (mode.value === "name") {
        const sn = $("#edSpuName", wrap).value.trim();
        if (!sn) { UI.fail("请填写 SPU 名称"); return; }
        body.spu_name = sn;
      }
      const values = {};
      let bad = false;
      fields.filter((f) => !isComputed(f)).forEach((f) => {
        const el = wrap.querySelector(`[data-fld="${CSS.escape(f.name)}"]`);
        const clearEl = wrap.querySelector(`[data-clear="${CSS.escape(f.name)}"]`);
        if (clearEl && clearEl.checked) { values[f.name] = null; return; }
        const orig = item.values[f.name];
        const origStr = f.type === "bool" ? (valStr(orig) || "") : String(orig ?? "");
        const v = readFieldInput(f, el);
        if (v === null) { bad = true; return; }
        if (v !== undefined && el.value.trim() !== origStr) values[f.name] = v;
      });
      if (bad) { UI.fail("字段值格式不正确"); return; }
      if (Object.keys(values).length) body.values = values;
      if (!Object.keys(body).length) { UI.toast("没有任何修改"); dlg.close(); return; }
      try {
        const updated = await XM.patch(`/items/${encodeURIComponent(item.id)}`, body);
        // 换挂 SPU 会改变单品编号，提示时以新编号为准
        const refChanged = updated.id !== item.id;
        UI.ok(`单品 ${item.id} 已更新` + (refChanged ? `，编号变为 ${updated.id}` : ""),
              { title: "ITEM_UPDATED" });
        dlg.close();
        if (onSpuCreated) onSpuCreated();
        if (onSaved) onSaved(updated);
      } catch (e) { UI.apiFail(e, "保存失败"); }
    });

    dlg.showModal();
    setTimeout(() => $("#edName", wrap).focus(), 50);
  }

  // ---------- 新建单品（SKU）小窗 ----------
  // fields：自定义字段定义数组；spus：SPU 数组（引用共享）
  // onCreated(item)：创建成功后回调；onSpuCreated(spu)：窗内快捷新建 SPU 后回调
  function openCreateItem({ fields = [], spus = [], onCreated, onSpuCreated, onEditExisting } = {}) {
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
    const combo = buildSpuCombo(spus, { onSpuCreated });
    wrap.querySelector("[data-combo]").appendChild(combo.wrap);

    let done = false;
    const cancel = UI.mkBtn("取消", "btn-outline"), submit = UI.mkBtn("创建单品");
    cancel.type = submit.type = "button";
    foot.append(cancel, submit);
    cancel.onclick = () => { done = true; dlg.close(); };
    dlg.addEventListener("close", () => { done = true; combo.destroy(); dlg.remove(); });

    // SPU 解析：已选中直接用；只输入了完整名称则先精确匹配已有 SPU；
    // 仍不存在则询问是否直接创建（issue #29）
    async function resolveSpu() {
      let spu = combo.getSelected();
      if (!spu) {
        const typed = combo.getTypedName();
        if (!typed) { UI.fail("请选择 SPU，或点击下拉首项「新建 SPU」快速创建"); return undefined; }
        spu = combo.matchByName(typed);
        if (!spu) {
          const ok = await UI.confirmDialog({
            title: "SPU 不存在",
            bodyHtml: `<p>SPU「<b>${esc(typed)}</b>」不存在，是否直接创建？</p>` +
                      `<p class="hint">创建后会选中该 SPU，并继续创建单品。</p>`,
            confirmText: "创建并继续",
          });
          if (!ok) return undefined;
          try {
            spu = await XM.post("/spus", { name: typed });
          } catch (e) { UI.apiFail(e, "创建 SPU 失败"); return undefined; }
          spus.push(spu);
          if (onSpuCreated) onSpuCreated(spu);
          combo.matchByName(spu.name);
        }
      }
      return spu;
    }

    // SKU 重名把关（issue #33）：同一 SPU 下已存在完全相同名称时给出对照与两个操作。
    // 返回 true 表示可以继续创建；false 表示已被拦下
    async function checkDuplicate(spu, name) {
      let data;
      try {
        data = await XM.get(`/items?spu_id=${spu.id}&name=${encodeURIComponent(name)}&limit=1`);
      } catch (e) { UI.apiFail(e, "查重失败"); return false; }
      if (!data.total) return true;
      const dup = data.items[0];
      const rows = [
        ["编号", dup.id], ["名称", dup.name], ["所属 SPU", dup.spu_name],
        ["当前库存", String(dup.qty)],
        ...fields.map((f) => [f.name, isComputed(f)
          ? (dup.calculations?.[f.name]?.display ?? "—")
          : (dup.values[f.name] === undefined || dup.values[f.name] === null || dup.values[f.name] === ""
              ? "—" : String(dup.values[f.name]))]),
      ];
      const listHtml = `<div class="dup-list">` + rows.map(([k, v]) =>
        `<div class="dup-row"><span class="dup-key">${esc(k)}</span><span class="dup-val">${esc(v)}</span></div>`
      ).join("") + `</div>`;
      const edit = await new Promise((resolve) => {
        const wrap2 = document.createElement("div");
        wrap2.innerHTML =
          `<p>当前 SPU 下已有一个完全相同的 SKU 名，请查看这是否与你想要添加的单品相同，并选择对应操作。</p>` +
          listHtml;
        const { dlg: d2, foot: f2 } = UI.baseDialog("SKU 重名", wrap2);
        const back = UI.mkBtn("返回修改这个 SKU 名", "btn-outline");
        const go = UI.mkBtn("编辑这个当前已有的 SKU");
        back.type = go.type = "button";
        f2.append(back, go);
        let done = false;
        const finish = (v) => { if (!done) { done = true; d2.close(); resolve(v); } };
        back.onclick = () => finish(false);
        go.onclick = () => finish(true);
        d2.addEventListener("close", () => { finish(false); d2.remove(); });
        d2.showModal();
      });
      if (edit) {
        dlg.close();
        if (onEditExisting) onEditExisting(dup);
        else UI.fail("当前页面无法打开编辑小窗");
      } else {
        nameEl.focus();
        nameEl.select();
      }
      return false;
    }

    submit.onclick = () => UI.withBusy(submit, async () => {
      const name = nameEl.value.trim();
      if (!name) { UI.fail("请填写 SKU 名称"); return; }
      const spu = await resolveSpu();
      if (!spu) return;
      if (!await checkDuplicate(spu, name)) return;
      const body = { name, spu_id: spu.id, values: {} };
      let bad = false;
      UI.$$("[data-fld]", valueGrid).forEach((el) => {
        const f = fields.find((x) => x.name === el.dataset.fld);
        const v = readFieldInput(f, el);
        if (v === null) bad = true;
        else if (v !== undefined) body.values[f.name] = v;
      });
      if (bad) { UI.fail("字段值格式不正确"); return; }
      try {
        const it = await XM.post("/items", body);
        resultBox.className = "result-box";
        resultBox.innerHTML =
          `<div class="headline"><span class="badge">ITEM_CREATED</span> ` +
          `${esc(it.spu_name)}-${esc(it.name)} 已创建，当前库存 ${it.qty}</div>`;
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

    dlg.showModal();
    setTimeout(() => nameEl.focus(), 50);
  }

  return { fieldInputHtml, readFieldInput, fieldsGridHtml, calcHtml, computedRowHtml,
           isComputed, openCreateSpu, openCreateItem, openEditItem };
})();
