/* SPU 管理页：列表 / 新建 / 改名 */
"use strict";

import { XM } from "xm/api";
import { UI, $, $$ } from "xm/ui";
import { ShopContext } from "xm/shop-context";
import { DeleteFlow } from "xm/components/delete-flow";

async function loadSpus() {
  const tbody = $("#spuRows");
  try {
    const data = await XM.get("/spus");
    if (!data.spus.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="t-empty">还没有 SPU；可在「库存」页用「新建 SPU」创建，或在新建单品时按名称自动创建</td></tr>`;
      return;
    }
    tbody.innerHTML = data.spus.map((s) => `
      <tr class="spu-row" data-spu="${s.id}">
        <td class="num mono">${s.id}</td>
        <td title="${UI.esc(s.name)}">${UI.esc(s.name)}</td>
        <td class="num">${s.sku_count}</td>
        <td class="muted">${UI.fmtTime(s.updated_at)}</td>
        <td class="row-actions">
          <button type="button" class="btn-outline btn-sm" data-rename="${s.id}" data-name="${UI.esc(s.name)}">改名</button>
          <button type="button" class="btn-outline btn-sm btn-danger-outline" data-del="${s.id}" data-name="${UI.esc(s.name)}" data-count="${s.sku_count}">删除</button>
        </td>
      </tr>`).join("");
    $$("button[data-rename]", tbody).forEach((b) => b.addEventListener("click", renameFlow));
    $$("button[data-del]", tbody).forEach((b) => b.addEventListener("click", deleteFlow));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
  }
}

async function renameFlow(ev) {
  const btn = ev.currentTarget;
  const id = Number(btn.dataset.rename);
  const oldName = btn.dataset.name;
  const name = await UI.promptDialog({
    title: `SPU #${id} 改名`, label: "新名称（全局一处生效）",
    value: oldName, maxLength: 200,
  });
  if (name === null || name === oldName) return;
  try {
    await XM.patch(`/spus/${id}`, { name });
    UI.ok(`SPU #${id} 已改名为「${name}」`);
    loadSpus();
  } catch (e) { UI.apiFail(e, "改名失败"); }
}

// 删除 SPU：其下还有单品时后端会拒绝，这里先按已知的 sku_count 给出提示
function deleteFlow(ev) {
  const btn = ev.currentTarget;
  const id = Number(btn.dataset.del);
  const name = btn.dataset.name;
  const count = Number(btn.dataset.count);
  return DeleteFlow.run({
    precheck: () => {
      if (count === 0) return true;
      UI.fail(`SPU「${name}」下还有 ${count} 个单品，不能删除`,
              { title: "无法删除", hint: "请先删除该 SPU 下的全部单品（仅从未产生过流水的单品可删除）" });
      return false;
    },
    title: `删除 SPU「${name}」`,
    bodyHtml: `<p>该 SPU 下没有任何单品，删除后<b style="color:var(--danger)">不可恢复</b>。</p>`,
    path: `/spus/${id}?confirm=true`,
    okText: (res) => `SPU「${res.name}」已删除`,
    onDone: loadSpus,
  });
}

export const page = {
  shell: true, shopContext: "required", requireSyncReady: true, autoUpdatePrompt: true,
  async init() {
      if (ShopContext.currentId() === null) return;

    // 双击行内任意非控件区域（含单元格内文字与文字旁的空白）跳到库存页并展开该 SPU。
    // 判据与库存页行点击一致：只排除控件，不排除文字——文字由双击选中的选区自行处理，
    // 拖选文字后松手不跳转，也不清用户已有选区
    $("#spuRows").addEventListener("dblclick", (e) => {
      const row = e.target.closest("tr.spu-row[data-spu]");
      if (!row) return;
      if (e.target.closest("button, a, input, select, textarea, label")) return;
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed) return;
      location.href = `/inventory?spu_id=${row.dataset.spu}`;
    });

    $("#fCreate").addEventListener("submit", (ev) => {
      ev.preventDefault();
      const btn = ev.target.querySelector("button[type=submit]");
      const name = $("#spuName").value.trim();
      if (!name) { UI.fail("请填写 SPU 名称"); return; }
      UI.withBusy(btn, async () => {
        try {
          const spu = await XM.post("/spus", { name });
          UI.resultBox("#createResult", {
            html: UI.okHtml(`SPU「${spu.name}」已创建（#${spu.id}）`),
          });
          $("#spuName").value = "";
          loadSpus();
        } catch (e) {
          UI.resultBox("#createResult", { error: true, html: UI.apiFailHtml(e) });
        }
      });
    });

    await loadSpus();
  },
};
