/* 库存页：SPU 分组表格（带附属 SKU 的 SPU 行可展开）+ 顶部操作区（新建/出入库/盘点小窗 + 搜索）
   操作区各按钮复用 components/ 下的共享表单组件，避免与单品页、原库存操作页各写一份 */
"use strict";

let overviewRows = [];          // [{ spu, items, total }]：SPU、其附属 SKU（按 spu_id 预分组）、整组合计库存
let fieldsMeta = [];            // 自定义字段定义（新建单品小窗用）
let spusList = [];              // SPU 列表（与新建单品小窗内的组合框共享引用）
const expandedSpus = new Set(); // 当前展开的 SPU id
let lastSearchQuery = null;     // 上次渲染使用的搜索词，用于仅在新搜索时自动展开

async function loadOverview() {
  const tbody = $("#spuRows");
  try {
    // /spus 带 sku_count；/items 单次上限 200，个人规模足够，超出时提示合计可能不完整
    const [spus, itemsData, fields] = await Promise.all([
      XM.get("/spus"), XM.get("/items?limit=200"), XM.get("/fields"),
    ]);
    if (itemsData.total > itemsData.items.length) {
      UI.fail(`单品共 ${itemsData.total} 个，本页仅显示前 ${itemsData.items.length} 个，合计库存可能不完整`);
    }
    spusList = spus.spus;
    fieldsMeta = fields.fields.filter((f) => !f.system);
    const bySpu = new Map();
    for (const it of itemsData.items) {
      if (!bySpu.has(it.spu_id)) bySpu.set(it.spu_id, []);
      bySpu.get(it.spu_id).push(it);
    }
    overviewRows = spusList.map((s) => {
      const items = bySpu.get(s.id) ?? [];
      return { spu: s, items, total: items.reduce((acc, it) => acc + it.qty, 0) };
    });
    renderOverview();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
    UI.apiFail(e);
  }
}

// 包含式匹配：SPU 名或任一 SKU 名命中即保留该 SPU 组
//   hit: "spu"（组名命中，整组 SKU 都显示）| "sku"（仅 SKU 名命中，自动展开且只显示命中的 SKU）
// 合计库存 / SKU 数始终按整组计，展示该 SPU 的真实汇总，不随筛选变化
function filterRows() {
  const q = $("#fltQ").value.trim().toLowerCase();
  if (!q) return overviewRows.map((r) => ({ ...r, shown: r.items, hit: null }));
  const out = [];
  for (const r of overviewRows) {
    if (r.spu.name.toLowerCase().includes(q)) {
      out.push({ ...r, shown: r.items, hit: "spu" });
      continue;
    }
    const hits = r.items.filter((it) => it.name.toLowerCase().includes(q));
    if (hits.length) out.push({ ...r, shown: hits, hit: "sku" });
  }
  return out;
}

function renderOverview() {
  const tbody = $("#spuRows");
  const rows = filterRows();
  const query = $("#fltQ").value.trim();
  const extra = fieldsMeta.length;
  const colCount = 6 + extra;
  // 表头随本店自定义字段一起重建；列顺序 = 展开 / ID / 名称 / SKU 数 / 合计库存 / 自定义字段… / 更新时间
  // 取值口径与单品页一致：计算字段取后端计算结果，普通字段取手填值
  $("#spuHead").innerHTML =
    `<tr><th style="width:2.5rem"></th><th class="num">ID</th><th>名称</th>` +
    `<th class="num">SKU 数</th><th class="num">合计库存</th>` +
    fieldsMeta.map((f) => `<th>${UI.esc(f.name)}</th>`).join("") +
    `<th>更新时间</th></tr>`;
  const extraCells = (it) => fieldsMeta.map((f) => `<td>${
    ItemForm.isComputed(f) ? ItemForm.calcHtml(it.calculations?.[f.name])
                           : UI.fmtVal(it.values[f.name])}</td>`).join("");
  // 每个新搜索词仅自动展开一次，后续重渲染尊重用户的手动展开/收起
  if (query && query !== lastSearchQuery) {
    rows.forEach((r) => { if (r.hit === "sku") expandedSpus.add(r.spu.id); });
  }
  lastSearchQuery = query;

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="${colCount}" class="t-empty">` +
      ($("#fltQ").value.trim() ? "没有匹配的 SPU / SKU" : "该店铺还没有 SPU，可点上方「新建 SPU」或「新建单品」创建") +
      `</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(({ spu, total, shown }) => {
    const open = expandedSpus.has(spu.id);
    // 只有存在附属 SKU 的 SPU 行才渲染展开三角
    const tri = spu.sku_count > 0
      ? `<button type="button" class="tri-btn${open ? " open" : ""}" data-spu="${spu.id}" aria-label="展开/收起 SKU">▼</button>`
      : "";
    // data-spu 供右键菜单识别目标 SPU（菜单里可「在此 SPU 下新建单品」）
    let row = `<tr class="spu-row" data-spu="${spu.id}">
      <td class="tri-cell">${tri}</td>
      <td class="num mono">${spu.id}</td>
      <td>${UI.esc(spu.name)}</td>
      <td class="num">${spu.sku_count}</td>
      <td class="num">${spu.sku_count > 0 ? total : "—"}</td>
      ${fieldsMeta.map(() => `<td class="muted">—</td>`).join("")}
      <td class="muted">${UI.fmtTime(spu.updated_at)}</td>
    </tr>`;
    if (open) {
      // data-item / data-item-name / data-spu 供右键菜单与后续操作识别目标 SKU
      row += shown.length ? shown.map((it) => `<tr class="sku-sub-row" data-item="${UI.esc(it.id)}" data-item-name="${UI.esc(it.name)}" data-spu="${spu.id}">
        <td></td>
        <td class="num mono">${UI.esc(it.id)}</td>
        <td>${UI.esc(it.name)}</td>
        <td class="muted">—</td>
        <td class="num">${it.qty}</td>
        ${extraCells(it)}
        <td class="muted">${UI.fmtTime(it.updated_at)}</td>
      </tr>`).join("")
        : `<tr class="sku-sub-row"><td></td><td colspan="${colCount - 1}" class="t-empty">没有可显示的 SKU 数据</td></tr>`;
    }
    return row;
  }).join("");
}

// ---------- 操作区：各按钮 → 对应小窗 ----------
const OP_ACTIONS = {
  "new-item": () => ItemForm.openCreateItem({
    fields: fieldsMeta, spus: spusList, onCreated: () => loadOverview(),
    // issue #33：重名提示里选「编辑这个当前已有的 SKU」时打开同一个编辑小窗
    onEditExisting: (item) => ItemForm.openEditItem({
      item, fields: fieldsMeta, spus: spusList, onSaved: () => loadOverview(),
    }),
  }),
  "new-spu": async () => {
    if (await ItemForm.openCreateSpu()) loadOverview();
  },
  in: () => OpsForm.open("in", { onDone: () => loadOverview() }),
  out: () => OpsForm.open("out", { onDone: () => loadOverview() }),
  adjust: () => OpsForm.open("adjust", { onDone: () => loadOverview() }),
};

function bindOpsBar() {
  $$(".ops-bar button[data-op]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const act = OP_ACTIONS[btn.dataset.op];
      if (act) act();
    });
  });

  const q = $("#fltQ"), clearBtn = $("#btnClearQ");
  const sync = () => {
    clearBtn.hidden = q.value === "";
    renderOverview();
  };
  q.addEventListener("input", sync);
  q.addEventListener("keydown", (e) => { if (e.key === "Escape") { q.value = ""; sync(); } });
  clearBtn.addEventListener("click", () => { q.value = ""; sync(); q.focus(); });
}

function toggleSpu(id) {
  if (expandedSpus.has(id)) expandedSpus.delete(id);
  else expandedSpus.add(id);
  renderOverview();
}

// ---------- 右键菜单：SPU 行与 SKU 子行 ----------
// 菜单项全部复用操作区已有的小窗实现，不新增后端接口
function openCreateItemForSpu(spuId) {
  ItemForm.openCreateItem({
    fields: fieldsMeta, spus: spusList, initialSpuId: spuId,
    onCreated: () => loadOverview(),
    onEditExisting: (item) => ItemForm.openEditItem({
      item, fields: fieldsMeta, spus: spusList, onSaved: () => loadOverview(),
    }),
  });
}

function bindContextMenu() {
  $("#spuRows").addEventListener("contextmenu", (e) => {
    const sku = e.target.closest("tr.sku-sub-row[data-item]");
    if (sku) {
      ContextMenu.open(e, [
        { label: "入库", onClick: () => OpsForm.open("in", { itemRef: sku.dataset.item, onDone: () => loadOverview() }) },
        { label: "出库", onClick: () => OpsForm.open("out", { itemRef: sku.dataset.item, onDone: () => loadOverview() }) },
        { label: "盘点调整", onClick: () => OpsForm.open("adjust", { itemRef: sku.dataset.item, onDone: () => loadOverview() }) },
        { label: "新建同 SPU 单品",
          onClick: () => openCreateItemForSpu(Number(sku.dataset.spu)) },
      ]);
      return;
    }
    const spu = e.target.closest("tr.spu-row[data-spu]");
    if (spu) {
      ContextMenu.open(e, [
        { label: "在此 SPU 下新建单品",
          onClick: () => openCreateItemForSpu(Number(spu.dataset.spu)) },
      ]);
    }
  });
}

window.PAGE_INIT = async function () {
  if (!UI.requireShop()) return;
  // 事件委托绑在 tbody 上：重渲染后无需重绑
  // 三角按钮与 SPU 行空白区域都触发同一个展开/收起，一次点击只切换一次
  $("#spuRows").addEventListener("click", (e) => {
    const btn = e.target.closest(".tri-btn[data-spu]");
    if (btn) { toggleSpu(Number(btn.dataset.spu)); return; }
    // 拖拽选中行内文字后松开也会触发 click，此时不应切换展开状态
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    // data-spu 也可能被其它功能（例如右键菜单识别目标行）加到不可展开的行上：
    // 这里以「行内存在三角按钮」作为可展开的判据，避免空 SPU 行被误展开
    const row = e.target.closest("tr.spu-row[data-spu]");
    if (row && row.querySelector(".tri-btn")) toggleSpu(Number(row.dataset.spu));
  });
  bindOpsBar();
  bindContextMenu();
  await loadOverview();
  // 概览页快捷入口带 ?op=xxx 跳来：进页后直接弹出对应小窗
  const op = new URLSearchParams(location.search).get("op");
  if (op && OP_ACTIONS[op]) OP_ACTIONS[op]();
};
