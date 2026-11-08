/* 概览页：关键指标 + 最近流水 */
"use strict";

import { XM } from "xm/api";
import { UI } from "xm/ui";
import { ShopContext } from "xm/shop-context";

export const page = {
  shell: true, shopContext: "required", requireSyncReady: true, autoUpdatePrompt: true,
  async init() {
    // 无店铺时外壳已给出提示并禁用业务按钮，这里不再发起业务查询
    if (ShopContext.currentId() === null) return;
    const safe = (label, p) => p.catch((e) => { UI.apiFail(e, label); return null; });

    const [items, movs, spus, over] = await Promise.all([
      safe("查询单品总数", XM.get("/items?limit=1")),
      safe("查询流水", XM.get("/movements?limit=5")),
      safe("查询 SPU", XM.get("/spus")),
      safe("查询概览统计", XM.get("/overview")),
    ]);
    UI.$("#stItems").textContent = items ? items.total : "—";
    UI.$("#stSpus").textContent = spus ? spus.spus.length : "—";
    UI.$("#stQty").textContent = over ? over.total_qty : "—";
    UI.$("#stAnomalies").textContent = over ? over.anomaly_items : "—";

    if (!movs) return;
    const tbody = UI.$("#recentMovs");
    if (!movs.movements.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="t-empty">还没有流水，<a href="/inventory">去「库存总览」页记一笔</a></td></tr>`;
      return;
    }
    // 文本列单元格：固定列宽下长内容被省略号截断，title 让悬停可看全文；空值不挂 title
    const textCell = (v) => {
      const s = v === null || v === undefined ? "" : String(v).trim();
      return s ? `<td title="${UI.esc(s)}">${UI.esc(v)}</td>` : `<td class="muted">—</td>`;
    };
    const typeBadge = { in: `<span class="badge green">入库</span>`,
                        out: `<span class="badge red">出库</span>`,
                        adjust: `<span class="badge gray">盘点</span>` };
    tbody.innerHTML = movs.movements.map((m) => `
      <tr>
        <td class="mono">${m.id}</td>
        <td>${UI.fmtTimeShort(m.created_at)}</td>
        <td>${typeBadge[m.type] || `<span class="muted">未知类型</span>`}</td>
        <td class="num">${UI.fmtDelta(m.qty_delta)}</td>
        <td class="num">${UI.fmtMoney(m.unit_price)}</td>
        ${textCell(m.supplier || m.channel)}
        ${textCell(m.note)}
      </tr>`).join("");
  },
};
