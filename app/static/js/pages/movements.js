/* 流水查询页：单品/类型筛选 + 分页 */
"use strict";

const MOV_LIMIT = 50;
let movOffset = 0;

const TYPE_BADGE = { in: `<span class="badge green">入库</span>`,
                     out: `<span class="badge red">出库</span>`,
                     adjust: `<span class="badge gray">盘点</span>` };

// 文本列单元格：固定列宽下长内容被省略号截断，title 让悬停可看全文。
// 空值仍按 UI.fmtVal 显示占位符，但此时不挂 title，避免空悬停框。
function textCell(v) {
  const s = v === null || v === undefined ? "" : String(v).trim();
  return s ? `<td title="${UI.esc(s)}">${UI.esc(v)}</td>` : `<td class="muted">—</td>`;
}

async function loadMovements() {
  const tbody = $("#movRows");
  const params = new URLSearchParams({ offset: String(movOffset), limit: String(MOV_LIMIT) });
  const itemId = $("#fltItem").value;
  const mtype = $("#fltType").value;
  if (itemId) params.set("item_id", itemId);
  if (mtype) params.set("type", mtype);
  try {
    const data = await XM.get("/movements?" + params);
    if (!data.movements.length) {
      tbody.innerHTML = `<tr><td colspan="13" class="t-empty">没有符合条件的流水</td></tr>`;
    } else {
      tbody.innerHTML = data.movements.map((m) => `
        <tr>
          <td class="num mono">${m.id ?? "—"}</td>
          <td class="muted" style="white-space:nowrap">${UI.fmtTime(m.created_at)}</td>
          <td class="num mono">${UI.esc(m.item_id)}</td>
          <td>${TYPE_BADGE[m.type] || `<span class="muted">未知类型</span>`}</td>
          <td class="num">${UI.fmtDelta(m.qty_delta)}</td>
          <td class="num">${UI.fmtMoney(m.unit_price)}</td>
          ${textCell(m.batch_no)}
          ${textCell(m.supplier)}
          ${textCell(m.channel)}
          ${textCell(m.customer)}
          <td class="num">${m.target_qty ?? "—"}</td>
          ${textCell(m.note)}
          <td class="muted mono" title="${UI.esc(m.actor)}">${UI.esc(m.actor)}</td>
        </tr>`).join("");
    }
    UI.renderPager("#movPager", { offset: movOffset, limit: MOV_LIMIT, total: data.total },
      (go) => { movOffset = go; loadMovements(); });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="13" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
    UI.apiFail(e);
  }
}

window.PAGE_INIT = async function () {
  if (!UI.requireShop()) return;
  try { await UI.fillItemOptions($("#fltItem"), { allLabel: "全部单品" }); }
  catch (e) { UI.apiFail(e, "加载单品列表失败"); }
  $("#btnQuery").addEventListener("click", () => { movOffset = 0; loadMovements(); });
  $("#fltType").addEventListener("change", () => { movOffset = 0; loadMovements(); });
  $("#fltItem").addEventListener("change", () => { movOffset = 0; loadMovements(); });
  loadMovements();
};
