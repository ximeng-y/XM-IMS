/* SPU 管理页：列表 / 新建 / 改名 / 列设置 */
"use strict";

import { XM } from "xm/api";
import { UI, $, $$ } from "xm/ui";
import { ShopContext } from "xm/shop-context";
import { DeleteFlow } from "xm/components/delete-flow";
import { ItemForm } from "xm/components/item-form";
import { ViewSettings } from "xm/runtime/view-settings";

// 列的语义标识与位置无关：列顺序和隐藏状态由列设置按店铺保存，
// 表头与每一行都按同一份可见列顺序渲染，列宽也只认列标识，不认「第几列」
const COLUMNS = [
  { key: "id", label: "ID", cls: "col-id", headCls: "num" },
  { key: "name", label: "名称", cls: "col-name" },
  { key: "sku_count", label: "SKU 数", cls: "col-sku-count", headCls: "num" },
  { key: "updated_at", label: "更新时间", cls: "col-updated" },
  // 操作是交互控件：可以排序，不提供隐藏
  { key: "actions", label: "操作", cls: "col-actions", control: true },
];

const CELLS = {
  id: (s) => `<td class="num mono">${s.id}</td>`,
  name: (s) => `<td title="${UI.esc(s.name)}">${UI.esc(s.name)}</td>`,
  sku_count: (s) => `<td class="num">${s.sku_count}</td>`,
  updated_at: (s) => `<td class="muted">${UI.esc(UI.fmtTime(s.updated_at))}</td>`,
  actions: (s) => `<td class="row-actions">
      <button type="button" class="btn-outline btn-sm" data-rename="${s.id}" data-name="${UI.esc(s.name)}">改名</button>
      <button type="button" class="btn-outline btn-sm btn-danger-outline" data-del="${s.id}" data-name="${UI.esc(s.name)}" data-count="${s.sku_count}">删除</button>
    </td>`,
};

let rows = null;      // 最近一次加载的列表；null 表示还没拿到数据
let errorText = "";   // 加载失败的文案

const settings = ViewSettings.create({
  view: "spus",
  columns: COLUMNS,
  // 列设置变化（含首次载入）后立即按新顺序与可见性重画表格
  onApply: () => render(),
});

function headHtml(keys) {
  return "<tr>" + keys.map((key) => {
    const col = COLUMNS.find((c) => c.key === key);
    const cls = [col.headCls, col.cls].filter(Boolean).join(" ");
    return `<th class="${cls}">${UI.esc(col.label)}</th>`;
  }).join("") + "</tr>";
}

function render() {
  const keys = settings.visible();
  const tbody = $("#spuRows");
  $("#spuHead").innerHTML = headHtml(keys);
  // 全部信息列都隐藏时给出页面级提示；操作列始终保留，恢复入口不会消失
  $("#columnsHiddenHint").hidden = settings.infoVisible();
  if (errorText) {
    tbody.innerHTML = `<tr><td colspan="${keys.length}" class="t-empty">加载失败：${UI.esc(errorText)}</td></tr>`;
    return;
  }
  if (rows === null) {
    tbody.innerHTML = `<tr><td colspan="${keys.length}" class="t-empty">加载中…</td></tr>`;
    return;
  }
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="${keys.length}" class="t-empty">还没有 SPU；可点击「＋ 新建 SPU」创建，或在新建单品时按名称自动创建</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map((s) =>
    `<tr class="spu-row" data-spu="${s.id}">` +
    keys.map((key) => CELLS[key](s)).join("") + "</tr>").join("");
  $$("button[data-rename]", tbody).forEach((b) => b.addEventListener("click", renameFlow));
  $$("button[data-del]", tbody).forEach((b) => b.addEventListener("click", deleteFlow));
}

async function loadSpus() {
  try {
    rows = (await XM.get("/spus")).spus;
    errorText = "";
  } catch (e) {
    rows = null;
    errorText = e.message;
  }
  render();
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

    $("#btnColumns").addEventListener("click", () => settings.open());
    $$("[data-open-columns]").forEach((b) =>
      b.addEventListener("click", () => settings.open()));

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

    // 新建入口复用共享组件：成功提示由组件给出，这里只按返回值决定是否刷新列表
    $("#btnNewSpu").addEventListener("click", async () => {
      if (await ItemForm.openCreateSpu()) loadSpus();
    });

    // 先取回本页已保存的列设置并应用，首次渲染就是用户上次的顺序
    await settings.ready();
    await loadSpus();
  },
};
