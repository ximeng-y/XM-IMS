/* 通用右键菜单：拦截浏览器原生右键，按调用方给出的菜单项渲染自建菜单

   - 全站拦截 contextmenu（输入框/文本域/可编辑区域除外，保留复制粘贴）
   - 菜单项由调用方通过 open(event, items) 提供：[{ label, disabled, danger, onClick }]
   - 单例：任何时刻最多一个菜单；Escape / 点击空白 / 滚动 / 缩放 / 再次右键都会关闭
   - 靠近视口右、下边缘时自动反向定位，保证菜单完整可见 */
"use strict";

const ContextMenu = (() => {
  const MARGIN = 8;                  // 菜单与视口边缘的最小间距
  const EDITABLE = "input, textarea, select, [contenteditable]";
  let box = null;                    // 当前菜单容器；null 表示没有打开的菜单
  let opening = false;               // open() 执行中：本轮的 contextmenu 不得把新菜单关掉

  const editable = (el) => !!(el && el.closest && el.closest(EDITABLE));

  function close() {
    if (!box) return;
    box.remove();
    box = null;
  }

  function open(event, items) {
    close();
    if (!items || !items.length) return;
    opening = true;
    box = document.createElement("div");
    box.className = "ctx-menu";
    for (const item of items) {
      const el = document.createElement("button");
      el.type = "button";
      el.className = "ctx-menu-item" + (item.disabled ? " disabled" : "") +
                     (item.danger ? " danger" : "");
      el.textContent = item.label;
      el.disabled = !!item.disabled;
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        close();
        if (item.onClick) item.onClick();
      });
      box.appendChild(el);
    }
    document.body.appendChild(box);
    const rect = box.getBoundingClientRect();
    let x = event.clientX;
    let y = event.clientY;
    if (x + rect.width + MARGIN > window.innerWidth) x = Math.max(MARGIN, x - rect.width);
    if (y + rect.height + MARGIN > window.innerHeight) y = Math.max(MARGIN, y - rect.height);
    box.style.left = `${x}px`;
    box.style.top = `${y}px`;
    // 让下一次右键能正常关闭菜单；本轮事件（preventDefault 已在更早的 document
    // 监听器里做过）结束后复位
    queueMicrotask(() => { opening = false; });
  }

  // 拦截浏览器原生右键：输入框等编辑区域保持原样
  document.addEventListener("contextmenu", (e) => {
    if (editable(e.target)) return;
    e.preventDefault();
    // 正在开菜单（调用方的 contextmenu 处理器紧接着会调用 open）或右键落在菜单自身时
    // 都不关闭；其余位置只拦截，已有菜单顺带关掉
    if (!opening && !e.target.closest(".ctx-menu")) close();
  });
  document.addEventListener("click", (e) => {
    if (box && !e.target.closest(".ctx-menu")) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
  window.addEventListener("scroll", close, true);
  window.addEventListener("resize", close);

  return { open, close };
})();
