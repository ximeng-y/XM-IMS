/* 列设置内容组件：在通用右侧栏内列出当前页的全部列，支持拖动排序、隐藏与恢复

   - 列表项固定按「名称 → 隐藏/显示按钮 → 拖拽手柄」排列，手柄在最右侧；
     展开控制、操作一类交互控件没有隐藏按钮，只能排序
   - 隐藏的列仍留在原位置，可以继续排序，也可以点「显示」恢复
   - 排序复用通用 SortableList（与店铺管理页同一套动效），不复制拖拽实现；
     列较多时允许一路拖到列表底部（放开默认的向下滚动额度）
   - 只负责展示与交互通知，不获取店铺、业务数据，也不保存偏好：
     顺序或隐藏状态变化后经 onChange 交回调用方
*/
"use strict";

import { SortableList } from "xm/components/sortable-list";

// 已保存顺序 → 当前列的完整顺序：
// 保留仍存在的列，新出现的列按默认位置追加到末尾，失效的列丢弃
function resolveOrder(columns, settings) {
  const keys = columns.map((c) => c.key);
  const known = new Set(keys);
  const controls = new Set(columns.filter((c) => c.control).map((c) => c.key));
  const saved = (settings && settings.order) || [];
  const order = [...new Set(saved.filter((k) => known.has(k)))];
  keys.forEach((k) => { if (!order.includes(k)) order.push(k); });
  const savedHidden = new Set((settings && settings.hidden) || []);
  return {
    order,
    hidden: keys.filter((k) => !controls.has(k) && savedHidden.has(k)),
  };
}

// 当前可见的列（保持完整顺序，去掉已隐藏的信息列）
function visibleKeys(columns, settings) {
  const { order, hidden } = resolveOrder(columns, settings);
  const hiddenSet = new Set(hidden);
  return order.filter((k) => !hiddenSet.has(k));
}

// opts:
//   columns  当前页实际列：[{ key, label, title?, header?, control? }]，顺序即默认列顺序；
//            title 是设置列表里显示的名称（缺省用 label），header 是表头文字（缺省用 label）
//   settings 当前店铺、当前页面的设置 {order, hidden}
//   onChange 顺序或隐藏状态变化后回调整份设置 {order, hidden}
// 返回 { root, footer, state(), reset(), dragging(), destroy() }
function mount({ columns, settings, onChange }) {
  const root = document.createElement("div");
  root.className = "column-settings";
  const list = document.createElement("div");
  list.className = "column-settings-list";
  root.appendChild(list);

  const footer = document.createElement("div");
  footer.className = "column-settings-footer";
  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "btn-outline btn-sm";
  resetBtn.textContent = "恢复默认";
  footer.appendChild(resetBtn);

  const defaults = columns.map((c) => c.key);
  const byKey = new Map(columns.map((c) => [c.key, c]));
  let state = resolveOrder(columns, settings);
  let sortable = null;

  // 按 state 重排列表项：隐藏项留在原位，只切换状态样式与按钮文案
  function render() {
    const hiddenSet = new Set(state.hidden);
    list.innerHTML = "";
    state.order.forEach((key) => {
      const col = byKey.get(key);
      if (!col) return;
      const label = col.title ?? col.label;
      const item = document.createElement("div");
      item.className = "column-item" + (hiddenSet.has(key) ? " is-hidden" : "");
      item.dataset.key = key;

      const name = document.createElement("span");
      name.className = "column-item-name";
      name.textContent = label;
      name.title = label;
      item.appendChild(name);

      if (!col.control) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "btn-outline btn-sm column-item-toggle";
        const hidden = hiddenSet.has(key);
        // 按钮说明下一步做什么：当前可见 → 「隐藏」，当前隐藏 → 「显示」
        toggle.textContent = hidden ? "显示" : "隐藏";
        toggle.setAttribute("aria-pressed", hidden ? "true" : "false");
        toggle.addEventListener("click", () => toggleHidden(key));
        item.appendChild(toggle);
      }

      const handle = document.createElement("span");
      handle.className = "column-item-drag sortable-handle";
      handle.title = "按住拖动调整顺序";
      handle.setAttribute("aria-hidden", "true");
      handle.appendChild(document.createElement("span"));
      item.appendChild(handle);

      list.appendChild(item);
    });
  }

  function toggleHidden(key) {
    const hiddenSet = new Set(state.hidden);
    if (hiddenSet.has(key)) hiddenSet.delete(key);
    else hiddenSet.add(key);
    state = { ...state, hidden: state.order.filter((k) => hiddenSet.has(k)) };
    render();
    notify();
  }

  // 拖动落位后 DOM 已是新顺序：只记顺序并通知，不重建列表，
  // 否则正在播放的落位动画会跟元素一起被替换掉
  function onSortEnd(items) {
    state = { ...state, order: items.map((el) => el.dataset.key) };
    notify();
  }

  function notify() {
    if (onChange) onChange({ order: [...state.order], hidden: [...state.hidden] });
  }

  render();
  sortable = SortableList.create(list, {
    handle: ".column-item-drag",
    item: ".column-item",
    onEnd: onSortEnd,
    // 列可能多于可视高度：不设向下滚动额度，拖到列表底部就能继续滚
    downItems: Infinity,
  });

  resetBtn.addEventListener("click", () => {
    state = { order: [...defaults], hidden: [] };
    render();
    notify();
  });

  return {
    root,
    footer,
    // 当前生效的设置快照（调用方保存时使用）
    state: () => ({ order: [...state.order], hidden: [...state.hidden] }),
    // 正在拖动排序：此时 Esc 由列表组件用于取消拖动，不应关闭侧栏
    dragging: () => !!sortable && sortable.isDragging(),
    destroy() {
      if (sortable) sortable.destroy();
      sortable = null;
    },
  };
}

export const ColumnSettings = { mount, resolveOrder, visibleKeys };