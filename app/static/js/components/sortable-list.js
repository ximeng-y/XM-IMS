/* 通用拖拽排序：按住手柄拖动列表项，其余项实时让位，松手后回调新顺序

   - 只处理指针与 DOM，不含业务含义；顺序变化后由 onEnd 通知调用方自行落盘
   - 被拖项留在文档流里，它的空槽就是落点位置；DOM 位置随指针变化，
     被挤开的项用 FLIP 动画滑到新位置
   - 用 Pointer Events 而不是 HTML5 拖放：原生拖放的跟随图像由浏览器绘制，
     既不能参与布局也不会实时让位，触屏上更是不工作
   - 指针靠近滚动区域上下边缘时自动滚动
   - 配套样式见 css/components/sortable-list.css */
"use strict";

const EDGE = 56;                       // 触发自动滚动的边缘宽度（px）
const MAX_SPEED = 16;                  // 自动滚动每帧的最大像素
const FLIP_MS = 180;                   // 让位与落位动画时长
const FLIP_EASE = "cubic-bezier(.2,.8,.2,1)";

const reducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

// opts:
//   handle  拖拽起点的选择器（必填），只有从这些元素上按下才会开始拖动
//   item    可拖项的选择器，默认容器内所有直接子元素
//   onEnd   可选，拖动结束且顺序已落到 DOM 后调用，参数为按新顺序排列的元素数组
function create(container, { handle, item, onEnd } = {}) {
  if (!container) throw new TypeError("SortableList 需要一个容器元素");
  if (!handle) throw new TypeError("SortableList 需要 handle 选择器");

  const items = () => [...container.children].filter((el) => !item || el.matches(item));
  const anims = new WeakMap();         // 每个让位动画只保留最新的一个，新动画前先撤旧的

  // 从按下点向上找到本次拖动的项：必须是容器的直接子元素之一
  function itemOf(node) {
    if (node === container || !container.contains(node)) return null;
    let el = node;
    while (el && el.parentElement !== container) el = el.parentElement;
    return el && (!item || el.matches(item)) ? el : null;
  }

  let drag = null;
  let pendingCleanup = null;           // 上一次落位动画的收尾；新动画开始前必须先执行

  // 上一段落位动画若还没播完就开始了新动作，先按终态收尾：
  // 否则被取消的动画不会再触发回调，浮起状态会一直留在旧项上
  function flushPending() {
    const c = pendingCleanup;
    pendingCleanup = null;
    if (c) c();
  }

  // el 从给定的视觉位移滑回自然位置，结束后清掉内联位移并运行 done
  function settle(el, fromY, done) {
    flushPending();
    let anim = null;
    let settled = false;
    const finishOnce = () => {
      if (settled) return;
      settled = true;
      if (pendingCleanup === finishOnce) pendingCleanup = null;
      // 先写回自然位置再撤动画：两步在同一任务内完成，中间不会绘制出旧位移
      el.style.transform = "";
      if (anim) anim.cancel();
      if (done) done();
    };
    pendingCleanup = finishOnce;
    if (Math.abs(fromY) < 1 || reducedMotion()) { finishOnce(); return; }
    anim = el.animate(
      [{ transform: `translateY(${fromY}px)` }, { transform: "translateY(0)" }],
      { duration: FLIP_MS, easing: FLIP_EASE, fill: "forwards" }
    );
    anim.onfinish = finishOnce;
    // 元素在动画途中被移除时动画只会被取消，同样要按终态收尾
    anim.oncancel = finishOnce;
  }

  // 把 el 插到 before 之前（null 表示末尾），其余项从各自的视觉位置滑到新位置。
  // 返回 el 因这次挪位产生的布局位移，调用方据此反向补偿自身位移
  function flipMove(el, before) {
    const others = items().filter((x) => x !== el);
    // 先记录各项目前的视觉位置（含尚未播完的让位动画），再撤掉旧动画，
    // 之后的测量才是干净的布局位置；中间不绘制，看不到跳动
    const tops = others.map((x) => x.getBoundingClientRect().top);
    others.forEach((x) => {
      const a = anims.get(x);
      if (a) { a.cancel(); anims.delete(x); }
    });
    const fromTop = el.offsetTop;
    container.insertBefore(el, before || null);
    if (!reducedMotion()) {
      others.forEach((x, i) => {
        const delta = tops[i] - x.getBoundingClientRect().top;
        if (Math.abs(delta) < 1) return;
        anims.set(x, x.animate(
          [{ transform: `translateY(${delta}px)` }, { transform: "translateY(0)" }],
          { duration: FLIP_MS, easing: FLIP_EASE }
        ));
      });
    }
    return el.offsetTop - fromTop;
  }

  // 落点判定用布局坐标（offsetTop）：正在让位的项带着 transform，
  // 按 rect 比较会把动画位移算进去，落点会在两项之间来回抖
  function dropBefore() {
    const el = drag.el;
    const mid = el.offsetTop + el.offsetHeight / 2 + drag.ty;
    return items().find((x) => x !== el && mid < x.offsetTop + x.offsetHeight / 2) || null;
  }

  function updateOrder() {
    const el = drag.el;
    const before = dropBefore();
    const current = items();
    const others = current.filter((x) => x !== el);
    const desired = others.slice();
    desired.splice(before ? others.indexOf(before) : others.length, 0, el);
    if (desired.every((x, i) => x === current[i])) return;
    // DOM 挪位会改变 el 的布局位置，反向补偿让它在屏幕上原地不动
    const delta = flipMove(el, before);
    drag.changed = true;
    if (delta) {
      drag.ty -= delta;
      el.style.transform = `translateY(${drag.ty}px)`;
    }
  }

  // 跟随指针：用当前布局位置反推位移，页面滚动与 DOM 挪位都自动算进去
  function place() {
    const el = drag.el;
    const natural = el.getBoundingClientRect().top - drag.ty;
    const ty = drag.startTop + (drag.pointerY - drag.startY) - natural;
    if (Math.abs(ty - drag.ty) < 0.1) return;
    drag.ty = ty;
    el.style.transform = `translateY(${ty}px)`;
  }

  // 指针进入滚动区域的上下边缘时持续滚动，越靠边越快
  function autoScroll() {
    const scroller = drag.scroller;
    const view = scroller === document.scrollingElement
      ? { top: 0, bottom: window.innerHeight }
      : scroller.getBoundingClientRect();
    const y = drag.pointerY;
    const over = Math.max(view.top + EDGE - y, y - (view.bottom - EDGE));
    if (over <= 0) return;
    const step = Math.min(1, over / EDGE) * MAX_SPEED;
    scroller.scrollBy(0, y < view.top + EDGE ? -step : step);
  }

  // 拖动项所在的可滚动祖先；没有就滚整个页面
  function scrollerOf(el) {
    for (let node = el.parentElement; node && node !== document.body; node = node.parentElement) {
      const oy = getComputedStyle(node).overflowY;
      if ((oy === "auto" || oy === "scroll") && node.scrollHeight > node.clientHeight + 1) return node;
    }
    return document.scrollingElement || document.documentElement;
  }

  // 拖动期间逐帧推进：先滚动，再跟随指针，最后按最新位置判定落点
  function tick() {
    if (!drag) return;
    autoScroll();
    place();
    updateOrder();
    drag.raf = requestAnimationFrame(tick);
  }
  function start(e, el) {
    if (items().length < 2) return;
    // 上一项的落位动画可能还在播，先让它按终态收尾（清除浮起状态）
    flushPending();
    // 捕获挂在容器上而非手柄：拖动中项会被 insertBefore 挪位，
    // 挪位等于先从文档移除再插回，挂在被挪元素上的捕获会随之丢失
    container.setPointerCapture(e.pointerId);
    // 先进入拖动态再测量：拖动态会压掉页面自带的过渡与悬停位移，
    // 此时读到的才是干净的起始位置，松手时不会跳一下
    container.classList.add("sortable-active");
    el.classList.add("sortable-lifted");
    // 上一次落位动画可能还没播完，先撤掉，否则它的关键帧会盖住本次的跟随位移
    el.getAnimations().forEach((a) => a.cancel());
    el.style.transform = "translateY(0px)";
    drag = {
      el, pointerId: e.pointerId, changed: false,
      startIndex: items().indexOf(el),
      startY: e.clientY, pointerY: e.clientY,
      startTop: el.getBoundingClientRect().top,
      ty: 0, raf: 0,
      scroller: scrollerOf(el),
    };
    window.addEventListener("keydown", onKey);
    drag.raf = requestAnimationFrame(tick);
  }

  function finish(commit) {
    const d = drag;
    if (!d) return;
    drag = null;
    cancelAnimationFrame(d.raf);
    window.removeEventListener("keydown", onKey);
    if (container.hasPointerCapture(d.pointerId)) container.releasePointerCapture(d.pointerId);
    const done = () => {
      d.el.classList.remove("sortable-lifted");
      container.classList.remove("sortable-active");
    };
    // 只是点了一下手柄、顺序没变：不需要回插，也不需要回调
    if (!d.changed) {
      settle(d.el, d.ty, done);
      return;
    }
    if (commit) {
      settle(d.el, d.ty, done);
      // DOM 已是最终顺序，调用方只需落盘，不要再重建列表，
      // 否则正在播放的落位动画会随元素一起被替换掉
      if (onEnd) onEnd(items());
      return;
    }
    // 取消：先把项放回原槽位，再按布局位移补偿，让它从视觉当前位置滑回去
    const others = items().filter((x) => x !== d.el);
    const back = flipMove(d.el, others[d.startIndex] || null);
    settle(d.el, d.ty - back, done);
  }

  function onPointerDown(e) {
    if (drag) return;
    if (e.pointerType === "mouse" && e.button !== 0) return;
    const handleEl = e.target.closest ? e.target.closest(handle) : null;
    if (!handleEl || !container.contains(handleEl)) return;
    const el = itemOf(handleEl);
    if (!el) return;
    // 阻止文字选中与浏览器自带的原生拖动
    e.preventDefault();
    start(e, el);
  }

  function onPointerMove(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    drag.pointerY = e.clientY;
  }

  function onPointerUp(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    finish(true);
  }

  // 浏览器接管指针（如触屏手势）时撤销本次拖动，顺序回到开始前
  function onPointerCancel(e) {
    if (!drag || e.pointerId !== drag.pointerId) return;
    finish(false);
  }

  // 捕获意外丢失（如拖动中元素被移除）时按已落地的顺序收尾
  function onLostCapture(e) {
    if (drag && e.pointerId === drag.pointerId) finish(true);
  }

  function onKey(e) {
    if (e.key !== "Escape") return;
    e.preventDefault();
    finish(false);
  }

  container.addEventListener("pointerdown", onPointerDown);
  container.addEventListener("pointermove", onPointerMove);
  container.addEventListener("pointerup", onPointerUp);
  container.addEventListener("pointercancel", onPointerCancel);
  container.addEventListener("lostpointercapture", onLostCapture);

  return {
    destroy() {
      finish(false);
      container.removeEventListener("pointerdown", onPointerDown);
      container.removeEventListener("pointermove", onPointerMove);
      container.removeEventListener("pointerup", onPointerUp);
      container.removeEventListener("pointercancel", onPointerCancel);
      container.removeEventListener("lostpointercapture", onLostCapture);
    },
  };
}

export const SortableList = { create };
