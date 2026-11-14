/* 页面列设置的接入：读取/保存当前店铺当前页面的偏好，并接到通用侧栏与列设置组件

   - 只管偏好本身与三者的接线：不渲染表格，不做列宽规则，不含页面渲染逻辑
   - 页面启动时 await ready() 取得已保存设置并应用，随后正常渲染
   - 顺序、隐藏状态变化立即应用并自动保存；侧栏宽度在调宽结束后保存
   - 保存不打断界面：失败只提示，界面上的顺序仍以用户刚才的操作为准
*/
"use strict";

import { XM } from "xm/api";
import { UI } from "xm/ui";
import { RightSidebar } from "xm/components/right-sidebar";
import { ColumnSettings } from "xm/components/column-settings";

const EMPTY = () => ({ order: [], hidden: [], sidebar_width: null });

// view -> 保存队列的末端：同一页面的多次保存串行发出，先发后到不会打乱顺序
const chains = new Map();

// 后端返回或本地状态 → 规范设置：未知类型逐项丢弃，不因一条脏数据中断页面
function normalize(data) {
  const raw = data || {};
  const order = Array.isArray(raw.order) ? raw.order.filter((x) => typeof x === "string" && x) : [];
  const hidden = Array.isArray(raw.hidden) ? raw.hidden.filter((x) => typeof x === "string" && x) : [];
  const width = Number.isInteger(raw.sidebar_width) && raw.sidebar_width > 0 ? raw.sidebar_width : null;
  return { order: [...new Set(order)], hidden: [...new Set(hidden)], sidebar_width: width };
}

function save(view, values) {
  const body = normalize(values);
  const prev = chains.get(view) || Promise.resolve();
  const next = prev.catch(() => {}).then(async () => {
    const saved = normalize(await XM.put(`/view-settings/${view}`, body));
    return saved;
  });
  chains.set(view, next);
  return next;
}

// 当前页可能暂时缺某些列（如库存页的自定义属性还没取回）：保存时把上次设置里
// 存在、本次列清单里没有的列补回原位置，隐藏状态也保留，否则一次保存就会写没它们。
// 已知列按面板给出的新顺序与隐藏状态排列
function mergeUnknown(prev, next, known) {
  const order = next.order.slice();
  const inOrder = new Set(order);
  // 从后往前回插：未知列放回它上次顺序里的后继之前，后继已不存在时放到末尾
  for (let i = prev.order.length - 1; i >= 0; i--) {
    const key = prev.order[i];
    if (known.has(key) || inOrder.has(key)) continue;
    const at = order.indexOf(prev.order[i + 1]);
    order.splice(at < 0 ? order.length : at, 0, key);
  }
  const inHidden = new Set(next.hidden);
  return {
    order,
    hidden: [...next.hidden,
      ...prev.hidden.filter((k) => !known.has(k) && !inHidden.has(k))],
  };
}

// opts:
//   view     页面标识（spus / items / inventory）
//   columns  当前页实际列 [{ key, label, control?, css? }]，顺序即默认列顺序；
//            自定义属性随字段定义变化，可传函数返回最新列
//   onApply  设置变化时回调 {order, hidden}，调用方据此重排表头与各类数据行
// 返回 { ready, open, visible, infoVisible, settings }
function create({ view, columns, onApply }) {
  const columnList = () => (typeof columns === "function" ? columns() : columns);
  let current = EMPTY();
  let instance = null;       // 打开中的列设置内容组件
  let pending = null;        // 首次读取的同一个 promise：ready 与 open 共用

  function apply(next) {
    const known = new Set(columnList().map((c) => c.key));
    const merged = mergeUnknown(current, next, known);
    current = { ...current, order: merged.order, hidden: merged.hidden };
    onApply({ order: current.order, hidden: current.hidden });
  }

  function persist() {
    save(view, current).then((saved) => {
      // 只回写本页没有改动中的字段，避免把界面上的新顺序换成旧值
      current = { ...current, sidebar_width: saved.sidebar_width };
    }).catch((e) => UI.apiFail(e, "保存列设置失败"));
  }

  // 首次进入页面：读一次已保存设置并应用；读取失败按默认列顺序继续
  function ready() {
    if (pending) return pending;
    pending = XM.get(`/view-settings/${view}`).then((data) => {
      current = normalize(data);
    }).catch((e) => {
      current = EMPTY();
      UI.apiFail(e, "读取列设置失败");
    }).then(() => {
      apply(current);
      return current;
    });
    return pending;
  }

  async function open() {
    // 已经打开同一个侧栏时不再叠加第二个
    if (RightSidebar.isOpen()) return;
    await ready();
    instance = ColumnSettings.mount({
      columns: columnList(),
      settings: current,
      onChange: (next) => {
        apply(next);
        persist();
      },
    });
    RightSidebar.open({
      title: "列设置",
      width: current.sidebar_width || RightSidebar.DEFAULT_WIDTH,
      content: instance.root,
      footer: instance.footer,
      holdEscape: () => !!instance && instance.dragging(),
      onWidthChange: (width) => {
        current = { ...current, sidebar_width: width };
        persist();
      },
      onClose: () => {
        if (instance) instance.destroy();
        instance = null;
      },
    });
  }

  return {
    ready,
    open,
    // 当前可见列（完整顺序去掉已隐藏的信息列）
    visible: () => ColumnSettings.visibleKeys(columnList(), current),
    // 是否还有信息列可见：全隐藏时页面给出提示与恢复入口
    infoVisible: () => ColumnSettings.visibleKeys(columnList(), current)
      .some((key) => !(columnList().find((c) => c.key === key) || {}).control),
    settings: () => ({ ...current }),
  };
}

export const ViewSettings = { create, normalize };
