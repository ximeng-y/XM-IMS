/* 单品（SKU）管理页：搜索筛选分页 / 编辑（改名、换挂 SPU、改值/清除值）
   新建走共享组件 components/item-form.js 的「新建单品」小窗（与库存页操作区同一实现） */
"use strict";

const LIMIT = 50;
let offset = 0;
let pageTotal = 0;   // 最近一次加载的总条数（删除后据此回退分页）
let fieldsMeta = [];   // 自定义字段定义
let spusList = [];     // SPU 列表（与新建单品小窗内的组合框共享引用）

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
    pageTotal = data.total;
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
    extraHead + `<th style="width:9rem">操作</th></tr>`;
  if (!data.items.length) {
    tbody.innerHTML = `<tr><td colspan="${5 + fieldsMeta.length}" class="t-empty">没有符合条件的单品</td></tr>`;
    return;
  }
  tbody.innerHTML = data.items.map((it, idx) => {
    // 计算字段与普通字段同列展示：普通字段取手填值，计算字段取后端算出的结果文本/错误
    const extra = fieldsMeta.map((f) => `<td>${
      ItemForm.isComputed(f) ? ItemForm.calcHtml(it.calculations?.[f.name])
                             : UI.fmtVal(it.values[f.name])}</td>`).join("");
    return `<tr>
      <td class="num mono">${UI.esc(it.id)}</td>
      <td>${UI.esc(it.name)}</td>
      <td>${UI.esc(it.spu_name)}<span class="muted mono" style="font-size:.75rem"> #${it.spu_id}</span></td>
      <td class="num">${it.qty}</td>
      ${extra}
      <td class="row-actions">
        <button type="button" class="btn-outline btn-sm" data-idx="${idx}">编辑</button>
        <button type="button" class="btn-outline btn-sm btn-danger-outline" data-del="${idx}">删除</button>
      </td>
    </tr>`;
  }).join("");
  $$("button[data-idx]", tbody).forEach((b) =>
    b.addEventListener("click", () => {
      try { editFlow(data.items[Number(b.dataset.idx)]); }
      catch (e) { UI.fail("打开编辑对话框失败：" + (e.message || e)); console.error(e); }
    }));
  $$("button[data-del]", tbody).forEach((b) =>
    b.addEventListener("click", () => deleteFlow(data.items[Number(b.dataset.del)])));
}

// ---------- 删除（仅限从未产生过流水的单品） ----------
async function deleteFlow(item) {
  const confirmed = await UI.confirmDialog({
    title: `删除单品 ${item.id}`,
    bodyHtml: `<p>将删除单品 <b>${UI.esc(item.spu_name)}-${UI.esc(item.name)}</b>` +
              `（当前库存 ${item.qty}）及其全部自定义字段值，<b style="color:var(--danger)">操作不可恢复</b>。</p>` +
              `<p class="hint">已有库存流水的单品不能删除：流水是不可变账本，` +
              `录错请用反向操作或盘点调整修正。</p>`,
    confirmText: "确认删除", danger: true,
  });
  if (!confirmed) return;
  try {
    const res = await XM.del(`/items/${encodeURIComponent(item.id)}?confirm=true`);
    UI.ok(`单品 ${res.item}「${res.name}」已删除` +
          (res.deleted_values ? `，连带清除 ${res.deleted_values} 个字段值` : ""),
          { title: "ITEM_DELETED" });
    // 删除的是当前页最后一条时回退一页，避免停留在空页
    if (pageTotal === 1 && offset > 0) offset = Math.max(0, offset - LIMIT);
    loadItems();
  } catch (e) { UI.apiFail(e, "删除失败"); }
}

// ---------- 新建（共享小窗） ----------
function openCreateDialog() {
  ItemForm.openCreateItem({
    fields: fieldsMeta,
    spus: spusList,
    onCreated: () => { offset = 0; loadItems(); },
    onSpuCreated: () => fillSpuSelects(),
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
      ${fieldsMeta.filter((f) => !ItemForm.isComputed(f))
          .map((f) => ItemForm.fieldInputHtml(f, item.values[f.name], { withClear: true })).join("") ||
        `<div class="hint">该店铺还没有可填写的自定义字段</div>`}
    </div>
    ${fieldsMeta.some(ItemForm.isComputed) ? `
      <div class="hint" style="margin:.9rem 0 .4rem">计算字段（值由公式自动算出，不可手填或清除）</div>
      <div class="form-grid">
        ${fieldsMeta.filter(ItemForm.isComputed)
            .map((f) => ItemForm.computedRowHtml(f, item.calculations?.[f.name])).join("")}
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
    fieldsMeta.filter((f) => !ItemForm.isComputed(f)).forEach((f) => {
      const el = wrap.querySelector(`[data-fld="${CSS.escape(f.name)}"]`);
      const clearEl = wrap.querySelector(`[data-clear="${CSS.escape(f.name)}"]`);
      if (clearEl && clearEl.checked) { values[f.name] = null; return; }
      const orig = item.values[f.name];
      const origStr = f.type === "bool" ? (valStr(orig) || "") : String(orig ?? "");
      const v = ItemForm.readFieldInput(f, el);
      if (v === null) { bad = true; return; }
      if (v !== undefined && el.value.trim() !== origStr) values[f.name] = v;
    });
    if (bad) { UI.fail("字段值格式不正确"); return; }
    if (Object.keys(values).length) body.values = values;
    if (!Object.keys(body).length) { UI.toast("没有任何修改"); done = true; dlg.close(); return; }
    try {
      const updated = await XM.patch(`/items/${encodeURIComponent(item.id)}`, body);
      // 换挂 SPU 会改变单品编号，提示时以新编号为准
      const refChanged = updated.id !== item.id;
      UI.ok(`单品 ${item.id} 已更新` + (refChanged ? `，编号变为 ${updated.id}` : ""),
            { title: "ITEM_UPDATED" });
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

  $("#btnNewItem").addEventListener("click", openCreateDialog);
  $("#btnFilter").addEventListener("click", () => { offset = 0; loadItems(); });
  $("#fltQ").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { offset = 0; loadItems(); }
  });
  $("#fltSpu").addEventListener("change", () => { offset = 0; loadItems(); });
  loadItems();
};
