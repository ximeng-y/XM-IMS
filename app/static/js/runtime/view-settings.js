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

// 页面列可能少于已保存设置里的列：属性元数据晚到、或设置来自列更多的页面版本。
// 这些列在本次设置里无法表达，保存时按上次的位置补回，隐藏状态也保留，
// 否则一次保存就会写没它们。known 必须是产生这次 next 的那份列清单：
// 面板早开时属性还没取回，面板里根本没有这些列，用页面最新列清单判断就会误当已知列。
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

// 可选分组：各组共用同一份 order / hidden，但分别维护自己的列清单和恢复默认入口。
// 组定义可以是函数，页面数据取回后新增的列下次打开设置时会自然出现。
function resolveGroups(groups, fallbackColumns) {
  const raw = typeof groups === "function" ? groups() : groups;
  if (!Array.isArray(raw) || !raw.length) return null;
  return raw.map((group, index) => ({
    key: String(group.key ?? index),
    label: group.label || `列组 ${index + 1}`,
    columns: group.columns || fallbackColumns,
    inherit: group.inherit || null,
  }));
}

function columnsOf(group) {
  return typeof group.columns === "function" ? group.columns() : group.columns;
}

// 旧页面版本使用平面列键。新分组首次读取时，让新键沿用它对应的旧键顺序与隐藏状态；
// 旧键继续保留，避免用户在页面版本回退时丢失原设置。
function inheritKeys(values, groups) {
  if (!groups) return values;
  const order = [...values.order];
  const hidden = [...values.hidden];
  for (const group of groups) {
    for (const [nextKey, oldKey] of Object.entries(group.inherit || {})) {
      if (order.includes(nextKey) || hidden.includes(nextKey)) continue;
      const at = order.indexOf(oldKey);
      if (at >= 0) order.splice(at + 1, 0, nextKey);
      if (hidden.includes(oldKey) && !hidden.includes(nextKey)) hidden.push(nextKey);
    }
  }
  return { ...values, order: [...new Set(order)], hidden: [...new Set(hidden)] };
}

// opts:
//   view     页面标识（spus / items / inventory）
//   columns  不分组的当前页实际列 [{ key, label, control?, css? }]，顺序即默认列顺序；
//            自定义属性随字段定义变化，可传函数返回最新列
//   groups   可选分组函数/数组：[{ key, label, columns, inherit? }]；传了 groups 时忽略 columns
//   onApply  设置变化时回调 {order, hidden}，调用方据此重排表头与各类数据行
// 返回 { ready, open, visible, infoVisible, settings }
function create({ view, columns, groups, onApply }) {
  const columnList = () => {
    const list = typeof columns === "function" ? columns() : columns;
    return Array.isArray(list) ? list : [];
  };
  const groupList = () => resolveGroups(groups, columnList);
  let current = EMPTY();
  let instances = [];        // 打开中的列设置内容组件；分组时每组一个
  let pending = null;        // 首次读取的同一个 promise：ready 与 open 共用

  // knownColumns 必须是产生 next 的那份列清单，而不是页面此刻的列清单：
  // 面板早于属性元数据打开时，面板里没有属性列，用页面最新列清单判断会把它们
  // 当成已知列，从而跳过补齐、随后把缺列的设置写回本机
  function apply(next, knownColumns) {
    const known = new Set(knownColumns.map((c) => c.key));
    const merged = mergeUnknown(current, next, known);
    current = { ...current, order: merged.order, hidden: merged.hidden };
    if (onApply) onApply({ order: current.order, hidden: current.hidden });
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
      current = inheritKeys(normalize(data), groupList());
    }).catch((e) => {
      current = EMPTY();
      UI.apiFail(e, "读取列设置失败");
    }).then(() => {
      const activeGroups = groupList();
      const knownColumns = activeGroups
        ? activeGroups.flatMap((group) => columnsOf(group))
        : columnList();
      apply(current, knownColumns);
      return current;
    });
    return pending;
  }

  async function open() {
    // 已经打开同一个侧栏时不再叠加第二个
    if (RightSidebar.isOpen()) return;
    await ready();
    const activeGroups = groupList();
    const root = document.createElement("div");
    root.className = "column-settings";
    instances = [];

    const mountGroup = (panelColumns, label) => {
      const groupRoot = document.createElement("section");
      groupRoot.className = "column-settings-group";
      if (label) {
        const heading = document.createElement("h3");
        heading.className = "column-settings-group-title";
        heading.textContent = label;
        groupRoot.appendChild(heading);
      }
      const instance = ColumnSettings.mount({
        columns: panelColumns,
        settings: current,
        onChange: (next) => {
          apply(next, panelColumns);
          persist();
        },
      });
      groupRoot.append(instance.root, instance.footer);
      root.appendChild(groupRoot);
      instances.push(instance);
    };

    if (activeGroups) {
      for (const group of activeGroups) mountGroup(columnsOf(group), group.label);
    } else {
      // 面板列清单在这里固定一次：之后页面列再怎么变（如属性元数据到达），
      // 面板仍只列出挂载时的列，保存时也按这份清单判定哪些列缺失需要补齐
      mountGroup(columnList(), "");
    }
    RightSidebar.open({
      title: "列设置",
      width: current.sidebar_width || RightSidebar.DEFAULT_WIDTH,
      content: root,
      holdEscape: () => instances.some((instance) => instance.dragging()),
      onWidthChange: (width) => {
        current = { ...current, sidebar_width: width };
        persist();
      },
      onClose: () => {
        instances.forEach((instance) => instance.destroy());
        instances = [];
      },
    });
  }

  function visible(groupKey) {
    const activeGroups = groupList();
    if (!activeGroups) return ColumnSettings.visibleKeys(columnList(), current);
    const group = activeGroups.find((item) => item.key === groupKey);
    return group ? ColumnSettings.visibleKeys(columnsOf(group), current) : [];
  }

  function infoVisible(groupKey) {
    const activeGroups = groupList();
    if (!activeGroups) {
      const cols = columnList();
      return ColumnSettings.visibleKeys(cols, current)
        .some((key) => !(cols.find((c) => c.key === key) || {}).control);
    }
    const group = activeGroups.find((item) => item.key === groupKey);
    if (!group) return false;
    const cols = columnsOf(group);
    return ColumnSettings.visibleKeys(cols, current)
      .some((key) => !(cols.find((c) => c.key === key) || {}).control);
  }

  return {
    ready,
    open,
    // 当前可见列（完整顺序去掉已隐藏的信息列）；分组时传组 key
    visible,
    // 是否还有信息列可见：全隐藏时页面给出提示与恢复入口
    infoVisible,
    settings: () => ({ ...current }),
  };
}

export const ViewSettings = { create, normalize };
