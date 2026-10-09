/* 共享组件：库存操作小窗（入库 / 出库 / 盘点调整）
   三类操作字段结构相近，用一个描述表驱动渲染与提交体拼装；供「库存」页操作区调用。
   幂等键仅在小窗内部生成与轮换，不展示给用户 */
"use strict";

const OpsForm = (() => {
  const esc = UI.esc;

  const genKey = () => "web-" + (crypto.randomUUID
    ? crypto.randomUUID()
    : Date.now() + "-" + Math.random().toString(36).slice(2, 10));

  // 三类操作的领域配置：标题 + 端点 + 字段（kind: qty=数量+单价 / target=目标库存）
  const KINDS = {
    in: {
      endpoint: "/inbound", label: "入库",
      qty: { label: "数量", min: 1 },
      price: { label: "采购单价（元）", placeholder: "可空" },
      extras: [
        { key: "batch", label: "批次号", max: 100 },
        { key: "supplier", label: "供应商", max: 100 },
        { key: "note", label: "备注", max: 200 },
      ],
      submitText: "提交入库",
    },
    out: {
      endpoint: "/outbound", label: "出库",
      qty: { label: "数量", min: 1 },
      price: { label: "成交单价（元）", placeholder: "可空" },
      extras: [
        { key: "channel", label: "销售渠道", max: 100, placeholder: "如：闲鱼 / 微信" },
        { key: "customer", label: "客户信息", max: 100 },
        { key: "note", label: "备注", max: 200 },
      ],
      submitText: "提交出库",
    },
    adjust: {
      endpoint: "/adjust", label: "盘点调整",
      target: { label: "目标库存" },
      extras: [{ key: "note", label: "备注", max: 200 }],
      hint: "填「盘点后的应有数量」；系统自动记录 目标−当前 的带符号增量，"
            + "与当前数量相同时会被拦下",
      submitText: "提交盘点",
    },
  };

  // 开窗时的单品下拉：复用 UI.fillItemOptions，返回值用于库存提示
  let itemsCache = [];

  function buildFormHtml(cfg) {
    const req = `<span class="fieldreq">*</span>`;
    const cells = [`<div><label>单品 ${req}</label>
        <select data-f="item"></select><div class="hint" data-qty></div></div>`];
    if (cfg.qty) {
      cells.push(`<div><label>${cfg.qty.label} ${req}</label>
        <input data-f="qty" type="number" min="${cfg.qty.min}" step="1" value="1"></div>`);
      cells.push(`<div><label>${cfg.price.label}</label>
        <input data-f="price" type="number" min="0" step="0.01" placeholder="${cfg.price.placeholder}"></div>`);
    }
    if (cfg.target) {
      cells.push(`<div><label>${cfg.target.label} ${req}</label>
        <input data-f="target" type="number" min="0" step="1" value="0"></div>`);
    }
    for (const ex of cfg.extras) {
      cells.push(`<div><label>${ex.label}</label>
        <input data-f="${ex.key}" maxlength="${ex.max}" autocomplete="off" placeholder="${ex.placeholder || ""}"></div>`);
    }
    return `<div class="form-grid" style="grid-template-columns:1fr">${cells.join("")}</div>` +
      (cfg.hint ? `<div class="hint">${esc(cfg.hint)}</div>` : "") +
      `<div data-result></div>`;
  }

  const TYPE_BADGE = { in: `<span class="badge green">入库</span>`,
                       out: `<span class="badge red">出库</span>`,
                       adjust: `<span class="badge gray">盘点</span>` };

  function renderResult(box, cfg, res) {
    const m = res.movement;
    // 重复提交的成功结果不再标注「幂等重放」——这是接口机制词，用户只需要知道
    // 「没有重复记账」；具体机制与幂等键在控制台留一条记录便于排查。
    if (res.idempotent_replay) console.info("[ops] idempotent replay", m.id);
    const badges = `<span class="badge fill">已执行</span>`;
    const dl = [
      ["流水 ID", m.id],
      ["类型", TYPE_BADGE[m.type] || `<span class="muted">未知类型</span>`],
      ["数量增量", UI.fmtDelta(m.qty_delta)],
      ["单价", UI.fmtMoney(m.unit_price)],
      m.target_qty !== null && m.target_qty !== undefined ? ["目标库存", m.target_qty] : null,
      m.batch_no ? ["批次", esc(m.batch_no)] : null,
      m.supplier ? ["供应商", esc(m.supplier)] : null,
      m.channel ? ["渠道", esc(m.channel)] : null,
      m.customer ? ["客户", esc(m.customer)] : null,
      m.note ? ["备注", esc(m.note)] : null,
    ].filter(Boolean);
    box.className = "result-box";
    box.innerHTML = `<div class="headline">${badges} ${esc(cfg.label)}结果</div>` +
      `<div style="margin-top:.5rem">当前库存：<b>${res.current_qty}</b></div>` +
      `<table class="mini-table" style="margin-top:.5rem">` +
      dl.map(([k, v]) => `<tr><td class="muted" style="width:5.625rem">${k}</td><td>${v}</td></tr>`).join("") +
      `</table>`;
  }

  // 打开某类操作小窗；onDone(res) 在真实落库成功后回调（供调用方刷新列表）
  // itemRef 非空时预选该单品（右键菜单从某一 SKU 直接发起操作时使用）
  function open(kind, { onDone, itemRef = null } = {}) {
    const cfg = KINDS[kind];
    if (!cfg) throw new Error("未知操作类型：" + kind);

    const wrap = document.createElement("div");
    wrap.className = "dlg-form";
    wrap.innerHTML = buildFormHtml(cfg);
    const { dlg, foot } = UI.baseDialog(cfg.label, wrap);

    const itemSel = wrap.querySelector('[data-f="item"]');
    const qtyHint = wrap.querySelector("[data-qty]");
    const resultBox = wrap.querySelector("[data-result]");
    const input = (name) => wrap.querySelector(`[data-f="${name}"]`);

    const showQtyHint = () => {
      const it = itemsCache.find((x) => String(x.id) === itemSel.value);
      qtyHint.textContent = it ? `当前库存：${it.qty}` : "";
    };
    const refreshPickers = async (selected) => {
      const cur = selected ?? itemSel.value;
      itemsCache = await UI.fillItemOptions(itemSel);
      if (cur) itemSel.value = cur;
      showQtyHint();
    };

    const buildBody = () => {
      // 单品标识是「SPU 号-组内序号」字符串，不可转数值
      const body = { item_id: itemSel.value, idempotency_key: dlg.dataset.key, dry_run: false };
      if (!body.item_id) throw new Error("请选择单品");
      if (cfg.qty) {
        const qty = Number(input("qty").value);
        if (!Number.isInteger(qty) || qty <= 0) throw new Error("数量必须是正整数");
        body.qty = qty;
        const p = input("price").value.trim();
        if (p !== "") {
          const n = Number(p);
          if (!Number.isFinite(n) || n < 0) throw new Error("单价格式不正确");
          body.unit_price = n;
        }
      }
      if (cfg.target) {
        const t = Number(input("target").value);
        if (!Number.isInteger(t) || t < 0) throw new Error("目标库存必须是不小于 0 的整数");
        body.target_qty = t;
      }
      for (const ex of cfg.extras) {
        const el = input(ex.key);
        const v = el.value.trim();
        if (v !== "") body[ex.key] = v;
      }
      return body;
    };

    let done = false;
    const close = UI.mkBtn("关闭", "btn-outline"), submit = UI.mkBtn(cfg.submitText);
    close.type = submit.type = "button";
    foot.append(close, submit);
    close.onclick = () => { done = true; dlg.close(); };
    dlg.addEventListener("close", () => { done = true; dlg.remove(); });

    dlg.dataset.key = genKey();
    itemSel.addEventListener("change", showQtyHint);
    submit.onclick = async () => {
      let body;
      try { body = buildBody(); }
      catch (e) { UI.fail(e.message); return; }
      await UI.withBusy(submit, async () => {
        let res;
        try {
          res = await XM.post(cfg.endpoint, body);
        } catch (e) {
          resultBox.className = "result-box error-box";
          resultBox.innerHTML = UI.apiFailHtml(e);
          UI.apiFail(e, `${cfg.label}失败`);
          return;
        }
        renderResult(resultBox, cfg, res);
        if (res.idempotent_replay) UI.toast("这次提交之前已经成功记账，本次没有重复记账");
        else UI.ok(`${cfg.label}已执行（流水 #${res.movement.id}）`);
        // 真实落库后换新幂等键，避免下次误重放
        dlg.dataset.key = genKey();
        try {
          await refreshPickers(body.item_id);
        } catch (e) {
          UI.apiFail(e, "操作已成功，但刷新单品列表失败");
        }
        if (onDone) {
          try {
            await onDone(res);
          } catch (e) {
            UI.apiFail(e, "操作已成功，但刷新列表失败");
          }
        }
      });
    };

    dlg.showModal();
    refreshPickers(itemRef).catch((e) => UI.apiFail(e, "加载单品列表失败"));
    setTimeout(() => itemSel.focus(), 50);
  }

  return { open, KINDS };
})();
