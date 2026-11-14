/* 单品选项：库存操作小窗与流水筛选共用的下拉内容。
   展示用「SPU 名-SKU 名（库存 n）」，便于一眼看出单品归属哪个 SPU；
   SKU 允许同名，选项里再带编号，同名同库存的单品也能在下拉中区分。
   选项值仍是外部标识（形如 1-2，SPU 号-组内序号），提交给接口的编号不受影响。
   补选项等其余拼装路径一律调这里，避免同一份下拉出现两种文案。 */
"use strict";

import { XM } from "xm/api";
import { UI } from "xm/ui";

export function itemOptionLabel(i) {
  return `${i.spu_name}-${i.name}（库存 ${i.qty}，编号 ${i.id}）`;
}

export async function fillItemOptions(select, { allLabel = null } = {}) {
  const data = await XM.get("/items?limit=200");
  let html = allLabel ? `<option value="">${UI.esc(allLabel)}</option>` : "";
  html += data.items.map((i) =>
    `<option value="${UI.esc(i.id)}">${UI.esc(itemOptionLabel(i))}</option>`).join("");
  select.innerHTML = html || `<option value="">（该店铺还没有单品）</option>`;
  return data.items;
}
