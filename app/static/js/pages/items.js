/* 单品（SKU）管理页：动态字段表单 / 搜索筛选分页 / 编辑（改名、换挂 SPU、改值/清除值） */
"use strict";

const LIMIT = 50;
let offset = 0;
let fieldsMeta = [];   // 自定义字段定义
let spusList = [];

// ---------- 自定义字段值输入控件 ----------
function fieldInputHtml(f, value, { withClear = false } = {}) {
  const name = UI.esc(f.name);
  let input;
  if (f.type === "string") {
    input = `<input data-fld="${name}" type="text" value="${UI.esc(value ?? "")}" autocomplete="off">`;
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
  if (isNaN(n)) return null; // 交由后端校验报错，验证 422/400 结构
  return n;
}

// ---------- 数据加载 ----------
async function loadMeta() {
  const [fields, spus] = await Promise.all([XM.get("/fields"), XM.get("/spus")]);
  fieldsMeta = fields.fields.filter((f) => !f.system);
  spusList = spus.spus;
}

function fillSpuSelects() {
  const opts = spusList.map((s) => `<option value="${s.id}">${UI.esc(s.name)}（#${s.id}）</option>`).join("");
  const flt = $("#fltSpu");
  const cur = flt.value;
  flt.innerHTML = `<option value="">全部 SPU</option>` + opts;
  flt.value = cur;
}

async function loadItems() {
  const tbody = $("#itemRows");
  const q = $("#fltQ").value.trim();
  const spu = $("#fltSpu").value;
  const params = new URLSearchParams({ offset: String(offset), limit: String(LIMIT) });
  if (q) params.set("q", q);
  if (spu) params.set("spu_id", spu);
  try {
    const data = await XM.get("/items?" + params);
    renderTable(data);
    UI.renderPager("#itemPager", { offset, limit: LIMIT, total: data.total },
      (go) => { offset = go; loadItems(); });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="9" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
    UI.apiFail(e);
  }
}

function renderTable(data) {
  const tbody = $("#itemRows");
  // 表头：自定义字段列插在库存之后
  const extraHead = fieldsMeta.map((f) => `<th>${UI.esc(f.name)}</th>`).join("");
  $("#itemHead").innerHTML =
    `<tr><th class="num">ID</th><th>名称</th><th>SPU</th><th class="num">库存</th>` +
    extraHead + `<th style="width:5.625rem">操作</th></tr>`;
  if (!data.items.length) {
    tbody.innerHTML = `<tr><td colspan="${5 + fieldsMeta.length}" class="t-empty">没有符合条件的单品</td></tr>`;
    return;
  }
  tbody.innerHTML = data.items.map((it, idx) => {
    const extra = fieldsMeta.map((f) => `<td>${UI.fmtVal(it.values[f.name])}</td>`).join("");
    return `<tr>
      <td class="num mono">${it.id}</td>
      <td>${UI.esc(it.name)}</td>
      <td>${UI.esc(it.spu_name)}<span class="muted mono" style="font-size:.75rem"> #${it.spu_id}</span></td>
      <td class="num">${it.qty}</td>
      ${extra}
      <td><button type="button" class="btn-outline btn-sm" data-idx="${idx}">编辑</button></td>
    </tr>`;
  }).join("");
  $$("button[data-idx]", tbody).forEach((b) =>
    b.addEventListener("click", () => {
      try { editFlow(data.items[Number(b.dataset.idx)]); }
      catch (e) { UI.fail("打开编辑对话框失败：" + (e.message || e)); console.error(e); }
    }));
}

// ---------- 新建：SPU 可搜索组合框 ----------
function setupCreateForm() {
  const input = $("#itSpuInput"), list = $("#itSpuList");
  let selectedSpu = null; // 组合框当前选中的 SPU

  // 渲染下拉列表：按输入子串过滤（不区分大小写）；首项固定为「新建 SPU」
  function renderSpuList() {
    const raw = input.value.trim();
    const q = raw.toLowerCase();
    const hits = spusList.filter((s) => s.name.toLowerCase().includes(q));
    list.innerHTML =
      `<div class="spu-combo-opt spu-new" data-new>＋ 新建 SPU${raw ? `「${UI.esc(raw)}」` : ""}</div>` +
      hits.map((s) =>
        `<div class="spu-combo-opt" data-id="${s.id}">${UI.esc(s.name)}<span class="muted mono"> #${s.id}</span></div>`).join("") +
      (hits.length ? "" :
        `<div class="spu-combo-opt" style="cursor:default;color:var(--ink-soft)">没有匹配的 SPU，可点击上方「新建 SPU」</div>`);
  }
  const openList = () => { renderSpuList(); list.classList.add("open"); };
  const closeList = () => list.classList.remove("open");

  function choose(spu) {
    selectedSpu = spu;
    input.value = spu.name;
    closeList();
  }

  // 快捷新建 SPU：弹窗内创建后立即选中，不离开当前页
  async function newSpuDialog() {
    const name = await UI.promptDialog({
      title: "新建 SPU", label: "SPU 名称", value: input.value.trim(),
      submitText: "创建", placeholder: "如：纯棉 T 恤", maxLength: 200,
    });
    if (name === null) return;
    try {
      const spu = await XM.post("/spus", { name });
      UI.ok(`SPU「${name}」已创建（#${spu.id}）`, { title: "SPU_CREATED" });
      spusList.push(spu);
      fillSpuSelects();
      choose(spu);
    } catch (e) { UI.apiFail(e, "创建 SPU 失败"); }
  }

  input.addEventListener("focus", openList);
  input.addEventListener("input", () => { selectedSpu = null; openList(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeList(); return; }
    if (e.key !== "Enter") return;
    // 回车在此输入框内一律阻止隐式整表提交：快捷新建 SPU 弹窗关闭后焦点会回落到
    // 此处，随后到来的回车（连击或再按一次）不应被当成「创建单品」表单提交
    e.preventDefault();
    if (!list.classList.contains("open")) return;
    const first = list.querySelector(".spu-combo-opt[data-id]");
    if (first) choose(spusList.find((s) => s.id === Number(first.dataset.id)));
    else newSpuDialog();
  });
  list.addEventListener("click", (e) => {
    const opt = e.target.closest(".spu-combo-opt");
    if (!opt) return;
    if (opt.hasAttribute("data-new")) newSpuDialog();
    else choose(spusList.find((s) => s.id === Number(opt.dataset.id)));
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#itSpuCombo")) closeList();
  });

  const grid = $("#itValueGrid");
  grid.innerHTML = fieldsMeta.length
    ? fieldsMeta.map((f) => fieldInputHtml(f, undefined)).join("")
    : `<div class="hint">该店铺还没有自定义字段；可先到「字段」页创建（如：成本 / 售价）</div>`;

  $("#fCreate").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const btn = ev.target.querySelector("button[type=submit]");
    const name = $("#itName").value.trim();
    if (!name) { UI.fail("请填写 SKU 名称"); return; }
    if (!selectedSpu) { UI.fail("请选择 SPU，或点击下拉首项「新建 SPU」快速创建"); return; }
    const body = { name, spu_id: selectedSpu.id, values: {} };
    let bad = false;
    $$("[data-fld]", grid).forEach((el) => {
      const f = fieldsMeta.find((x) => x.name === el.dataset.fld);
      const v = readFieldInput(f, el);
      if (v === null) bad = true;
      else if (v !== undefined) body.values[f.name] = v;
    });
    if (bad) { UI.fail("字段值格式不正确"); return; }
    UI.withBusy(btn, async () => {
      try {
        const it = await XM.post("/items", body);
        UI.resultBox("#createResult", {
          html: `<div class="headline"><span class="badge">ITEM_CREATED</span> ` +
                `#${it.id}「${UI.esc(it.name)}」已创建，当前库存 ${it.qty}</div>`,
        });
        $("#itName").value = "";
        grid.innerHTML = fieldsMeta.map((f) => fieldInputHtml(f, undefined)).join("");
        offset = 0; loadItems();
      } catch (e) {
        UI.resultBox("#createResult", { error: true, html: UI.apiFailHtml(e) });
      }
    });
  });
}

// ---------- 编辑（PATCH：改名 / 换挂 SPU / 改值，null = 清除） ----------
function editFlow(item) {
  let done = false;
  const wrap = document.createElement("div");
  const valStr = (v) => v === true ? "yes" : v === false ? "no" : (v ?? "");
  wrap.innerHTML = `
    <div class="form-grid" style="grid-template-columns:1fr 1fr">
      <div><label>SKU 名称</label>
        <input id="edName" value="${UI.esc(item.name)}" maxlength="200"></div>
      <div><label>SPU 归属</label>
        <select id="edSpuMode">
          <option value="keep">（保持：${UI.esc(item.spu_name)}）</option>
          <option value="pick">选择已有 SPU</option>
          <option value="name">输入名称（不存在则自动创建）</option>
        </select></div>
      <div id="edSpuPickBox" style="display:none"><label>已有 SPU</label>
        <select id="edSpuPick">${spusList.map((s) =>
          `<option value="${s.id}" ${s.id === item.spu_id ? "selected" : ""}>${UI.esc(s.name)}（#${s.id}）</option>`).join("")}
        </select></div>
      <div id="edSpuNameBox" style="display:none"><label>SPU 名称（新）</label>
        <input id="edSpuName" maxlength="200"></div>
    </div>
    <div class="hint" style="margin:.9rem 0 .4rem">自定义字段值（勾选「清除」= 提交 null 清除该值）</div>
    <div class="form-grid">
      ${fieldsMeta.map((f) => fieldInputHtml(f, item.values[f.name], { withClear: true })).join("") ||
        `<div class="hint">该店铺还没有自定义字段</div>`}
    </div>`;
  const { dlg, foot } = UI.baseDialog(`编辑单品 #${item.id}`, wrap);

  const mode = $("#edSpuMode", wrap);
  mode.addEventListener("change", () => {
    $("#edSpuPickBox", wrap).style.display = mode.value === "pick" ? "" : "none";
    $("#edSpuNameBox", wrap).style.display = mode.value === "name" ? "" : "none";
  });

  const save = UI.mkBtn("保存修改"), cancel = UI.mkBtn("取消", "btn-outline");
  save.type = cancel.type = "button";
  foot.append(cancel, save);
  cancel.onclick = () => { done = true; dlg.close(); };
  dlg.addEventListener("close", () => { done = true; dlg.remove(); });

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
    fieldsMeta.forEach((f) => {
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
    if (!Object.keys(body).length) { UI.toast("没有任何修改"); done = true; dlg.close(); return; }
    try {
      await XM.patch(`/items/${item.id}`, body);
      UI.ok(`单品 #${item.id} 已更新`, { title: "ITEM_UPDATED" });
      done = true; dlg.close();
      await loadMeta(); fillSpuSelects();
      loadItems();
    } catch (e) { UI.apiFail(e, "保存失败"); }
  });

  dlg.showModal();
  setTimeout(() => $("#edName", wrap).focus(), 50);
}

// ---------- 启动 ----------
window.PAGE_INIT = async function () {
  if (!UI.requireShop()) return;
  try { await loadMeta(); } catch (e) { UI.apiFail(e, "加载字段/SPU 失败"); }
  fillSpuSelects();
  setupCreateForm();

  $("#btnFilter").addEventListener("click", () => { offset = 0; loadItems(); });
  $("#fltQ").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { offset = 0; loadItems(); }
  });
  $("#fltSpu").addEventListener("change", () => { offset = 0; loadItems(); });
  loadItems();
};
