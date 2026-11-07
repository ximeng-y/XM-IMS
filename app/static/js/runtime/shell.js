/* 普通页面外壳：顶栏店铺组件、新建店铺、右键基础监听与无店铺状态。
   店铺状态由 ShopContext 持有；本模块只负责取数、渲染与用户操作后的页面刷新。 */
"use strict";

import { XM } from "xm/api";
import { UI, $, $$ } from "xm/ui";
import { ShopContext } from "xm/shop-context";
import { ContextMenu } from "xm/components/context-menu";

// 零店铺时：不保留空的选择框，原位置换成「新建店铺」。店铺管理页本身不参与
// 零店铺的整页禁用，否则空状态无法创建第一间店铺。
// hasShop 为 null 表示列表读取失败、店铺数未知：此时隐藏选择框与空状态提示，
// 避免把读取失败当成「当前还没有店铺」。
function applyNoShop(hasShop, { isShopsPage = false } = {}) {
  const known = hasShop !== null;
  const box = $("#noShopBox");
  if (box) box.hidden = !known ? true : (hasShop || isShopsPage);
  const sel = $("#shopSelect");
  if (sel) sel.hidden = !hasShop;
  const label = $(".shop-box label");
  if (label) label.hidden = !hasShop;
  const btn = $("#btnNewShop");
  if (btn) btn.hidden = hasShop !== false;
  if (isShopsPage) return;
  $$("main.content button").forEach((b) => { b.disabled = !hasShop; });
}

export function renderShopOptions() {
  const sel = $("#shopSelect");
  if (!sel) return;
  const shops = ShopContext.snapshot();
  const currentId = ShopContext.currentId();
  sel.innerHTML = shops
    .map((s) => `<option value="${s.id}">${UI.esc(s.name)}（#${s.id}）</option>`).join("");
  if (currentId !== null) sel.value = String(currentId);
}

async function createShopFlow() {
  const name = await UI.promptDialog({
    title: "新建店铺", label: "店铺名称（全局唯一）",
    placeholder: "如：主店", submitText: "创建", maxLength: 100,
  });
  if (name === null) return;
  try {
    const shop = await XM.post("/shops", { name });
    ShopContext.rememberSelection(shop.id);
    // 新店铺默认追加到末尾：不写入本地顺序，排序时会自动把它排在已有店铺之后
    UI.ok(`店铺「${shop.name}」已创建`);
    setTimeout(() => window.location.reload(), 600);
  } catch (e) { UI.apiFail(e, "创建店铺失败"); }
}

export { createShopFlow };

// 顶栏选择框与新建按钮：只在普通页面（存在这些节点时）绑定
function bindTopbar() {
  const btn = $("#btnNewShop");
  if (btn) btn.addEventListener("click", createShopFlow);
  const sel = $("#shopSelect");
  if (sel) {
    sel.addEventListener("change", () => {
      ShopContext.rememberSelection(sel.value);
      // 切换店铺后整页刷新，确保所有数据与新店铺上下文一致
      window.location.reload();
    });
  }
}

// 取店铺列表并交给上下文所有者排序、挑选当前店铺，再渲染顶栏。
// 返回 false 表示读取失败（调用方不继续业务初始化）。
export async function loadShops({ isShopsPage = false } = {}) {
  let list;
  try {
    const data = await XM.get("/shops");
    list = ShopContext.replaceShops(data.shops);
  } catch (e) {
    UI.apiFail(e, "无法获取店铺列表");
    ShopContext.clear();
    applyNoShop(null, { isShopsPage });
    return false;
  }
  renderShopOptions();
  applyNoShop(list.length > 0, { isShopsPage });
  return true;
}

// 页面自行改动店铺后，用它把顶栏与无店铺状态同步到最新（卡片由页面自己渲染）
export function refreshShopChrome({ isShopsPage = false } = {}) {
  const list = ShopContext.snapshot();
  renderShopOptions();
  applyNoShop(list.length > 0, { isShopsPage });
  return list;
}

export function installShell() {
  ContextMenu.install();
  bindTopbar();
}
