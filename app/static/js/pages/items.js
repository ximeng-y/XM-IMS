/* 单品（SKU）管理页：搜索筛选分页 / 编辑（改名、换挂 SPU、改值/清除值）
   新建走共享组件 components/item-form.js 的「新建单品」小窗（与库存页操作区同一实现） */
"use strict";

const LIMIT = 50;
let offset = 0;
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
    // 以刷新后的总数校正页码，删除末页最后一条后重新加载有效页。
    const lastOffset = Math.max(0, Math.floor((data.total - 1) / LIMIT) * LIMIT);
    if (offset > lastOffset) {
      offset = lastOffset;
      return loadItems();
    }
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
    b.addEventListener("click", () => { void deleteFlow(data.items[Number(b.dataset.del)]); }));
}

// ---------- 删除（仅限从未产生过流水的单品） ----------
function deleteFlow(item) {
  return DeleteFlow.run({
    title: `删除单品 ${item.id}`,
    bodyHtml: `<p>将删除单品 <b>${UI.esc(item.spu_name)}-${UI.esc(item.name)}</b>` +
              `（当前库存 ${item.qty}）及其全部自定义字段值，<b style="color:var(--danger)">操作不可恢复</b>。</p>` +
              `<p class="hint">已有库存流水的单品不能删除：流水是不可变账本，` +
              `录错请用反向操作或盘点调整修正。</p>`,
    path: `/items/${encodeURIComponent(item.id)}?confirm=true`,
    okText: (res) => `单品 ${res.item}「${res.name}」已删除` +
                     (res.deleted_values ? `，连带清除 ${res.deleted_values} 个字段值` : ""),
    onDone: loadItems,
  });
}

// ---------- 新建（共享小窗） ----------
function openCreateDialog() {
  ItemForm.openCreateItem({
    fields: fieldsMeta,
    spus: spusList,
    onCreated: () => { offset = 0; loadItems(); },
    onSpuCreated: () => fillSpuSelects(),
    // issue #33：重名提示里选「编辑这个当前已有的 SKU」时打开同一个编辑小窗
    onEditExisting: (dup) => editFlow(dup),
  });
}

// ---------- 编辑（PATCH：改名 / 换挂 SPU / 改值，null = 清除） ----------
// 小窗本体在 components/item-form.js：单品页与库存页（重名提示的「编辑这个当前已有的 SKU」）共用
function editFlow(item) {
  ItemForm.openEditItem({
    item,
    fields: fieldsMeta,
    spus: spusList,
    onSaved: async () => { await loadMeta(); fillSpuSelects(); loadItems(); },
    onSpuCreated: () => fillSpuSelects(),
  });
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
