/* 库存总览页：SPU 分组表格，带附属 SKU 的 SPU 行可展开查看其 SKU（子行以浅色底体现层级） */
"use strict";

let overviewRows = [];        // [{ spu, items }]：SPU 与其附属 SKU（按 spu_id 预分组）
const expandedSpus = new Set(); // 当前展开的 SPU id

async function loadOverview() {
  const tbody = $("#spuRows");
  try {
    // /spus 带 sku_count；/items 单次上限 200，个人规模足够，超出时提示合计可能不完整
    const [spus, itemsData] = await Promise.all([XM.get("/spus"), XM.get("/items?limit=200")]);
    if (itemsData.total > itemsData.items.length) {
      UI.fail(`单品共 ${itemsData.total} 个，超过一次拉取上限（200），页面数据可能不完整`);
    }
    const bySpu = new Map();
    for (const it of itemsData.items) {
      if (!bySpu.has(it.spu_id)) bySpu.set(it.spu_id, []);
      bySpu.get(it.spu_id).push(it);
    }
    overviewRows = spus.spus.map((s) => ({ spu: s, items: bySpu.get(s.id) ?? [] }));
    renderOverview();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
    UI.apiFail(e);
  }
}

function renderOverview() {
  const tbody = $("#spuRows");
  if (!overviewRows.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="t-empty">该店铺还没有 SPU，可到「SPU」页创建</td></tr>`;
    return;
  }
  tbody.innerHTML = overviewRows.map(({ spu, items }) => {
    const open = expandedSpus.has(spu.id);
    const total = items.reduce((acc, it) => acc + it.qty, 0);
    // 只有存在附属 SKU 的 SPU 行才渲染展开三角
    const tri = spu.sku_count > 0
      ? `<button type="button" class="tri-btn${open ? " open" : ""}" data-spu="${spu.id}" aria-label="展开/收起 SKU">▼</button>`
      : "";
    let row = `<tr class="spu-row">
      <td class="tri-cell">${tri}</td>
      <td class="num mono">${spu.id}</td>
      <td>${UI.esc(spu.name)}</td>
      <td class="num">${spu.sku_count}</td>
      <td class="num">${spu.sku_count > 0 ? total : "—"}</td>
      <td class="muted">${UI.fmtTime(spu.updated_at)}</td>
    </tr>`;
    if (open) {
      row += items.length ? items.map((it) => `<tr class="sku-sub-row">
        <td></td>
        <td class="num mono">${it.id}</td>
        <td>${UI.esc(it.name)}</td>
        <td class="muted">—</td>
        <td class="num">${it.qty}</td>
        <td class="muted">${UI.fmtTime(it.updated_at)}</td>
      </tr>`).join("")
        : `<tr class="sku-sub-row"><td></td><td colspan="5" class="t-empty">没有可显示的 SKU 数据</td></tr>`;
    }
    return row;
  }).join("");
}

window.PAGE_INIT = async function () {
  if (!UI.requireShop()) return;
  // 事件委托绑在 tbody 上：重渲染后无需重绑
  $("#spuRows").addEventListener("click", (e) => {
    const btn = e.target.closest(".tri-btn[data-spu]");
    if (!btn) return;
    const id = Number(btn.dataset.spu);
    if (expandedSpus.has(id)) expandedSpus.delete(id);
    else expandedSpus.add(id);
    renderOverview();
  });
  await loadOverview();
};
