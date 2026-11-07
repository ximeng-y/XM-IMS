/* 通用右键菜单：拦截浏览器原生右键，按调用方给出的菜单项渲染自建菜单

   - install() 由外壳在普通页面初始化时调用一次；首启页不安装，保留原生右键
   - 全站拦截 contextmenu（输入框/文本域/可编辑区域除外，保留复制粘贴）
   - 菜单项由调用方通过 open(event, items) 提供：[{ label, disabled, danger, onClick }]
   - 单例：任何时刻最多一个菜单；Escape / 点击空白 / 滚动 / 缩放 / 再次右键都会关闭
   - 靠近视口右、下边缘时自动反向定位，保证菜单完整可见 */
"use strict";

const MARGIN = 8;                  // 菜单与视口边缘的最小间距
const EDITABLE = "input, textarea, select, [contenteditable]";
let box = null;                    // 当前菜单容器；null 表示没有打开的菜单
let installed = false;

const editable = (el) => !!(el && el.closest && el.closest(EDITABLE));

function close() {
  if (!box) return;
  box.remove();
  box = null;
}

function open(event, items) {
  close();
  // 编辑区域只关闭旧菜单，保留原生复制粘贴；调用方无需提前拦截事件。
  if (editable(event.target)) return;
  // 同步阻止本次右键继续冒泡，避免 document 的关闭逻辑误关刚打开的菜单。
  event.preventDefault();
  event.stopPropagation();
  if (!items || !items.length) return;
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
}

// 未被 open() 接管的右键：编辑区域关闭旧菜单并保留原生菜单，其余区域继续拦截。
function install() {
  if (installed) return;
  installed = true;
  document.addEventListener("contextmenu", (e) => {
    if (editable(e.target)) {
      close();
      return;
    }
    e.preventDefault();
    // 右键落在菜单自身时不关闭，其余位置关闭已有菜单。
    if (!e.target.closest(".ctx-menu")) close();
  });
  document.addEventListener("click", (e) => {
    if (box && !e.target.closest(".ctx-menu")) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
  window.addEventListener("scroll", close, true);
  window.addEventListener("resize", close);
}

export const ContextMenu = { open, close, install };
