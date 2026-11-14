/* 共享组件：库存操作小窗（入库 / 出库 / 盘点调整）
   三类操作字段结构相近，用一个描述表驱动渲染与提交体拼装；供「库存」页操作区调用。
   桌面为两栏：左侧选择单品并查看只读信息，右侧填写本次操作；窄屏改为上下排列。
   幂等键仅在小窗内部生成与轮换，不展示给用户 */
"use strict";

import { XM } from "xm/api";
import { UI } from "xm/ui";
import { fillItemOptions, itemOptionLabel } from "xm/components/item-picker";
import { ItemInfo } from "xm/components/item-info";

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

  // 属性定义供信息区展示：当前店铺的自定义属性（不含系统字段），只取一次
  async function loadFields() {
    const data = await XM.get("/fields");
    return data.fields.filter((f) => !f.system);
  }

  // 右侧填写区：原有的输入项与含义不变
  function buildFormHtml(cfg) {
    const req = `<span class="fieldreq">*</span>`;
    const cells = [];
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
    return `<div class="form-grid ops-fields" style="grid-template-columns:1fr">${cells.join("")}</div>` +
      (cfg.hint ? `<div class="hint">${esc(cfg.hint)}</div>` : "") +
      `<div data-result></div>`;
  }

  // 左侧：单品选择 + 只读信息
  function buildInfoHtml() {
    return `<label>单品 <span class="fieldreq">*</span></label>
      <select data-f="item"></select>
      <div class="ops-info" data-info></div>`;
  }

  // 打开某类操作小窗；onDone(res) 在真实落库成功后回调（供调用方刷新列表）
  // itemRef 非空时预选该单品（右键菜单从某一 SKU 直接发起操作时使用）
  function open(kind, { onDone, itemRef = null } = {}) {
    const cfg = KINDS[kind];
    if (!cfg) throw new Error("未知操作类型：" + kind);

    // 开窗时的单品下拉缓存与属性定义：并行取回，属性未到前信息区先给出基础信息与加载提示
    let itemsCache = [];
    let fieldsCache = null;

    const wrap = document.createElement("div");
    wrap.className = "dlg-form dlg-form-ops";
    wrap.innerHTML = `<div class="ops-layout">
        <div class="ops-side">${buildInfoHtml()}</div>
        <div class="ops-main">${buildFormHtml(cfg)}</div>
      </div>`;
    const { dlg, foot } = UI.baseDialog(cfg.label, wrap);

    const itemSel = wrap.querySelector('[data-f="item"]');
    const infoBox = wrap.querySelector("[data-info]");
    const resultBox = wrap.querySelector("[data-result]");
    const input = (name) => wrap.querySelector(`[data-f="${name}"]`);

    // 下拉只覆盖前 200 个单品（全店加载不在此处展开）。明确指定了目标又不在列表中时，
    // 用单个单品接口把它补进下拉再选中，避免静默落到第一条上。
    // 取不到（目标已被删除等）不抛出：保持下拉原样并明确告知，错误不再冒泡成一句
    // 「加载单品列表失败」。返回 false 表示目标不可用，调用方据此清空选择。
    async function ensureTargetOption(ref) {
      if (!ref || itemsCache.some((x) => String(x.id) === String(ref))) return true;
      let it;
      try {
        it = await XM.get(`/items/${encodeURIComponent(ref)}`);
      } catch (e) {
        console.info("[ops] 补齐单品下拉失败", ref, e);
        UI.fail("没能取到指定的单品，请重新选择", { title: "单品不可用" });
        return false;
      }
      itemsCache.push(it);
      itemSel.append(new Option(itemOptionLabel(it), it.id));
      return true;
    }
    const refreshPickers = async (selected) => {
      const cur = selected ?? itemSel.value;
      itemsCache = await fillItemOptions(itemSel);
      if (cur) {
        const ok = await ensureTargetOption(cur);
        itemSel.value = ok ? String(cur) : "";
        // 赋值后仍不等于目标（目标不在下拉里）就清空，不让选择框停在第一条上，
        // 否则信息区会把第一条的库存当成目标的库存显示出来
        if (itemSel.value !== String(cur)) itemSel.value = "";
      }
      showInfo();
    };

    // 信息区按选中编号取对象：同名单品文本相同，绝不能按名称匹配
    function currentItem() {
      return itemsCache.find((x) => String(x.id) === itemSel.value) || null;
    }
    function showInfo() {
      infoBox.innerHTML = ItemInfo.html(currentItem(), fieldsCache);
    }

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
    itemSel.addEventListener("change", showInfo);
    submit.onclick = async () => {
      let body;
      try { body = buildBody(); }
      catch (e) { UI.fail(e.message); return; }
      await UI.withBusy(submit, async () => {
        let res;
        try {
          res = await XM.post(cfg.endpoint, body);
        } catch (e) {
          // 失败保留窗口与输入；重试仍用本次操作的幂等键
          resultBox.className = "result-box error-box";
          resultBox.innerHTML = UI.apiFailHtml(e);
          UI.apiFail(e, `${cfg.label}失败`);
          return;
        }
        // 接口确认成功即关闭小窗，结果改在页面提示；幂等重放单独说明没有重复记账
        done = true;
        dlg.close();
        if (res.idempotent_replay) {
          UI.ok("这次提交之前已经成功记账，本次没有重复记账", { title: `${cfg.label}成功` });
        } else {
          UI.ok(`${cfg.label}已执行（流水 #${res.movement.id}）`);
        }
        // 关闭不等待刷新：刷新失败只说明列表未更新，不改报提交失败
        if (onDone) {
          try {
            await onDone(res);
          } catch (e) {
            UI.apiFail(e, "操作已成功，但刷新列表失败");
          }
        }
      });
    };

    // 单品列表与属性定义各自取回；属性未到时信息区先展示基础信息与加载提示
    dlg.showModal();
    setTimeout(() => itemSel.focus(), 50);
    loadFields()
      .then((fields) => { fieldsCache = fields; showInfo(); })
      .catch((e) => {
        fieldsCache = "error";
        showInfo();
        UI.apiFail(e, "加载属性列表失败");
      });
    refreshPickers(itemRef).catch((e) => { UI.apiFail(e, "加载单品列表失败"); });
  }

  return { open, KINDS };
})();

export { OpsForm };
