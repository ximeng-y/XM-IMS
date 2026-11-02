/* 库存页：SPU 分组表格（带附属 SKU 的 SPU 行可展开）+ 顶部操作区（新建/出入库/盘点小窗 + 搜索）
   操作区各按钮复用 components/ 下的共享表单组件，避免与单品页、原库存操作页各写一份。
   SKU 明细按 SPU 按需加载并缓存（issue #49）；列表容器限高内滚（issue #54） */
"use strict";

// ---------- 页面状态 ----------
let overviewRows = [];          // [{ spu, total }]：SPU 汇总行（SKU 明细不在此长期持有）
let fieldsMeta = [];            // 自定义字段定义（新建单品小窗用）
let spusList = [];              // SPU 列表（与新建单品小窗内的组合框共享引用）
const expandedSpus = new Set(); // 当前展开的 SPU id（表达展开意图；无 SKU 的组不会进入）

// SKU 完整明细缓存：spuId -> { items, bytes, lastRequest }
//   items 只在分页取完后才写入；加载中与失败状态分别由 pendingSpus / loadErrors 表达
const skuCache = new Map();
const pendingSpus = new Map();  // 在途加载任务：spuId -> Promise（同一组复用，不重复拉取）
const loadErrors = new Map();   // 加载失败文案：spuId -> message
const loadingSpus = new Set();  // 正在加载的 SPU id（组内占位用）
const CACHE_TTL = 5 * 60 * 1000;          // 空闲期限：最后一次展开请求后 5 分钟
const CACHE_BUDGET = 32 * 1024 * 1024;    // 已收起缓存的序列化字节预算 32 MiB
const MAX_PARALLEL = 3;                   // 同时加载的 SPU 组数上限
const PAGE_SIZE = 200;                    // /items 单页上限
let cacheBytes = 0;                       // 已收起的完整缓存字节合计

let storeGen = 0;               // 数据代次：写入后自增，旧周期的在途响应据此作废
let searchToken = 0;            // 搜索序号：任何新输入都使在途搜索失效
let searchTimer = null;         // 搜索防抖定时器
let skuHits = new Map();        // 搜索命中的 SPU -> 命中 SKU 编号集合（仅识别信息）
let searchError = null;         // 搜索失败文案（与「没有匹配结果」区分开）
let lastAutoExpandQuery = null; // 上次自动展开过的搜索词（同一词只自动展开一次）

// ---------- 概览数据 ----------
async function loadOverview() {
  const tbody = $("#spuRows");
  try {
    // 首次只取 SPU 汇总与字段定义：SKU 明细按 SPU 展开时再分页加载（issue #49）
    const [spus, fields] = await Promise.all([XM.get("/spus"), XM.get("/fields")]);
    spusList = spus.spus;
    fieldsMeta = fields.fields.filter((f) => !f.system);
    // 合计库存与 SKU 数直接取 SPU 列表的汇总字段，不再拉取全店 SKU
    overviewRows = spusList.map((s) => ({ spu: s, total: s.total_qty }));
    reconcileCaches();
    renderOverview();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
    UI.apiFail(e);
  }
}

// SPU 列表刷新后，缓存里已不存在的组、SKU 数对不上的组都不可信；
// 展开状态同理——SPU 被删掉后若仍留在展开集合里，之后的每次写入都会为它重拉一次空列表
function reconcileCaches() {
  const live = new Map(spusList.map((s) => [s.id, s.sku_count]));
  for (const [id, e] of [...skuCache]) {
    const n = live.get(id);
    if (n === undefined || n !== e.items.length) releaseCache(id);
  }
  for (const id of [...loadErrors.keys()]) if (!live.has(id)) loadErrors.delete(id);
  for (const id of [...expandedSpus]) if (!live.has(id)) expandedSpus.delete(id);
}

// 包含式匹配：SPU 名或任一 SKU 名命中即保留该 SPU 组
//   hit: "spu"（组名命中，整组 SKU 都显示）| "sku"（仅 SKU 名命中，只显示命中的 SKU）
// 合计库存 / SKU 数始终按整组计，不随筛选变化
function filterRows() {
  const q = $("#fltQ").value.trim().toLowerCase();
  if (!q) return overviewRows.map((r) => ({ spu: r.spu, total: r.total, shown: null, hit: null }));
  const out = [];
  for (const r of overviewRows) {
    if (r.spu.name.toLowerCase().includes(q)) {
      out.push({ spu: r.spu, total: r.total, shown: readCache(r.spu.id), hit: "spu" });
      continue;
    }
    const ids = skuHits.get(r.spu.id);
    if (ids) {
      const items = readCache(r.spu.id);
      out.push({ spu: r.spu, total: r.total,
                 shown: items ? items.filter((it) => ids.has(it.id)) : null, hit: "sku" });
    }
  }
  return out;
}

function renderOverview() {
  const tbody = $("#spuRows");
  const rows = filterRows();
  const query = $("#fltQ").value.trim();
  const colCount = 6 + fieldsMeta.length;
  // 搜索、展开、数据刷新都会重排内容，旧的定位留白不再适用
  clearTail();
  // 表头随本店自定义字段一起重建；列顺序 = 展开 / ID / 名称 / SKU 数 / 合计库存 / 自定义字段… / 更新时间
  // 取值口径与单品页一致：计算字段取后端计算结果，普通字段取手填值
  $("#spuHead").innerHTML =
    `<tr><th></th><th class="num">ID</th><th>名称</th>` +
    `<th class="num">SKU 数</th><th class="num">合计库存</th>` +
    fieldsMeta.map((f) => `<th>${UI.esc(f.name)}</th>`).join("") +
    `<th>更新时间</th></tr>`;

  if (!rows.length) {
    const msg = searchError ? `搜索失败：${UI.esc(searchError)}`
      : query ? "没有匹配的 SPU / SKU"
      : '从第一个单品开始记录库存。<br><a href="/inventory?op=new-item">添加第一个单品</a><br>' +
        '<a href="/fields">设置成本、售价等字段（可稍后）</a>';
    tbody.innerHTML = `<tr><td colspan="${colCount}" class="t-empty">${msg}</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(({ spu, total, shown }) => {
    const open = expandedSpus.has(spu.id);
    // 只有存在附属 SKU 的 SPU 行才渲染展开三角
    const tri = spu.sku_count > 0
      ? `<button type="button" class="tri-btn${open ? " open" : ""}" data-spu="${spu.id}" aria-label="展开/收起 SKU">▼</button>`
      : "";
    // data-spu 供右键菜单识别目标 SPU（菜单里可「在此 SPU 下新建单品」）
    let row = `<tr class="spu-row" data-spu="${spu.id}">
      <td class="tri-cell">${tri}</td>
      <td class="num mono">${spu.id}</td>
      <td title="${UI.esc(spu.name)}">${UI.esc(spu.name)}</td>
      <td class="num">${spu.sku_count}</td>
      <td class="num">${spu.sku_count > 0 ? total : "—"}</td>
      ${fieldsMeta.map(() => `<td class="muted">—</td>`).join("")}
      <td class="muted">${UI.fmtTime(spu.updated_at)}</td>
    </tr>`;
    if (open) row += skuGroupHtml(spu, shown, colCount);
    return row;
  }).join("");
}

// 展开组内的行：未加载完 / 加载失败都不能伪装成「没有 SKU」
function skuGroupHtml(spu, shown, colCount) {
  const emptyRow = (html) =>
    `<tr class="sku-sub-row"><td></td><td colspan="${colCount - 1}" class="t-empty">${html}</td></tr>`;
  if (loadErrors.has(spu.id)) {
    return emptyRow(`${UI.esc(loadErrors.get(spu.id))} ` +
      `<button type="button" class="btn-outline btn-sm" data-retry-spu="${spu.id}">重试</button>`);
  }
  const cache = skuCache.get(spu.id);
  if (!cache) return emptyRow(loadingSpus.has(spu.id) ? "加载中…" : "正在准备数据…");
  const items = shown ?? cache.items;
  if (!items.length) {
    return emptyRow($("#fltQ").value.trim() ? "该 SPU 下没有匹配的 SKU" : "该 SPU 下还没有 SKU");
  }
  // 普通字段与计算字段都挂 title：固定列宽下长值与长金额会被省略号截断，悬停需能看全文。
  // 出错时的徽标自带 title（写明原因），悬停徽标显示的是它，不受这里的 title 影响。
  const extraCells = (it) => fieldsMeta.map((f) => {
    if (ItemForm.isComputed(f)) {
      const calc = it.calculations?.[f.name];
      const full = calc && !calc.error ? calc.display : "";
      return `<td${full ? ` title="${UI.esc(full)}"` : ""}>${ItemForm.calcHtml(calc)}</td>`;
    }
    const v = it.values[f.name];
    const s = v === null || v === undefined || v === "" ? "" : String(v);
    return `<td${s ? ` title="${UI.esc(s)}"` : ""}>${UI.fmtVal(v)}</td>`;
  }).join("");
  // data-item / data-item-name / data-spu 供右键菜单与后续操作识别目标 SKU
  return items.map((it) => `<tr class="sku-sub-row" data-item="${UI.esc(it.id)}" data-item-name="${UI.esc(it.name)}" data-spu="${spu.id}">
    <td></td>
    <td class="num mono">${UI.esc(it.id)}</td>
    <td title="${UI.esc(it.name)}">${UI.esc(it.name)}</td>
    <td class="muted">—</td>
    <td class="num">${it.qty}</td>
    ${extraCells(it)}
    <td class="muted">${UI.fmtTime(it.updated_at)}</td>
  </tr>`).join("");
}

// ---------- SKU 按需加载与缓存 ----------
// 展开入口（点击、三角、搜索自动展开、页面跳转）统一走这里，渲染函数自身不发请求
function ensureSkus(ids, { retry = false } = {}) {
  const waits = [];
  for (const id of ids) {
    if (skuCache.has(id)) { touchCache(id); continue; }
    if (pendingSpus.has(id)) { waits.push(pendingSpus.get(id)); continue; }
    if (loadErrors.has(id) && !retry) continue;
    loadErrors.delete(id);
    let p;
    p = queueLoad(id).finally(() => { if (pendingSpus.get(id) === p) pendingSpus.delete(id); });
    pendingSpus.set(id, p);
    waits.push(p);
  }
  return Promise.all(waits);
}

// 同一时刻最多 MAX_PARALLEL 组在加载，命中多组时不会瞬间打出一堆请求
const loadQueue = [];
let activeLoads = 0;

function queueLoad(spuId) {
  return new Promise((resolve) => {
    loadQueue.push({ spuId, resolve });
    pumpQueue();
  });
}

function pumpQueue() {
  while (activeLoads < MAX_PARALLEL && loadQueue.length) {
    const job = loadQueue.shift();
    activeLoads++;
    loadOneSpu(job.spuId).catch(() => {}).finally(() => {
      activeLoads--;
      job.resolve();
      pumpQueue();
    });
  }
}

async function loadOneSpu(spuId) {
  const gen = storeGen;
  loadingSpus.add(spuId);
  loadErrors.delete(spuId);
  renderOverview();
  const all = [];
  try {
    let offset = 0;
    for (;;) {
      const data = await XM.get(`/items?spu_id=${spuId}&offset=${offset}&limit=${PAGE_SIZE}`);
      if (gen !== storeGen) return;                 // 数据代次已变，本轮结果作废
      all.push(...data.items);
      offset += data.items.length;
      if (!data.items.length || offset >= data.total) break;
    }
    if (gen !== storeGen) return;
    putCache(spuId, all);
  } catch (e) {
    if (gen !== storeGen) return;
    // 中途失败丢弃本轮部分结果，不写入缓存
    loadErrors.set(spuId, `SKU 加载失败：${e.message}`);
  } finally {
    loadingSpus.delete(spuId);
    if (gen === storeGen) renderOverview();
  }
}

function putCache(spuId, items) {
  releaseCache(spuId);
  // 只记录大小数值，不另存一份序列化副本
  const bytes = new Blob([JSON.stringify(items)]).size;
  skuCache.set(spuId, { items, bytes, lastRequest: Date.now() });
  if (!expandedSpus.has(spuId)) { cacheBytes += bytes; enforceBudget(); }
  syncRowSummary(spuId, items);
}

// 完整明细到位后校正组头：本地数量与合计不再显示旧值
function syncRowSummary(spuId, items) {
  const row = overviewRows.find((r) => r.spu.id === spuId);
  if (!row) return;
  const total = items.reduce((acc, it) => acc + it.qty, 0);
  if (row.spu.sku_count === items.length && row.spu.total_qty === total) return;
  row.spu.sku_count = items.length;
  row.spu.total_qty = total;
  row.total = total;
}

function readCache(spuId) {
  const e = skuCache.get(spuId);
  if (!e) return null;
  // 后台标签页计时器会被节流，读取前仍按时间判断一次
  if (Date.now() - e.lastRequest >= CACHE_TTL && !expandedSpus.has(spuId)) {
    releaseCache(spuId);
    return null;
  }
  return e.items;
}

function touchCache(spuId) {
  if (readCache(spuId)) skuCache.get(spuId).lastRequest = Date.now();
}

function releaseCache(spuId) {
  const e = skuCache.get(spuId);
  if (!e) return;
  if (!expandedSpus.has(spuId)) cacheBytes -= e.bytes;
  skuCache.delete(spuId);
}

// 展开保护：展开中的组不因过期或预算被释放，包括被搜索过滤隐藏的已展开组
function setExpanded(spuId, on) {
  if (on === expandedSpus.has(spuId)) return;
  const e = skuCache.get(spuId);
  if (on) { expandedSpus.add(spuId); if (e) cacheBytes -= e.bytes; }
  else { expandedSpus.delete(spuId); if (e) cacheBytes += e.bytes; }
}

// 先清过期项，再按最后请求时间从旧到新淘汰，直到落到预算内
function enforceBudget() {
  if (cacheBytes <= CACHE_BUDGET) return;
  const cands = [...skuCache].filter(([id]) => !expandedSpus.has(id))
    .sort((a, b) => a[1].lastRequest - b[1].lastRequest);
  for (const [id] of cands) {
    if (cacheBytes <= CACHE_BUDGET) break;
    releaseCache(id);
  }
}

// 设置实际到期清理，而不是仅在下次点击时删除
function sweepExpired() {
  const now = Date.now();
  for (const [id, e] of [...skuCache]) {
    if (expandedSpus.has(id)) continue;
    if (now - e.lastRequest >= CACHE_TTL) releaseCache(id);
  }
}

function toggleSpu(id) {
  const opening = !expandedSpus.has(id);
  setExpanded(id, opening);
  renderOverview();
  if (opening) ensureSkus([id]);
}

function retrySpu(id) {
  loadErrors.delete(id);
  renderOverview();
  ensureSkus([id], { retry: true });
}

// 写入后：作废旧缓存与在途代次，刷新汇总与字段，只重载仍展开的组
async function refreshAfterWrite() {
  invalidateSkuData();
  await loadOverview();
  await runSearch($("#fltQ").value.trim());
  await ensureSkus([...expandedSpus]);
}

function invalidateSkuData() {
  storeGen++;                    // 在途响应据此自动作废
  skuCache.clear();
  pendingSpus.clear();
  loadErrors.clear();
  loadingSpus.clear();
  cacheBytes = 0;
}

// ---------- 全店搜索 ----------
// SPU 名本地匹配；SKU 名走 /items?q= 分页查全店，不能只搜缓存
function onSearchInput() {
  searchToken++;                 // 任何新输入（含清空）都立即作废在途搜索
  $("#btnClearQ").hidden = $("#fltQ").value === "";
  const qv = $("#fltQ").value.trim();
  if (!qv) {
    skuHits = new Map();
    searchError = null;
    lastAutoExpandQuery = null;
    renderOverview();
    return;
  }
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(qv), 250);
}

async function runSearch(qv) {
  const token = searchToken;
  if (!qv) { skuHits = new Map(); searchError = null; renderOverview(); return; }
  try {
    const hits = await fetchSkuHits(qv, token);
    if (hits === null) return;                   // 已有更新的搜索，丢弃本次结果
    skuHits = hits;
    searchError = null;
    const ids = [...hits.keys()];
    // 同一搜索词只自动展开一次，之后尊重用户的手动收起
    if (qv !== lastAutoExpandQuery) ids.forEach((id) => setExpanded(id, true));
    lastAutoExpandQuery = qv;
    renderOverview();
    // 命中组要展示匹配 SKU，就得有整组完整数据；只保障仍展开的组
    await ensureSkus(ids.filter((id) => expandedSpus.has(id)));
  } catch (e) {
    if (token !== searchToken) return;
    skuHits = new Map();
    searchError = e.message;
    renderOverview();
    UI.apiFail(e, "搜索失败");
  }
}

async function fetchSkuHits(qv, token) {
  const hits = new Map();
  let offset = 0;
  for (;;) {
    const data = await XM.get(`/items?q=${encodeURIComponent(qv)}&offset=${offset}&limit=${PAGE_SIZE}`);
    if (token !== searchToken) return null;
    for (const it of data.items) {
      if (!hits.has(it.spu_id)) hits.set(it.spu_id, new Set());
      hits.get(it.spu_id).add(it.id);
    }
    offset += data.items.length;
    if (!data.items.length || offset >= data.total) break;
  }
  return hits;
}

// ---------- 操作区：各按钮 → 对应小窗 ----------
const afterWrite = () => refreshAfterWrite();

const OP_ACTIONS = {
  "new-item": () => ItemForm.openCreateItem({
    fields: fieldsMeta, spus: spusList, onCreated: afterWrite,
    // issue #33：重名提示里选「编辑这个当前已有的 SKU」时打开同一个编辑小窗
    onEditExisting: (item) => ItemForm.openEditItem({
      item, fields: fieldsMeta, spus: spusList, onSaved: afterWrite,
    }),
  }),
  "new-spu": async () => {
    if (await ItemForm.openCreateSpu()) afterWrite();
  },
  in: () => OpsForm.open("in", { onDone: afterWrite }),
  out: () => OpsForm.open("out", { onDone: afterWrite }),
  adjust: () => OpsForm.open("adjust", { onDone: afterWrite }),
};

function bindOpsBar() {
  $$(".ops-bar button[data-op]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const act = OP_ACTIONS[btn.dataset.op];
      if (act) act();
    });
  });

  const q = $("#fltQ"), clearBtn = $("#btnClearQ");
  q.addEventListener("input", onSearchInput);
  q.addEventListener("keydown", (e) => { if (e.key === "Escape") { q.value = ""; onSearchInput(); } });
  clearBtn.addEventListener("click", () => { q.value = ""; onSearchInput(); q.focus(); });
}

// ---------- 右键菜单：SPU 行与 SKU 子行 ----------
// 菜单项全部复用操作区已有的小窗实现，不新增后端接口
function openCreateItemForSpu(spuId) {
  ItemForm.openCreateItem({
    fields: fieldsMeta, spus: spusList, initialSpuId: spuId,
    onCreated: afterWrite,
    onEditExisting: (item) => ItemForm.openEditItem({
      item, fields: fieldsMeta, spus: spusList, onSaved: afterWrite,
    }),
  });
}

// 删除前取目标单品。SKU 明细按需加载，缓存只覆盖已展开的组，不能拿本地数据查找，
// 改为按行上的标识向单品接口取一次；取不到说明该单品已不在，按删除失败的既有文案处理。
async function loadItemForDelete(itemRef) {
  try {
    return await XM.get(`/items/${encodeURIComponent(itemRef)}`);
  } catch (e) {
    console.info("[inventory] 取删除目标失败", itemRef, e);
    UI.fail("该单品可能已被删除，请刷新页面后重试");
    return null;
  }
}

// 删除单品：与单品页同一口径，仅从未产生过流水的 SKU 可删
function deleteItemFlow(item) {
  return DeleteFlow.run({
    title: `删除单品 ${item.id}`,
    bodyHtml: `<p>将删除单品 <b>${UI.esc(item.spu_name)}-${UI.esc(item.name)}</b>` +
              `（当前库存 ${item.qty}）及其全部自定义字段值，<b style="color:var(--danger)">操作不可恢复</b>。</p>` +
              `<p class="hint">已有库存流水的单品不能删除：流水是不可变账本，` +
              `录错请用反向操作或盘点调整修正。</p>`,
    path: `/items/${encodeURIComponent(item.id)}?confirm=true`,
    okText: (res) => `单品 ${res.item}「${res.name}」已删除` +
                     (res.deleted_values ? `，连带清除 ${res.deleted_values} 个字段值` : ""),
    onDone: afterWrite,
  });
}

// 删除 SPU：其下还有单品时后端会拒绝，这里先按已知的 sku_count 给出提示
function deleteSpuFlow(spu) {
  return DeleteFlow.run({
    precheck: () => {
      if (spu.sku_count === 0) return true;
      UI.fail(`SPU「${spu.name}」下还有 ${spu.sku_count} 个单品，不能删除`,
              { title: "无法删除", hint: "请先删除该 SPU 下的全部单品（仅从未产生过流水的单品可删除）" });
      return false;
    },
    title: `删除 SPU「${spu.name}」`,
    bodyHtml: `<p>该 SPU 下没有任何单品，删除后<b style="color:var(--danger)">不可恢复</b>。</p>`,
    path: `/spus/${spu.id}?confirm=true`,
    okText: (res) => `SPU「${res.name}」已删除`,
    onDone: afterWrite,
  });
}

function bindContextMenu() {
  $("#spuRows").addEventListener("contextmenu", (e) => {
    const sku = e.target.closest("tr.sku-sub-row[data-item]");
    if (sku) {
      ContextMenu.open(e, [
        { label: "入库", onClick: () => OpsForm.open("in", { itemRef: sku.dataset.item, onDone: afterWrite }) },
        { label: "出库", onClick: () => OpsForm.open("out", { itemRef: sku.dataset.item, onDone: afterWrite }) },
        { label: "盘点调整", onClick: () => OpsForm.open("adjust", { itemRef: sku.dataset.item, onDone: afterWrite }) },
        { label: "新建同 SPU 单品",
          onClick: () => openCreateItemForSpu(Number(sku.dataset.spu)) },
        { label: "删除", danger: true,
          onClick: async () => {
            const it = await loadItemForDelete(sku.dataset.item);
            if (it) void deleteItemFlow(it);
          } },
      ]);
      return;
    }
    const spu = e.target.closest("tr.spu-row[data-spu]");
    if (spu) {
      ContextMenu.open(e, [
        { label: "在此 SPU 下新建单品",
          onClick: () => openCreateItemForSpu(Number(spu.dataset.spu)) },
        { label: "删除", danger: true,
          onClick: () => {
            const s = spusList.find((x) => x.id === Number(spu.dataset.spu));
            if (s) void deleteSpuFlow(s);
            else UI.fail("该 SPU 可能已被删除，请刷新页面后重试");
          } },
      ]);
    }
  });
}

// ---------- 限高滚动容器（issue #54） ----------
const MIN_SCROLL_H = 160;       // 10rem：窗口极矮时保底，仍不足则由整页滚动兜底

// 元素在文档中的布局位置：offsetTop 链不受 fade-in 的 translateY 影响
function docTop(el) {
  let top = 0;
  for (let node = el; node; node = node.offsetParent) top += node.offsetTop || 0;
  return top;
}

// 容器最大可视高度 = 视口高度 − 容器上沿文档位置 − 外层面板下内边距与下边框 − 页面下方既有留白
//   − 表格下方横向滑动提示的占位（窄屏显示时才有，隐藏时为 0）
// 提示节点由 ui.js 插在滚动容器之后，高度只在窄屏且表格横向溢出时非零
function hintHeight() {
  const scroller = $("#inventoryScroll");
  const hint = scroller && scroller.nextElementSibling;
  if (!hint || !hint.classList.contains("table-scroll-hint")) return 0;
  const h = hint.getBoundingClientRect().height;   // display:none 时为 0
  if (!h) return 0;
  const cs = getComputedStyle(hint);
  return h + (parseFloat(cs.marginTop) || 0) + (parseFloat(cs.marginBottom) || 0);
}

function computeScrollMax() {
  const scroller = $("#inventoryScroll"), panel = $("#inventoryPanel");
  if (!scroller || !panel) return 0;
  const pcs = getComputedStyle(panel);
  const panelBottom = (parseFloat(pcs.paddingBottom) || 0) + (parseFloat(pcs.borderBottomWidth) || 0);
  const main = $("main.content");
  const bottomGap = (main ? (parseFloat(getComputedStyle(main).paddingBottom) || 0) : 0) +
                    (parseFloat(getComputedStyle(document.body).marginBottom) || 0);
  const avail = window.innerHeight - docTop(scroller) - panelBottom - bottomGap - hintHeight();
  return Math.max(MIN_SCROLL_H, Math.floor(avail));
}

function applyScrollMax() {
  const scroller = $("#inventoryScroll");
  // 用 max-height 而非固定高度：数据少时自然收缩，不撑出空白
  if (scroller) scroller.style.maxHeight = computeScrollMax() + "px";
}

// 观察上方各块的实际占位变化（操作区换行、侧栏与标题高度变化）
// 以及表格下方提示的出现/消失（窄屏断点切换会改变其占位）
function installScrollSizing() {
  applyScrollMax();
  const ro = new ResizeObserver(() => applyScrollMax());
  $$(".topbar, .sidenav, .page-head, .ops-bar").forEach((el) => ro.observe(el));
  const scroll = $("#inventoryScroll");
  const hint = scroll && scroll.nextElementSibling;
  if (hint && hint.classList.contains("table-scroll-hint")) ro.observe(hint);
  window.addEventListener("resize", () => { clearTail(); applyScrollMax(); });
}

// ---------- 一次性底部留白与定位（issue #49） ----------
let tailInitial = 0;     // 本次定位补足的留白总量（上限，只减不增）
let tailAmount = 0;      // 当前留白高度
let tailAnchor = 0;      // 定位完成时的滚动位置，用于换算已退出的尾部空间
let lastScrollTop = 0;   // 上一次滚动位置，用于判断滚动方向

function setTail(px) {
  const t = $("#inventoryTail");
  if (!t) return;
  tailInitial = Math.max(0, Math.round(px));
  tailAmount = tailInitial;
  t.hidden = tailAmount === 0;
  t.style.height = tailAmount + "px";
}

// 约定：底部留白只在跳转定位（centerSpuRow）时补，其它路径一律清除。
// 每次重渲染都会调用本函数，因此「补留白 → 重渲染」不会互相打架；
// 搜索自动展开期间的重渲染会顺带清掉跳转留下的留白，这是预期行为，不会重新居中。
// 搜索变化、展开状态变化、数据刷新、窗口尺寸变化都会清掉旧留白，且不重新居中
function clearTail() {
  const t = $("#inventoryTail");
  if (!t) return;
  tailInitial = 0;
  tailAmount = 0;
  t.hidden = true;
  t.style.height = "0px";
}

function installTailWatch() {
  const scroller = $("#inventoryScroll");
  lastScrollTop = scroller.scrollTop;
  scroller.addEventListener("scroll", () => {
    const st = scroller.scrollTop;
    const up = st < lastScrollTop;
    lastScrollTop = st;
    // 只回收：留白总量减去累计上滚距离，且不得超过当前高度（向下滚不会把留白要回来）
    if (!tailInitial || !up) return;
    const next = Math.min(tailAmount, Math.max(0, tailInitial - (tailAnchor - st)));
    if (next === tailAmount) return;
    tailAmount = next;
    $("#inventoryTail").style.height = next + "px";
    if (next === 0) $("#inventoryTail").hidden = true;
  });
}

// 任意库存行（SPU 行或 SKU 子行）位于最大可视区域上半区时不滚动；下半区则即时定位到中线
// 返回目标行是否存在并完成了定位判定（已在半区上半时也算完成，只是不滚动）
function centerRow(row) {
  const scroller = $("#inventoryScroll");
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

function centerSpuRow(spuId) {
  const scroller = $("#inventoryScroll");
  const row = scroller && scroller.querySelector(`tr.spu-row[data-spu="${spuId}"]`);
  return row ? centerRow(row) : false;
}

// SKU 子行按公开编号（形如 n-m）精确比对行上的 data-item，不做选择器拼接也不按名称模糊匹配；
// 找不到即返回 false，由调用方提示，绝不退而定位到别的行
function centerSkuRow(itemId) {
  const scroller = $("#inventoryScroll");
  if (!scroller || !itemId) return false;
  const row = [...scroller.querySelectorAll("tr.sku-sub-row[data-item]")]
    .find((tr) => tr.dataset.item === itemId);
  return row ? centerRow(row) : false;
}

// ---------- 页面跳转定位（?spu_id= / ?item_id=） ----------
// 消费掉该参数，保留其他参数与 hash，避免随后切店刷新时错误复用目标
function consumeSpuJumpParam() {
  const url = new URL(location.href);
  const raw = url.searchParams.get("spu_id");
  if (raw === null) return null;
  url.searchParams.delete("spu_id");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
  return /^\d+$/.test(raw) && Number(raw) > 0 ? Number(raw) : null;
}

async function initSpuJump(spuId) {
  const spu = spusList.find((s) => s.id === spuId);
  if (!spu) { UI.fail(`SPU #${spuId} 不存在，可能已被删除`); return; }
  if (spu.sku_count > 0) {
    setExpanded(spuId, true);
    renderOverview();
    await ensureSkus([spuId]);
    if (loadErrors.has(spuId)) return;            // 失败就留在该组的错误状态，不伪装成完成
  }
  centerSpuRow(spuId);
}

// 单品页双击 SKU 行跳来：参数同样一次性消费，保留其他参数与 hash
function consumeItemJumpParam() {
  const url = new URL(location.href);
  const raw = url.searchParams.get("item_id");
  if (raw === null) return null;
  url.searchParams.delete("item_id");
  history.replaceState(null, "", url.pathname + url.search + url.hash);
  return raw.trim() || null;
}

// 编号与 SPU 归属以详情接口为准（行上的 data-item 可能与用户手填的参数写法不同），
// 不按名称匹配，也绝不定位到别的 SKU
async function initItemJump(itemId) {
  let item;
  try {
    item = await XM.get(`/items/${encodeURIComponent(itemId)}`);
  } catch (e) {
    UI.apiFail(e, "定位单品失败");
    return;
  }
  const skuId = item && item.id ? String(item.id) : "";
  const spuId = item ? Number(item.spu_id) : NaN;
  if (!skuId || !(spuId > 0)) {
    UI.fail(`找不到单品 ${itemId}，可能已被删除`);
    return;
  }
  if (!spusList.some((s) => s.id === spuId)) {
    UI.fail(`单品 ${skuId} 所属的 SPU 不存在，可能已被删除`);
    return;
  }
  setExpanded(spuId, true);
  renderOverview();
  await ensureSkus([spuId]);                      // 等该 SPU 的全部 SKU 明细取回
  if (loadErrors.has(spuId)) return;              // 沿用组内加载失败与重试状态，不伪装成空结果
  if (!centerSkuRow(skuId)) UI.fail(`单品 ${skuId} 在库存列表中找不到，可能已被删除`);
}

// ---------- 页面初始化 ----------
window.PAGE_INIT = async function () {
  if (!UI.requireShop()) return;
  installScrollSizing();
  installTailWatch();
  // 事件委托绑在 tbody 上：重渲染后无需重绑
  // 三角按钮与 SPU 行空白区域都触发同一个展开/收起，一次点击只切换一次
  $("#spuRows").addEventListener("click", (e) => {
    const retry = e.target.closest("[data-retry-spu]");
    if (retry) { retrySpu(Number(retry.dataset.retrySpu)); return; }
    const btn = e.target.closest(".tri-btn[data-spu]");
    if (btn) { toggleSpu(Number(btn.dataset.spu)); return; }
    // 拖拽选中行内文字后松开也会触发 click，此时不应切换展开状态
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    // data-spu 也可能被其它功能（例如右键菜单识别目标行）加到不可展开的行上：
    // 这里以「行内存在三角按钮」作为可展开的判据，避免空 SPU 行被误展开
    const row = e.target.closest("tr.spu-row[data-spu]");
    if (row && row.querySelector(".tri-btn")) toggleSpu(Number(row.dataset.spu));
  });
  bindOpsBar();
  bindContextMenu();
  setInterval(sweepExpired, 60 * 1000);           // 到期实际清理，不等下次点击
  const jumpSpuId = consumeSpuJumpParam();
  const jumpItemId = consumeItemJumpParam();      // 两个参数同时出现时以 item_id 为准
  await loadOverview();
  if (jumpItemId) await initItemJump(jumpItemId);
  else if (jumpSpuId) await initSpuJump(jumpSpuId);
  // 概览页快捷入口带 ?op=xxx 跳来：进页后直接弹出对应小窗（定位在先，操作在后）
  const op = new URLSearchParams(location.search).get("op");
  if (op && OP_ACTIONS[op]) OP_ACTIONS[op]();
};
