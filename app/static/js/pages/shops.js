/* 店铺管理页：卡片列表、创建 / 切换 / 重命名 / 删除，以及本机拖动排序

   排列顺序只保存在 localStorage（UI.applyShopOrder / UI.readOrder），
   不上传服务器，也不进入备份或同步快照。 */
"use strict";

const ShopsPage = (() => {
  const list = () => $("#shopList");
  let dragging = null;          // 正在拖动的卡片
  let shops = [];               // 当前页面的店铺列表（已按本地顺序）

  const currentId = () => XM.state.shopId;

  function cardHtml(shop) {
    const active = String(shop.id) === String(currentId());
    // 当前店铺不显示「切换到此店铺」，但用同样文案的透明占位按钮守住宽度，
    // 避免「重命名」「删除」向左补位
    const switchSlot = active
      ? `<button type="button" class="btn-outline btn-sm shop-switch-ghost" tabindex="-1"
                 aria-hidden="true" disabled>切换到此店铺</button>`
      : `<button type="button" class="btn-outline btn-sm" data-switch="${shop.id}">切换到此店铺</button>`;
    return `
      <div class="shop-card${active ? " is-current" : ""}" data-shop="${shop.id}">
        <div class="shop-card-accent" aria-hidden="true"></div>
        <div class="shop-card-main">
          <h2 class="shop-card-name" title="${UI.esc(shop.name)}">${UI.esc(shop.name)}</h2>
          <div class="shop-card-actions">
            ${switchSlot}
            <button type="button" class="btn-outline btn-sm" data-rename="${shop.id}">重命名</button>
            <button type="button" class="btn-outline btn-sm btn-danger-outline" data-del="${shop.id}">删除</button>
            ${active ? `<span class="shop-current-tag">当前店铺</span>` : ""}
          </div>
        </div>
        <span class="shop-drag" title="按住拖动调整顺序" aria-hidden="true">
          <span></span>
        </span>
      </div>`;
  }

  function render() {
    const box = list();
    if (!shops.length) {
      box.innerHTML = `<div class="shop-empty">当前还没有店铺，点击「创建店铺」来创建一个吧~</div>`;
      return;
    }
    box.innerHTML = shops.map(cardHtml).join("");
    bindCards();
  }

  function bindCards() {
    const box = list();
    $$("button[data-switch]", box).forEach((b) => b.addEventListener("click", switchShop));
    $$("button[data-rename]", box).forEach((b) => b.addEventListener("click", renameShop));
    $$("button[data-del]", box).forEach((b) => b.addEventListener("click", deleteShop));
    $$(".shop-card", box).forEach((card) => {
      // 拖动只从手柄开始：手柄按下才临时让卡片可拖，避免误拖按钮与文字选中。
      // 拖动事件本身发在可拖元素（卡片）上，故监听挂在卡片上。
      const handle = card.querySelector(".shop-drag");
      if (!handle) return;
      handle.addEventListener("mousedown", () => { card.draggable = true; });
      handle.addEventListener("mouseup", () => { if (!dragging) card.draggable = false; });
      card.addEventListener("dragstart", (ev) => startDrag(ev, card));
      card.addEventListener("dragend", endDrag);
    });
  }

  // ---------- 拖动排序 ----------
  // 原生 HTML5 拖动：手柄按下才让卡片可拖，避免误拖行内按钮与文字选中。
  // 落点以「鼠标相对卡片中线」判断插到前还是后，拖动中实时移动 DOM，松手后落盘。
  function startDrag(ev, card) {
    dragging = card;
    card.classList.add("dragging");
    // 拖动期间禁止选中文字，避免拖拽过程中把卡片文字选蓝
    list().classList.add("is-dragging");
    ev.dataTransfer.effectAllowed = "move";
    // 部分浏览器要求设置数据才会真正进入拖动
    ev.dataTransfer.setData("text/plain", card.dataset.shop);
  }

  function endDrag() {
    if (!dragging) return;
    dragging.classList.remove("dragging");
    dragging.draggable = false;
    dragging = null;
    list().classList.remove("is-dragging");
    $$(".shop-card", list()).forEach((c) => c.classList.remove("drop-before", "drop-after"));
    persistOrder();
  }

  function onDragOver(ev) {
    if (!dragging) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
    const card = ev.target.closest(".shop-card");
    if (!card) {
      // 卡片之间的空隙 / 列表底部空白：落到末尾
      $$(".shop-card", list()).forEach((c) => c.classList.remove("drop-before", "drop-after"));
      list().append(dragging);
      return;
    }
    if (card === dragging) return;
    const after = ev.clientY > card.getBoundingClientRect().top + card.offsetHeight / 2;
    $$(".shop-card", list()).forEach((c) => c.classList.remove("drop-before", "drop-after"));
    card.classList.add(after ? "drop-after" : "drop-before");
    if (after) card.after(dragging);
    else card.before(dragging);
  }

  function persistOrder() {
    const ids = $$(".shop-card", list()).map((c) => Number(c.dataset.shop));
    const ordered = ids.map((id) => shops.find((s) => s.id === id)).filter(Boolean);
    UI.applyShopOrder(ordered);
    shops = ordered;
    XM.state.shops = ordered;
    // 顶栏下拉与卡片顺序保持一致；当前选择不变
    UI.renderShopOptions();
    render();
  }

  // ---------- 店铺操作 ----------
  function switchShop(ev) {
    const id = Number(ev.currentTarget.dataset.switch);
    if (id === currentId()) return;
    localStorage.setItem(UI.SHOP_KEY, String(id));
    // 切换后整页刷新，确保页面数据与业务请求都使用新店铺
    location.reload();
  }

  async function renameShop(ev) {
    const id = Number(ev.currentTarget.dataset.rename);
    const shop = shops.find((s) => s.id === id);
    if (!shop) return;
    const name = await UI.promptDialog({
      title: `重命名「${shop.name}」`, label: "新名称（全局唯一）",
      value: shop.name, submitText: "保存", maxLength: 100,
    });
    if (name === null || name === shop.name) return;
    try {
      const res = await XM.patch(`/shops/${id}`, { name });
      shop.name = res.name;
      shops = shops.map((s) => (s.id === res.id ? { ...s, name: res.name } : s));
      UI.renderShopOptions();
      render();
      UI.ok(`店铺已改名为「${res.name}」`);
    } catch (e) { UI.apiFail(e, "改名失败"); }
  }

  async function deleteShop(ev) {
    const id = Number(ev.currentTarget.dataset.del);
    const shop = shops.find((s) => s.id === id);
    if (!shop) return;
    const isCurrent = String(id) === String(currentId());
    const confirmed = await UI.confirmDialog({
      title: `删除店铺「${shop.name}」`,
      bodyHtml: `<p>将清除该店铺的<b>商品、规格、自定义字段、库存流水</b>及其他全部所属数据，` +
                `<b style="color:var(--danger)">不可恢复</b>。</p>` +
                (isCurrent ? `<p>删除的是当前店铺，完成后会自动切换到另一间店铺。</p>` : ""),
      confirmText: "确认删除", danger: true,
    });
    if (!confirmed) return;
    try {
      const res = await XM.del(`/shops/${id}?confirm=true`);
      shops = shops.filter((s) => s.id !== id);
      // 本地记录同步清理；删的是当前店铺时，本地顺序中的第一个剩余店铺成为当前店铺
      UI.forgetShop(id, shops);
      XM.state.shopId = Number(localStorage.getItem(UI.SHOP_KEY)) || null;
      XM.state.shops = shops;
      UI.renderShopOptions();
      // 删到零店铺时，顶栏原选择框位置改为「新建店铺」
      UI.applyNoShop(shops.length > 0);
      render();
      UI.ok(`店铺「${res.name}」已删除`);
      // 当前店铺变化后，页面上下文（业务数据）需要重新读取
      if (isCurrent && shops.length) setTimeout(() => location.reload(), 400);
    } catch (e) { UI.apiFail(e, "删除店铺失败"); }
  }

  async function load() {
    const box = list();
    try {
      const data = await XM.get("/shops");
      XM.state.shops = UI.applyShopOrder(data.shops);
      shops = XM.state.shops;
      // 当前选择优先沿用本地记录；记录不存在时取本地顺序里的第一间店铺
      if (!shops.some((s) => String(s.id) === String(XM.state.shopId))) {
        XM.state.shopId = shops.length ? shops[0].id : null;
        if (shops.length) localStorage.setItem(UI.SHOP_KEY, String(shops[0].id));
      }
      UI.renderShopOptions();
      // 顶栏跟随实际店铺数：有店铺显示选择框，零店铺显示「新建店铺」
      UI.applyNoShop(shops.length > 0);
      render();
    } catch (e) {
      // 读取失败不等于空列表：不能显示「还没有店铺」
      box.innerHTML = `<div class="t-empty">店铺列表读取失败：${UI.esc(e.display || e.message)}</div>`;
    }
  }

  return { load, onDragOver };
})();

window.PAGE_INIT = async function () {
  // 本页不使用 X-Shop-Id，零店铺时也必须能用
  $("#btnCreateShop").addEventListener("click", async () => {
    await UI.createShopFlow();
  });
  const box = $("#shopList");
  box.addEventListener("dragover", ShopsPage.onDragOver);
  // 拖到列表空白处时阻止浏览器默认处理，保证落点稳定
  box.addEventListener("drop", (ev) => ev.preventDefault());
  await ShopsPage.load();
};
