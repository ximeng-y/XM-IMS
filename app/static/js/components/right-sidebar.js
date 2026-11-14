/* 通用右侧栏：固定在视口右侧的浮层，打开、关闭与调宽都不改变正文宽度与页面布局

   - 非模态：没有全屏遮罩，背景页面继续可操作，点击背景也不自动关闭
   - 右边缘固定，拖动左边缘调整宽度（向左拉宽、向右收窄）；窄屏改用接近全宽的侧栏，不提供拖动
   - 标题、关闭按钮与底部操作区常驻，长内容在面板内部滚动
   - 点击关闭按钮或按 Esc 关闭；正在调宽时 Esc 只取消本次拖动。
     内容区正在拖动时（调用方的 holdEscape 为真），Esc 留给内容组件取消拖动
   - 只负责容器、开关、调宽与滚动区域，不含页面名分支、店铺逻辑或保存接口；
     初始宽度与内容由调用方提供，调宽结束后经 onWidthChange 交回调用方自行落盘
*/
"use strict";

const MIN_WIDTH = 280;
const MAX_WIDTH = 640;
const DEFAULT_WIDTH = 360;
const NARROW = window.matchMedia("(max-width: 860px)");

let opened = null;   // 当前打开的侧栏；同一时刻只保留一个

function clampWidth(width) {
  // 最大宽度受当前视口限制，窄窗口下仍能留出可见的背景页面
  const max = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, window.innerWidth - 48));
  const w = Number.isFinite(Number(width)) ? Math.round(Number(width)) : DEFAULT_WIDTH;
  return Math.min(Math.max(w, MIN_WIDTH), max);
}

// opts:
//   title         标题文字（纯文本）
//   width         初始宽度（px）；空值使用默认宽度
//   content       内容节点，追加到面板内部的可滚动区域
//   footer        可选，底部常驻操作区的节点
//   onWidthChange 可选，调宽结束时回调新宽度（px）
//   onClose       可选，侧栏关闭后回调
//   holdEscape    可选，返回 true 表示内容区正在拖动，此时 Esc 不关闭侧栏
function open({ title, width, content, footer, onWidthChange, onClose, holdEscape } = {}) {
  close();
  const panel = document.createElement("aside");
  panel.className = "right-sidebar";
  panel.setAttribute("role", "complementary");
  panel.setAttribute("aria-label", title || "侧栏");
  // 侧栏自身可聚焦：打开时把焦点移进来，键盘与读屏用户直接从侧栏开始操作
  panel.tabIndex = -1;

  const grip = document.createElement("div");
  grip.className = "right-sidebar-grip";
  grip.setAttribute("aria-hidden", "true");

  const head = document.createElement("div");
  head.className = "right-sidebar-head";
  const heading = document.createElement("h2");
  heading.className = "right-sidebar-title";
  heading.textContent = title || "";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "right-sidebar-close";
  closeBtn.setAttribute("aria-label", "关闭侧栏");
  closeBtn.title = "关闭";
  closeBtn.textContent = "×";
  head.append(heading, closeBtn);

  const body = document.createElement("div");
  body.className = "right-sidebar-body";
  if (content) body.appendChild(content);

  const foot = document.createElement("div");
  foot.className = "right-sidebar-foot";
  if (footer) foot.appendChild(footer);

  panel.append(grip, head, body, foot);
  document.body.appendChild(panel);

  const state = { panel, body, grip, width: clampWidth(width), onClose, holdEscape };

  // 窄屏不提供调宽：宽度交给样式表，回到宽屏时恢复用户此前的宽度
  const applyWidth = () => {
    panel.style.width = NARROW.matches ? "" : state.width + "px";
  };
  applyWidth();

  const onNarrowChange = () => applyWidth();
  NARROW.addEventListener("change", onNarrowChange);
  state.unbindNarrow = () => NARROW.removeEventListener("change", onNarrowChange);

  state.unbindGrip = bindGrip(state, (w) => {
    if (onWidthChange) onWidthChange(w);
  });
  state.unbindKey = bindEscape(state);
  // 记住打开前的焦点元素，关闭后焦点回到它（通常是「列设置」按钮）
  const returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  state.returnFocus = returnFocus;
  closeBtn.addEventListener("click", () => close());

  opened = state;
  body.scrollTop = 0;
  // 焦点移入侧栏（不抢在布局之前），关闭时由 close() 还给打开前的元素
  panel.focus({ preventScroll: true });
  return {
    panel,
    body,
    foot,
    setTitle(text) { heading.textContent = text; },
    close: () => close(),
  };
}

// Esc 关闭侧栏。以下情况 Esc 属于更上层，不能同时关掉侧栏：
//   - 上层还有原生模态弹窗（新建、编辑、确认等）：Esc 只关那个弹窗
//   - 窄屏导航浮层展开中：Esc 只收起导航，它已经 preventDefault 标记过了
//   - 侧栏内容正在拖动（调用方的 holdEscape 声明）：Esc 是取消拖动
function bindEscape(state) {
  const onKey = (e) => {
    if (e.key !== "Escape" || opened !== state) return;
    if (document.querySelector("dialog:modal") || e.defaultPrevented) return;
    if (state.holdEscape && state.holdEscape()) return;
    e.preventDefault();
    close();
  };
  document.addEventListener("keydown", onKey);
  return () => document.removeEventListener("keydown", onKey);
}

// 左边缘拖动调宽：指针位移直接映射为宽度变化，松手后才回调新宽度
function bindGrip(state, onDone) {
  const { grip, panel } = state;
  let drag = null;

  const onDown = (e) => {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    grip.classList.add("is-dragging");
    drag = { id: e.pointerId, startX: e.clientX, startWidth: state.width };
  };

  const onMove = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    // 指针向左移时 clientX 变小、侧栏变宽，故取相反数
    const width = clampWidth(drag.startWidth + (drag.startX - e.clientX));
    panel.style.width = width + "px";
    drag.width = width;
  };

  const finish = (commit) => {
    if (!drag) return;
    const d = drag;
    drag = null;
    if (grip.hasPointerCapture(d.id)) grip.releasePointerCapture(d.id);
    grip.classList.remove("is-dragging");
    if (commit && d.width && d.width !== state.width) {
      state.width = d.width;
      onDone(d.width);
    } else {
      panel.style.width = NARROW.matches ? "" : state.width + "px";
    }
  };

  const onKey = (e) => {
    if (e.key !== "Escape" || !drag) return;
    // 调宽中的 Esc 只取消本次拖动，不关闭侧栏
    e.preventDefault();
    e.stopPropagation();
    finish(false);
  };

  grip.addEventListener("pointerdown", onDown);
  grip.addEventListener("pointermove", onMove);
  grip.addEventListener("pointerup", () => finish(true));
  grip.addEventListener("pointercancel", () => finish(false));
  grip.addEventListener("lostpointercapture", () => finish(true));
  window.addEventListener("keydown", onKey, true);
  return () => {
    window.removeEventListener("keydown", onKey, true);
    finish(false);
  };
}

function close() {
  const state = opened;
  if (!state) return;
  opened = null;
  state.unbindKey();
  state.unbindGrip();
  state.unbindNarrow();
  state.panel.remove();
  // 关闭后焦点回到打开前的元素，不落到页面开头；打开前焦点本就在页面上时无需处理
  const back = state.returnFocus;
  if (back && back !== document.body && back.isConnected) back.focus();
  if (state.onClose) state.onClose();
}

const isOpen = () => opened !== null;

export const RightSidebar = { open, close, isOpen, DEFAULT_WIDTH, MIN_WIDTH, MAX_WIDTH };
