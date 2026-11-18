/* 共享组件：单品（SKU）只读信息展示
   供 SKU 同名提醒与库存操作弹窗共用。只负责渲染传入的单品与属性定义，
   不发起请求、不持有选中状态、不参与创建或库存提交。

   属性按定义顺序全部列出：普通属性按类型给出显示值（百分比直接用接口返回的
   显示值，25 即 25%）；计算属性取后端格式化结果，失败时展开错误说明。 */
"use strict";

import { UI } from "xm/ui";
import { AttributeValuesForm } from "xm/components/attribute-values-form";

const esc = UI.esc;
const {
  calcHtml, computedRowHtml, calcBlockHtml, valueText, isComputed,
} = AttributeValuesForm;

// ---------- 基本信息 + 全部自定义属性 ----------
// fields 为该店铺的自定义字段定义（不含系统字段）。null 表示属性定义尚未取回，
// "error" 表示取回失败（两种情况仍展示基础信息，不让信息区空着）。
// item 为接口返回的单品对象。只读展示：任何内容都按文本转义后输出。
function html(item, fields = []) {
  if (!item) return `<div class="item-info-empty muted">尚未选择单品</div>`;
  const basics = [
    ["编号", `<span class="mono">${esc(item.id)}</span>`],
    ["名称", esc(item.name)],
    ["所属 SPU", esc(item.spu_name)],
    ["当前库存", esc(item.qty)],
  ];
  const basicHtml = `<div class="ii-basic">` + basics.map(([k, v]) =>
    `<div class="ii-row"><span class="ii-key">${k}</span><span class="ii-val">${v}</span></div>`
  ).join("") + `</div>`;

  let fieldHtml;
  if (fields === null) {
    fieldHtml = `<div class="ii-row"><span class="ii-val muted">属性加载中…</span></div>`;
  } else if (fields === "error") {
    fieldHtml = `<div class="ii-row"><span class="ii-val muted">属性加载失败，请关闭后重试</span></div>`;
  } else if (!fields.length) {
    fieldHtml = `<div class="ii-row"><span class="ii-val muted">该店铺还没有自定义属性</span></div>`;
  } else {
    fieldHtml = fields.map((f) => `<div class="ii-row">
        <span class="ii-key">${esc(f.name)}</span>
        <span class="ii-val">${isComputed(f)
          ? calcBlockHtml(item.calculations && item.calculations[f.name])
          : esc(valueText(f, item.values))}</span>
      </div>`).join("");
  }

  return `<div class="item-info">${basicHtml}<div class="ii-fields">${fieldHtml}</div></div>`;
}

export const ItemInfo = { html, calcHtml, computedRowHtml };
