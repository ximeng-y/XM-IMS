/* 库存页视口：限高滚动容器、一次性底部留白与行定位。
   只接收实际 DOM 节点，不认识业务编号；查找 SPU/SKU 行由页面控制器负责。 */
"use strict";

const MIN_SCROLL_H = 160;       // 10rem：窗口极矮时保底，仍不足则由整页滚动兜底

export function createViewport({ scroller, panel, tail, layoutNodes = [], hintOf = () => null }) {
  let tailInitial = 0;     // 本次定位补足的留白总量（上限，只减不增）
  let tailAmount = 0;      // 当前留白高度
  let tailAnchor = 0;      // 定位完成时的滚动位置，用于换算已退出的尾部空间
  let lastScrollTop = 0;   // 上一次滚动位置，用于判断滚动方向

  // 元素在文档中的布局位置：offsetTop 链不受 fade-in 的 translateY 影响
  function docTop(el) {
    let top = 0;
    for (let node = el; node; node = node.offsetParent) top += node.offsetTop || 0;
    return top;
  }

  // 容器最大可视高度 = 视口高度 − 容器上沿文档位置 − 外层面板下内边距与下边框 − 页面下方既有留白
  //   − 表格下方横向滑动提示的占位（窄屏显示时才有，隐藏时为 0）
  function hintHeight() {
    const hint = hintOf();
    if (!hint) return 0;
    const h = hint.getBoundingClientRect().height;   // display:none 时为 0
    if (!h) return 0;
    const cs = getComputedStyle(hint);
    return h + (parseFloat(cs.marginTop) || 0) + (parseFloat(cs.marginBottom) || 0);
  }

  function computeMax() {
    if (!scroller || !panel) return 0;
    const pcs = getComputedStyle(panel);
    const panelBottom = (parseFloat(pcs.paddingBottom) || 0) + (parseFloat(pcs.borderBottomWidth) || 0);
    const main = document.querySelector("main.content");
    const bottomGap = (main ? (parseFloat(getComputedStyle(main).paddingBottom) || 0) : 0) +
                      (parseFloat(getComputedStyle(document.body).marginBottom) || 0);
    const avail = window.innerHeight - docTop(scroller) - panelBottom - bottomGap - hintHeight();
    return Math.max(MIN_SCROLL_H, Math.floor(avail));
  }

  function applyMax() {
    // 用 max-height 而非固定高度：数据少时自然收缩，不撑出空白
    if (scroller) scroller.style.maxHeight = computeMax() + "px";
  }

  function setTail(px) {
    if (!tail) return;
    tailInitial = Math.max(0, Math.round(px));
    tailAmount = tailInitial;
    tail.hidden = tailAmount === 0;
    tail.style.height = tailAmount + "px";
  }

  // 约定：底部留白只在跳转定位时补，其它路径一律清除。
  // 每次重渲染都会调用本函数，因此「补留白 → 重渲染」不会互相打架；
  // 搜索自动展开期间的重渲染会顺带清掉跳转留下的留白，这是预期行为，不会重新居中。
  function clearTail() {
    if (!tail) return;
    tailInitial = 0;
    tailAmount = 0;
    tail.hidden = true;
    tail.style.height = "0px";
  }

  // 只回收：留白总量减去累计上滚距离，且不得超过当前高度（向下滚不会把留白要回来）
  function onScroll() {
    if (!scroller) return;
    const st = scroller.scrollTop;
    const up = st < lastScrollTop;
    lastScrollTop = st;
    if (!tailInitial || !up || !tail) return;
    const next = Math.min(tailAmount, Math.max(0, tailInitial - (tailAnchor - st)));
    if (next === tailAmount) return;
    tailAmount = next;
    tail.style.height = next + "px";
    if (next === 0) tail.hidden = true;
  }

  // 任意库存行（SPU 行或 SKU 子行）位于最大可视区域上半区时不滚动；下半区则即时定位到中线
  // 返回目标行是否存在并完成了定位判定（已在半区上半时也算完成，只是不滚动）
  function centerRow(row) {
    if (!scroller || !row || !scroller.contains(row)) return false;
    // 基准取容器实际可视内容高度（clientHeight 已扣掉横向滚动条厚度），
    // 不用短表当前自然高度的一半，也不用「限高 − 滚动条」反推
    const viewH = Math.max(MIN_SCROLL_H, scroller.clientHeight);
    const scRect = scroller.getBoundingClientRect();
    const rRect = row.getBoundingClientRect();
    const rowCenter = rRect.top - scRect.top + scroller.scrollTop + rRect.height / 2;
    const targetCenter = viewH / 2;
    if (rowCenter <= targetCenter) return true;     // 已在上半区，不滚动
    const desired = rowCenter - targetCenter;
    const naturalMax = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    // 自然内容不够长时临时补足尾部高度，保证目标能严格到达中线
    if (desired > naturalMax) setTail(desired - naturalMax);
    scroller.scrollTop = desired;                   // 只动容器，不联动页面与横向滚动
    tailAnchor = scroller.scrollTop;
    lastScrollTop = scroller.scrollTop;
    return true;
  }

  function install() {
    applyMax();
    // 观察上方各块的实际占位变化（操作区换行、侧栏与标题高度变化）
    // 以及表格下方提示的出现/消失（窄屏断点切换会改变其占位）
    const ro = new ResizeObserver(() => applyMax());
    [...layoutNodes, hintOf()].filter(Boolean).forEach((el) => ro.observe(el));
    if (scroller) {
      lastScrollTop = scroller.scrollTop;
      scroller.addEventListener("scroll", onScroll);
    }
    window.addEventListener("resize", () => { clearTail(); applyMax(); });
  }

  return { install, applyMax, clearTail, centerRow };
}
