/* 库存操作页：入库 / 出库 / 盘点 —— dry_run 预览、幂等键、结构化错误展示 */
"use strict";

const $ = UI.$;

let itemsCache = [];

const genKey = () => "web-" + (crypto.randomUUID
  ? crypto.randomUUID()
  : Date.now() + "-" + Math.random().toString(36).slice(2, 10));

// 三个表单的领域配置：字段 → 请求体拼装
const FORMS = [
  {
    form: "#fIn", endpoint: "/inbound", label: "入库",
    fields: { qty: "#inQty", price: "#inPrice", batch: "#inBatch",
              supplier: "#inSupplier", note: "#inNote" },
    item: "#inItem", dry: "#inDry",
  },
  {
    form: "#fOut", endpoint: "/outbound", label: "出库",
    fields: { qty: "#outQty", price: "#outPrice", channel: "#outChannel",
              customer: "#outCustomer", note: "#outNote" },
    item: "#outItem", dry: "#outDry",
  },
  {
    form: "#fAdj", endpoint: "/adjust", label: "盘点调整",
    fields: { target: "#adjTarget", note: "#adjNote" },
    item: "#adjItem", dry: "#adjDry",
  },
];

async function refreshPickers(selected) {
  try {
    itemsCache = await UI.fillItemOptions(document.querySelector(FORMS[0].item));
    FORMS.slice(1).forEach((cfg) => {
      const sel = document.querySelector(cfg.item);
      const cur = selected ?? sel.value;
      UI.fillItemOptions(sel).then(() => { if (cur) sel.value = cur; });
    });
    if (selected) document.querySelector(FORMS[0].item).value = selected;
    FORMS.forEach(showQtyHint);
  } catch (e) { UI.apiFail(e, "加载单品列表失败"); }
}

function showQtyHint(cfg) {
  const sel = document.querySelector(cfg.item);
  const hint = sel.parentElement.querySelector("[data-qty]");
  const it = itemsCache.find((x) => String(x.id) === sel.value);
  hint.textContent = it ? `当前库存：${it.qty}` : "";
}

// 提交体拼装（三种操作共用骨架）
function buildBody(cfg, { dryRun }) {
  const form = document.querySelector(cfg.form);
  const body = {
    item_id: Number(document.querySelector(cfg.item).value),
    idempotency_key: form.dataset.key,
    dry_run: dryRun,
  };
  const f = cfg.fields;
  if (!body.item_id) throw new Error("请选择单品");
  if (f.qty) {
    const qty = Number($(f.qty).value);
    if (!Number.isInteger(qty) || qty <= 0) throw new Error("数量必须是正整数");
    body.qty = qty;
    const p = $(f.price).value.trim();
    if (p !== "") {
      const n = Number(p);
      if (isNaN(n) || n < 0) throw new Error("单价格式不正确");
      body.unit_price = n;
    }
  }
  if (f.target) {
    const t = Number($(f.target).value);
    if (!Number.isInteger(t) || t < 0) throw new Error("目标库存必须是不小于 0 的整数");
    body.target_qty = t;
  }
  for (const key of ["batch", "supplier", "channel", "customer", "note"]) {
    const sel = f[key];
    if (sel) {
      const v = $(sel).value.trim();
      if (v !== "") body[key] = v;
    }
  }
  return body;
}

const TYPE_BADGE = { in: `<span class="badge green">入库</span>`,
                     out: `<span class="badge red">出库</span>`,
                     adjust: `<span class="badge gray">盘点</span>` };

function renderOpResult(box, cfg, res) {
  const m = res.movement;
  const badges = [
    res.dry_run ? `<span class="badge">dry_run 预览</span>` : `<span class="badge fill">已执行</span>`,
    res.idempotent_replay ? `<span class="badge red">幂等重放</span>` : "",
  ].join(" ");
  const qtyLine = res.dry_run
    ? `库存变化：<b>${res.current_qty}</b> → <b>${res.new_qty}</b>（预览，未落库）`
    : `当前库存：<b>${res.current_qty}</b>`;
  const dl = [
    ["流水 ID", m.id ?? "（预览无 ID）"],
    ["类型", TYPE_BADGE[m.type] || UI.esc(m.type)],
    ["数量增量", UI.fmtDelta(m.qty_delta)],
    ["单价", UI.fmtMoney(m.unit_price)],
    m.target_qty !== null && m.target_qty !== undefined ? ["目标库存", m.target_qty] : null,
    m.batch_no ? ["批次", UI.esc(m.batch_no)] : null,
    m.supplier ? ["供应商", UI.esc(m.supplier)] : null,
    m.channel ? ["渠道", UI.esc(m.channel)] : null,
    m.customer ? ["客户", UI.esc(m.customer)] : null,
    m.note ? ["备注", UI.esc(m.note)] : null,
  ].filter(Boolean);
  let html = `<div class="result-box"><div class="headline">${badges} ${cfg.label}结果</div>` +
    `<div style="margin-top:.5rem">${qtyLine}</div>
     <table class="mini-table" style="margin-top:.5rem">` +
    dl.map(([k, v]) => `<tr><td class="muted" style="width:5.625rem">${k}</td><td>${v}</td></tr>`).join("") +
    `</table>`;
  if (res.dry_run) {
    html += `<div style="margin-top:.8rem">
      <button type="button" class="btn-sm" data-confirm-exec>确认执行（沿用同一幂等键）</button>
      <span class="hint" style="display:inline">预览通过后可直接执行</span></div>`;
  }
  box.innerHTML = html + `</div>`;

  const execBtn = box.querySelector("[data-confirm-exec]");
  if (execBtn) execBtn.addEventListener("click", () => submitOp(cfg, execBtn, false));
}

async function submitOp(cfg, btn, dryOverride) {
  const form = document.querySelector(cfg.form);
  const box = form.parentElement.querySelector("[data-result]");
  const dry = dryOverride !== undefined ? dryOverride
            : document.querySelector(cfg.dry).checked;
  let body;
  try { body = buildBody(cfg, { dryRun: dry }); }
  catch (e) { UI.fail(e.message); return; }
  await UI.withBusy(btn, async () => {
    try {
      const res = await XM.post(cfg.endpoint, body);
      renderOpResult(box, cfg, res);
      if (!res.dry_run) {
        if (res.idempotent_replay) UI.toast("幂等键命中：返回首次提交的结果，未重复记账");
        else UI.ok(`${cfg.label}已执行（流水 #${res.movement.id}）`);
        // 真实落库后换新幂等键，避免下次误重放
        form.dataset.key = genKey();
        form.querySelector("[data-key]").textContent = form.dataset.key;
        await refreshPickers(String(body.item_id));
      }
    } catch (e) {
      box.innerHTML = UI.apiFailHtml(e);
      UI.apiFail(e, `${cfg.label}失败`);
    }
  });
}

window.PAGE_INIT = async function () {
  if (!UI.requireShop()) return;

  FORMS.forEach((cfg) => {
    const form = document.querySelector(cfg.form);
    form.dataset.key = genKey();
    form.querySelector("[data-key]").textContent = form.dataset.key;
    form.querySelector("[data-regen]").addEventListener("click", () => {
      form.dataset.key = genKey();
      form.querySelector("[data-key]").textContent = form.dataset.key;
    });
    document.querySelector(cfg.item).addEventListener("change", () => showQtyHint(cfg));
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      submitOp(cfg, ev.submitter || form.querySelector("button[type=submit]"),
               document.querySelector(cfg.dry).checked);
    });
  });

  await refreshPickers();
};
