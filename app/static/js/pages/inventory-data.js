/* 库存页 SKU 明细：按 SPU 分页加载、缓存与展开状态。
   只拥有缓存、加载队列与代次，不查询 DOM、不渲染页面；汇总行的更新通过 onGroupLoaded 交回页面。 */
"use strict";

const CACHE_TTL = 5 * 60 * 1000;          // 空闲期限：最后一次展开请求后 5 分钟
const CACHE_BUDGET = 32 * 1024 * 1024;    // 已收起缓存的序列化字节预算 32 MiB
const MAX_PARALLEL = 3;                   // 同时加载的 SPU 组数上限
const PAGE_SIZE = 200;                    // /items 单页上限

// fetchPage(spuId, offset)：返回一页 {items, total}；由页面提供，数据模块不直接调接口
export function createStore({ fetchPage, onStateChange = () => {}, onGroupLoaded = () => {} }) {
  // spuId -> { items, bytes, lastRequest }；items 只在分页取完后才写入
  const cache = new Map();
  const pending = new Map();   // 在途加载任务：spuId -> Promise（同一组复用，不重复拉取）
  const errors = new Map();    // 加载失败文案：spuId -> message
  const loading = new Set();   // 正在加载的 SPU id（组内占位用）
  const expanded = new Set();  // 当前展开的 SPU id（表达展开意图；无 SKU 的组不会进入）
  let cacheBytes = 0;          // 已收起的完整缓存字节合计
  let generation = 0;          // 数据代次：写入后自增，旧周期的在途响应据此作废

  // 同一时刻最多 MAX_PARALLEL 组在加载，命中多组时不会瞬间打出一堆请求
  const queue = [];
  let activeLoads = 0;

  function release(spuId) {
    const entry = cache.get(spuId);
    if (!entry) return;
    if (!expanded.has(spuId)) cacheBytes -= entry.bytes;
    cache.delete(spuId);
  }

  function read(spuId) {
    const entry = cache.get(spuId);
    if (!entry) return null;
    // 后台标签页计时器会被节流，读取前仍按时间判断一次
    if (Date.now() - entry.lastRequest >= CACHE_TTL && !expanded.has(spuId)) {
      release(spuId);
      return null;
    }
    return entry.items;
  }

  function touch(spuId) {
    if (read(spuId)) cache.get(spuId).lastRequest = Date.now();
  }

  function put(spuId, items) {
    release(spuId);
    // 只记录大小数值，不另存一份序列化副本
    const bytes = new Blob([JSON.stringify(items)]).size;
    cache.set(spuId, { items, bytes, lastRequest: Date.now() });
    if (!expanded.has(spuId)) { cacheBytes += bytes; enforceBudget(); }
    onGroupLoaded(spuId, items);
  }

  // 先清过期项，再按最后请求时间从旧到新淘汰，直到落到预算内
  function enforceBudget() {
    if (cacheBytes <= CACHE_BUDGET) return;
    const candidates = [...cache].filter(([id]) => !expanded.has(id))
      .sort((a, b) => a[1].lastRequest - b[1].lastRequest);
    for (const [id] of candidates) {
      if (cacheBytes <= CACHE_BUDGET) break;
      release(id);
    }
  }

  // 设置实际到期清理，而不是仅在下次点击时删除
  function sweep() {
    const now = Date.now();
    for (const [id, entry] of [...cache]) {
      if (expanded.has(id)) continue;
      if (now - entry.lastRequest >= CACHE_TTL) release(id);
    }
  }

  // 展开保护：展开中的组不因过期或预算被释放，包括被搜索过滤隐藏的已展开组
  function setExpanded(spuId, on) {
    if (on === expanded.has(spuId)) return;
    const entry = cache.get(spuId);
    if (on) { expanded.add(spuId); if (entry) cacheBytes -= entry.bytes; }
    else { expanded.delete(spuId); if (entry) cacheBytes += entry.bytes; }
  }

  async function loadOne(spuId) {
    const gen = generation;
    loading.add(spuId);
    errors.delete(spuId);
    onStateChange();
    const all = [];
    try {
      let offset = 0;
      for (;;) {
        const data = await fetchPage(spuId, offset);
        if (gen !== generation) return;                 // 数据代次已变，本轮结果作废
        all.push(...data.items);
        offset += data.items.length;
        if (!data.items.length || offset >= data.total) break;
      }
      if (gen !== generation) return;
      put(spuId, all);
    } catch (e) {
      if (gen !== generation) return;
      // 中途失败丢弃本轮部分结果，不写入缓存
      errors.set(spuId, `SKU 加载失败：${e.message}`);
    } finally {
      loading.delete(spuId);
      if (gen === generation) onStateChange();
    }
  }

  function pump() {
    while (activeLoads < MAX_PARALLEL && queue.length) {
      const job = queue.shift();
      activeLoads++;
      loadOne(job.spuId).catch(() => {}).finally(() => {
        activeLoads--;
        job.resolve();
        pump();
      });
    }
  }

  function enqueue(spuId) {
    return new Promise((resolve) => {
      queue.push({ spuId, resolve });
      pump();
    });
  }

  // 展开入口（点击、三角、搜索自动展开、页面跳转）统一走这里，渲染函数自身不发请求
  function ensure(ids, { retry = false } = {}) {
    const waits = [];
    for (const id of ids) {
      if (cache.has(id)) { touch(id); continue; }
      if (pending.has(id)) { waits.push(pending.get(id)); continue; }
      if (errors.has(id) && !retry) continue;
      errors.delete(id);
      let promise;
      promise = enqueue(id).finally(() => { if (pending.get(id) === promise) pending.delete(id); });
      pending.set(id, promise);
      waits.push(promise);
    }
    return Promise.all(waits);
  }

  // SPU 列表刷新后，缓存里已不存在的组、SKU 数对不上的组都不可信；
  // 展开状态同理——SPU 被删掉后若仍留在展开集合里，之后的每次写入都会为它重拉一次空列表
  function reconcile(live) {
    for (const [id, entry] of [...cache]) {
      const count = live.get(id);
      if (count === undefined || count !== entry.items.length) release(id);
    }
    for (const id of [...errors.keys()]) if (!live.has(id)) errors.delete(id);
    for (const id of [...expanded]) if (!live.has(id)) expanded.delete(id);
  }

  // 写入后：作废旧缓存与在途代次，只重载仍展开的组交给调用方决定
  function invalidate() {
    generation++;
    cache.clear();
    pending.clear();
    errors.clear();
    loading.clear();
    cacheBytes = 0;
  }

  return {
    ensure, read, setExpanded, reconcile, invalidate, sweep,
    // 渲染路径直接取缓存，不做空闲期限判断（原实现同样只在读取入口判过期）
    has: (id) => cache.has(id),
    raw: (id) => (cache.get(id) || { items: null }).items,
    expandedIds: () => [...expanded],
    isExpanded: (id) => expanded.has(id),
    errorOf: (id) => errors.get(id) || null,
    isLoading: (id) => loading.has(id),
    clearError: (id) => errors.delete(id),
  };
}
