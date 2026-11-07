/* 店铺状态所有者：当前店铺、列表顺序与两个 localStorage key 只在这里读写。
   不发 HTTP、不操作 DOM、不刷新页面；顶栏与页面的刷新由调用方明确协调。 */
"use strict";

const SHOP_KEY = "xmims.shopId";
// 排列顺序纯属本机偏好：只存在 localStorage，不写入数据库、配置、备份或同步快照
const ORDER_KEY = "xmims.shopOrder";

let shops = [];        // 已按本地顺序排列的店铺副本
let current = null;    // 当前店铺 id；null 表示没有可用店铺

function readOrder() {
  try {
    const raw = JSON.parse(localStorage.getItem(ORDER_KEY) || "[]");
    return Array.isArray(raw) ? raw.map(Number).filter(Number.isInteger) : [];
  } catch (_) { return []; }
}

function writeOrder(ids) {
  localStorage.setItem(ORDER_KEY, JSON.stringify([...new Set(ids.map(Number))]));
}

// 按本地顺序排列：已记录的店铺按记录顺序在前，未记录的按服务器顺序追加到末尾。
// 已保存顺序中不再存在的店铺自然被丢弃。
function sortShops(list) {
  const order = readOrder();
  const left = new Map(list.map((s) => [s.id, s]));
  const ordered = [];
  order.forEach((id) => {
    const shop = left.get(id);
    if (shop) { ordered.push(shop); left.delete(id); }
  });
  list.forEach((s) => { if (left.has(s.id)) ordered.push(s); });
  return ordered;
}

const copy = () => shops.map((s) => ({ ...s }));

// 用服务器返回的店铺列表替换本地状态：先按本地偏好排序并落盘完整顺序，
// 再沿用当前选择；当前选择不在新列表里时取顺序中的第一间。
// 默认选择只存在内存中，不写本地键——写入由明确的用户动作或调用方决定。
function replaceShops(list) {
  shops = sortShops(list.map((s) => ({ ...s })));
  writeOrder(shops.map((s) => s.id));
  const saved = localStorage.getItem(SHOP_KEY);
  const picked = shops.find((s) => String(s.id) === saved) || shops[0] || null;
  current = picked ? picked.id : null;
  return copy();
}

// 只记住选择，不改变列表：切换店铺与首启页进入店铺都走这里
function rememberSelection(id) {
  current = id === null || id === undefined ? null : Number(id);
  if (current === null) localStorage.removeItem(SHOP_KEY);
  else localStorage.setItem(SHOP_KEY, String(current));
}

// 列表读取失败：清空内存状态但不改动本地记录，下次成功读取时原来的选择与顺序仍可用
function clear() {
  shops = [];
  current = null;
  return [];
}

// 页面已知的店铺列表，仅用于读取；返回副本，调用方不能就地改所有者
const snapshot = () => copy();
const currentId = () => (current === null ? null : current);
const currentShop = () => (current === null ? null : (shops.find((s) => s.id === current) || null));

// 改名：就地替换副本；顶栏刷新由调用方显式请求
function rename(id, name) {
  shops = shops.map((s) => (s.id === Number(id) ? { ...s, name } : s));
  return copy();
}

function reorder(ids) {
  writeOrder(ids);
  const byId = new Map(shops.map((s) => [s.id, s]));
  shops = ids.map((id) => byId.get(Number(id))).filter(Boolean);
  return copy();
}

// 删除后的本地记录清理：删的是当前店铺时切到顺序中的第一间剩余店铺
function remove(id) {
  const target = Number(id);
  writeOrder(readOrder().filter((x) => x !== target));
  shops = shops.filter((s) => s.id !== target);
  if (current === target) current = shops.length ? shops[0].id : null;
  if (current === null) localStorage.removeItem(SHOP_KEY);
  else localStorage.setItem(SHOP_KEY, String(current));
  return copy();
}

export const ShopContext = {
  SHOP_KEY, ORDER_KEY,
  currentId, currentShop, snapshot,
  replaceShops, rememberSelection, clear, rename, reorder, remove,
  readOrder, sortShops,
};
