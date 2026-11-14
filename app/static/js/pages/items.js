/* 单品（SKU）管理页：搜索筛选分页 / 编辑（改名、换挂 SPU、改值/清除值）
   新建走共享组件 components/item-form.js 的「新建单品」小窗（与库存页操作区同一实现） */
"use strict";

import { XM } from "xm/api";
import { UI, $, $$ } from "xm/ui";
import { ShopContext } from "xm/shop-context";
import { ItemForm } from "xm/components/item-form";
import { DeleteFlow } from "xm/components/delete-flow";

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
    extraHead + `<th>操作</th></tr>`;
  if (!data.items.length) {
    tbody.innerHTML = `<tr><td colspan="${5 + fieldsMeta.length}" class="t-empty">没有符合条件的单品</td></tr>`;
    return;
  }
  tbody.innerHTML = data.items.map((it, idx) => {
    // 计算字段与普通字段同列展示：普通字段取手填值，计算字段取后端算出的结果文本/错误
    // 普通字段值与计算成功的结果都挂 title：固定列宽下长字符串、长金额会被省略号截断，
    // 悬停需能看全文。出错时的徽标自带 title（写明原因），不受这里影响，故不重复挂。
    const extra = fieldsMeta.map((f) => {
      if (ItemForm.isComputed(f)) {
        const calc = it.calculations?.[f.name];
        const full = calc && !calc.error ? calc.display : "";
        return `<td${full ? ` title="${UI.esc(full)}"` : ""}>${ItemForm.calcHtml(calc)}</td>`;
      }
      const v = it.values[f.name];
      const s = v === null || v === undefined || v === "" ? "" : String(v);
      return `<td${s ? ` title="${UI.esc(s)}"` : ""}>${UI.fmtVal(v)}</td>`;
    }).join("");
    // item-row + data-item：供双击跳转库存页定位目标单品（issue #62）
    return `<tr class="item-row" data-item="${UI.esc(it.id)}">
      <td class="num mono">${UI.esc(it.id)}</td>
      <td title="${UI.esc(it.name)}">${UI.esc(it.name)}</td>
      <td title="${UI.esc(it.spu_name)} #${it.spu_id}">${UI.esc(it.spu_name)}<span class="muted mono" style="font-size:.75rem"> #${it.spu_id}</span></td>
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
// 组件只读传入的 SPU 列表，有变化时经 onSpusChanged 交回完整新列表，由本页替换自己的数组
function applySpus(next) {
  spusList = next;
  fillSpuSelects();
}

function openCreateDialog() {
  ItemForm.openCreateItem({
    fields: fieldsMeta,
    spus: spusList,
    onCreated: () => { offset = 0; loadItems(); },
    onSpuCreated: () => fillSpuSelects(),
    onSpusChanged: applySpus,
    // issue #33：同名提醒里选「编辑这个当前已有的 SKU」时打开同一个编辑小窗
    onEditExisting: (dup) => editFlow(dup),
  });
}

// ---------- 编辑（PATCH：改名 / 换挂 SPU / 改值，null = 清除） ----------
// 小窗本体在 components/item-form.js：单品页与库存页（同名提醒的「编辑这个当前已有的 SKU」）共用
function editFlow(item) {
  ItemForm.openEditItem({
    item,
    fields: fieldsMeta,
    spus: spusList,
    onSaved: async () => { await loadMeta(); fillSpuSelects(); loadItems(); },
    onSpuCreated: () => fillSpuSelects(),
    onSpusChanged: applySpus,
  });
}

// ---------- 启动 ----------
export const page = {
  shell: true, shopContext: "required", requireSyncReady: true, autoUpdatePrompt: true,
  async init() {
      if (ShopContext.currentId() === null) return;
    try { await loadMeta(); } catch (e) { UI.apiFail(e, "加载字段/SPU 失败"); }
    fillSpuSelects();

    $("#btnNewItem").addEventListener("click", openCreateDialog);
    $("#btnFilter").addEventListener("click", () => { offset = 0; loadItems(); });
    $("#fltQ").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { offset = 0; loadItems(); }
    });
    $("#fltSpu").addEventListener("change", () => { offset = 0; loadItems(); });

    // 双击行内任意非控件区域（含单元格内文字与文字旁的空白）跳到库存页并定位该单品（issue #62）。
    // 判据与 SPU 页、库存页行点击一致：只排除控件，不排除文字——文字由双击选中的选区自行处理，
    // 拖选文字后松手不跳转，也不清用户已有选区；委托绑在 tbody 上，分页重渲染后无需重绑
    $("#itemRows").addEventListener("dblclick", (e) => {
      const row = e.target.closest("tr.item-row[data-item]");
      if (!row) return;
      if (e.target.closest("button, a, input, select, textarea, label")) return;
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed) return;
      location.href = `/inventory?item_id=${encodeURIComponent(row.dataset.item)}`;
    });

    loadItems();
  },
};
