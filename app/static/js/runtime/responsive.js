/* 窄屏导航浮层与表格横向滑动提示。
   断点与 CSS 保持一致：不超过 860px 视为窄屏（浮层导航 + 表格组件内滚动）。 */
"use strict";

import { $, $$ } from "xm/ui";

const NARROW = window.matchMedia("(max-width: 860px)");
const isNarrow = () => NARROW.matches;

// 窄屏顶栏高度随内容变化（换行或店铺区显示与否）；浮层与遮罩都贴在其下沿，
// 故把实测高度写进 --mobile-topbar-height 供两者定位
function watchTopbar() {
  const bar = $(".topbar");
  if (!bar) return;
  const apply = () => {
    document.documentElement.style.setProperty(
      "--mobile-topbar-height", bar.getBoundingClientRect().height + "px");
  };
  apply();
  // 同步状态文案、店铺区域显示与否或视口变化都会改变顶栏尺寸
  new ResizeObserver(apply).observe(bar);
}

const navOpen = () => document.body.classList.contains("nav-open");

// 浮层期间的背景隔离：CSS 的 pointer-events 只挡指针命中，Tab / Shift+Tab
// 仍会走进被遮罩盖住的正文与顶栏控件（Enter 照常触发），故这里再给它们加 inert。
// 右侧栏是 body 下的浮层、层级低于导航浮层，同样会被遮罩盖住，必须一并隔离
function setNavInert(on) {
  $$("main.content, .topbar > *:not(#navToggle), .right-sidebar").forEach((el) => {
    el.toggleAttribute("inert", on);
  });
}

// 关闭浮层：清标记、撤遮罩与滚动锁，焦点可选地回到菜单按钮
function setNavClosed({ focusBtn = false } = {}) {
  const wasOpen = navOpen();
  document.body.classList.remove("nav-open");
  // 先恢复交互再还原焦点：焦点不能落在 inert 元素内
  setNavInert(false);
  const backdrop = $("#navBackdrop");
  if (backdrop) backdrop.hidden = true;
  const btn = $("#navToggle");
  if (!btn) return;
  btn.setAttribute("aria-expanded", "false");
  btn.setAttribute("aria-label", "打开导航菜单");
  if (wasOpen && focusBtn) btn.focus();
}

function setNavOpen() {
  const nav = $("#primaryNav");
  if (!nav) return;
  document.body.classList.add("nav-open");
  setNavInert(true);
  const backdrop = $("#navBackdrop");
  if (backdrop) backdrop.hidden = false;
  const btn = $("#navToggle");
  if (btn) {
    btn.setAttribute("aria-expanded", "true");
    btn.setAttribute("aria-label", "关闭导航菜单");
  }
  // 焦点移到当前页面对应的可见导航项，键盘用户可直接继续操作
  const current = $$("a", nav).find((a) => !a.hidden && a.classList.contains("active"));
  if (current) current.focus();
}

// 从窄屏切回桌面时清掉浮层，不留遮罩、滚动锁与不可交互状态
function onNarrowChange() {
  if (!isNarrow() && navOpen()) setNavClosed();
  scheduleHintUpdate();
}

// 点击的是当前页面自身：阻止默认跳转，避免整页重载后正文入场动画重播。
// 带修饰键或非左键的点击不拦截，保留浏览器的新标签打开与下载等原生行为。
function samePageClick(a, e) {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return false;
  const page = document.body.dataset.page;
  return !!page && a.dataset.nav === page;
}

function initNav() {
  const nav = $("#primaryNav"), backdrop = $("#navBackdrop"), btn = $("#navToggle");
  if (!nav || !btn || !backdrop) return;
  btn.addEventListener("click", () => {
    if (navOpen()) setNavClosed({ focusBtn: true });
    else setNavOpen();
  });
  backdrop.addEventListener("click", () => setNavClosed({ focusBtn: true }));
  // 选择页面后立即收起浮层；点的是当前页面时只收起，不重新加载
  nav.addEventListener("click", (e) => {
    const a = e.target.closest("a");
    if (!a) return;
    if (samePageClick(a, e)) e.preventDefault();
    setNavClosed();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && navOpen()) {
      // 已由导航浮层消费：右侧栏等其它浮层看到 defaultPrevented 就不再跟着关闭
      e.preventDefault();
      setNavClosed({ focusBtn: true });
    }
  });
  NARROW.addEventListener("change", onNarrowChange);
  watchTopbar();
}

// ---------- 表格横向滑动提示 ----------
// 每个滚动容器后跟一个提示节点（只创建一次，不放进滚动区域内）。
// 仅窄屏且确实横向溢出时显示；尺寸变化用 rAF 合并，不做定时轮询。
function ensureHints() {
  $$("main.content .table-scroll").forEach((scroller) => {
    const next = scroller.nextElementSibling;
    if (next && next.classList.contains("table-scroll-hint")) return;
    const hint = document.createElement("div");
    hint.className = "table-scroll-hint";
    hint.textContent = "左右滑动查看其余列";
    scroller.after(hint);
  });
}

function refreshHints() {
  const narrow = isNarrow();
  $$("main.content .table-scroll-hint").forEach((hint) => {
    const scroller = hint.previousElementSibling;
    const overflowing = !!scroller && scroller.scrollWidth > scroller.clientWidth + 1;
    hint.classList.toggle("show", narrow && overflowing);
  });
}

let hintFrame = 0;
function scheduleHintUpdate() {
  if (hintFrame) return;
  hintFrame = requestAnimationFrame(() => { hintFrame = 0; refreshHints(); });
}

function watchHints() {
  ensureHints();
  const scrollers = $$("main.content .table-scroll");
  if (!scrollers.length) return;
  const ro = new ResizeObserver(scheduleHintUpdate);
  // 表格内容变化（数据渲染、展开行）会改变 scrollWidth，容器自身尺寸未必变
  const mo = new MutationObserver(scheduleHintUpdate);
  scrollers.forEach((s) => {
    ro.observe(s);
    mo.observe(s, { childList: true, subtree: true });
  });
  refreshHints();
}

// 导航与提示先就位：启动检查阶段（含数据同步页）也要能用菜单
export function initResponsive() {
  initNav();
  watchHints();
  window.addEventListener("resize", scheduleHintUpdate);
}
