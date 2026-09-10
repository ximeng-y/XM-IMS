/* API 访问层：统一走真实 /api/v1 JSON 端点（验证后端契约本身）
   - 自动附带 X-Shop-Id 头（业务端点约定）
   - 错误统一解析为 {code, message, hint, status} 抛出 ApiFail */
"use strict";

const XM = (() => {
  const state = { shopId: null, shops: [] };

  class ApiFail extends Error {
    constructor(code, message, hint, status) {
      super(message);
      this.code = code;
      this.hint = hint;
      this.status = status;
    }
  }

  async function api(method, path, body) {
    const headers = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    // 店铺端点本身无需 X-Shop-Id；其余请求带上当前店铺
    if (state.shopId !== null && !path.startsWith("/shops")) {
      headers["X-Shop-Id"] = String(state.shopId);
    }
    let res;
    try {
      res = await fetch("/api/v1" + path, {
        method, headers,
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      throw new ApiFail("NETWORK_ERROR", "网络请求失败，后端服务可能未启动", String(e), 0);
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* 非 JSON 响应 */ }
    if (!res.ok) {
      throw new ApiFail(
        (data && data.code) || ("HTTP_" + res.status),
        (data && data.message) || res.statusText || "请求失败",
        data && data.hint, res.status);
    }
    return data;
  }

  const get = (p) => api("GET", p);
  const post = (p, b) => api("POST", p, b);
  const patch = (p, b) => api("PATCH", p, b);
  const del = (p) => api("DELETE", p);

  return { state, api, get, post, patch, del, ApiFail };
})();
