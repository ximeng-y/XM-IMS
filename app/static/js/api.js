/* API 访问层：统一走真实 /api/v1 JSON 端点（验证后端契约本身）
   - 自动附带 X-Shop-Id 头（业务端点约定），当前店铺由 ShopContext 提供
   - 错误统一解析为 {code, message, hint, userMessage, userHint, status} 抛出 ApiFail
   - message/hint 是给 API 调用方（Agent）的文案，可能含端点与参数名；界面一律取
     userMessage/userHint（缺失时回落 message 且不展示 hint），避免接口类文案出现在页面上 */
"use strict";

import { ShopContext } from "xm/shop-context";

class ApiFail extends Error {
  constructor(code, message, hint, status, userMessage, userHint) {
    super(message);
    this.code = code;
    this.hint = hint;
    this.status = status;
    this.userMessage = userMessage;
    this.userHint = userHint;
  }
  // 面向界面的文案：优先后端给出的用户版，其次错误消息本身
  get display() { return this.userMessage || this.message; }
  get displayHint() { return this.userHint || ""; }
}

async function api(method, path, body) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (path.startsWith("/update/")) headers["X-XM-Update"] = "1";
  // 页面设置写请求需要同源标记（新增与删除列设置都不涉及，故所有方法统一带上）
  if (path.startsWith("/view-settings/")) headers["X-XM-View-Settings"] = "1";
  // 店铺端点本身无需 X-Shop-Id；其余请求带上当前店铺
  const shopId = ShopContext.currentId();
  if (shopId !== null && !path.startsWith("/shops")) {
    headers["X-Shop-Id"] = String(shopId);
  }
  let res;
  try {
    res = await fetch("/api/v1" + path, {
      method, headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiFail("NETWORK_ERROR", "网络请求失败: " + String(e), null, 0,
                      "无法连接到本地服务，程序可能已退出", "请重新启动程序后重试");
  }
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
  if (!res.ok) {
    const code = (data && data.code) || ("HTTP_" + res.status);
    throw new ApiFail(
      code,
      (data && data.message) || res.statusText || "请求失败",
      data && data.hint, res.status,
      data && data.user_message, data && data.user_hint);
  }
  return data;
}

const get = (p) => api("GET", p);
const post = (p, b) => api("POST", p, b);
const put = (p, b) => api("PUT", p, b);
const patch = (p, b) => api("PATCH", p, b);
const del = (p) => api("DELETE", p);

export const XM = { api, get, post, put, patch, del, ApiFail };
