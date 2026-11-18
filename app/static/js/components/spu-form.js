/* 共享组件：SPU 新建与编辑小窗。属性输入与计算结果显示复用通用组件。 */
"use strict";

import { XM } from "xm/api";
import { UI } from "xm/ui";
import { AttributeValuesForm } from "xm/components/attribute-values-form";

const esc = UI.esc;

function valuesSectionHtml(fields, current, calculations = {}) {
  const plain = AttributeValuesForm.plainFields(fields);
  const computed = (fields || []).filter(AttributeValuesForm.isComputed);
  return `
    <div class="hint" style="margin:.9rem 0 .4rem">自定义属性值${
      current ? "（勾选「清除」= 提交 null 清除该值）" : ""}</div>
    <div class="form-grid" data-spu-values>
      ${AttributeValuesForm.gridHtml(fields, current, {
        withClear: !!current,
        emptyText: plain.length ? "" : "还没有可填写的 SPU 自定义属性",
      })}
    </div>
    ${computed.length ? `
      <div class="hint" style="margin:.9rem 0 .4rem">计算属性（由公式自动算出）</div>
      <div class="form-grid">${AttributeValuesForm.computedGridHtml(fields, calculations)}</div>` : ""}`;
}

function openCreate({ initialName = "", fields = [] } = {}) {
  return new Promise((resolve) => {
    const wrap = document.createElement("div");
    wrap.className = "dlg-form dlg-form-wide";
    wrap.innerHTML = `
      <label>SPU 名称 <span class="fieldreq">*</span></label>
      <input data-spu-name maxlength="200" value="${esc(initialName)}"
             placeholder="如：纯棉 T 恤" autocomplete="off">
      ${valuesSectionHtml(fields)}
      <div data-result></div>`;
    const { dlg, foot } = UI.baseDialog("新建 SPU", wrap);
    const nameEl = wrap.querySelector("[data-spu-name]");
    const resultBox = wrap.querySelector("[data-result]");
    let closed = false;
    let inFlight = false;
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    dlg.addEventListener("close", () => {
      closed = true;
      dlg.remove();
      if (!inFlight) finish(null);
    });

    const cancel = UI.mkBtn("取消", "btn-outline");
    const submit = UI.mkBtn("创建");
    cancel.type = submit.type = "button";
    foot.append(cancel, submit);
    cancel.onclick = () => dlg.close();

    const doSubmit = () => UI.withBusy(submit, async () => {
      if (closed) return;
      const name = nameEl.value.trim();
      if (!name) {
        UI.fail("请填写 SPU 名称");
        nameEl.focus();
        return;
      }
      const collected = AttributeValuesForm.collectChanges(fields, wrap);
      if (collected.bad) {
        resultBox.innerHTML = UI.warnHtml("属性值格式不正确", "请检查数值输入后重试。");
        return;
      }
      inFlight = true;
      resultBox.innerHTML = "";
      let spu;
      try {
        spu = await XM.post("/spus", { name, values: collected.values });
      } catch (error) {
        inFlight = false;
        if (closed) {
          finish(null);
          return;
        }
        resultBox.innerHTML = UI.apiFailHtml(error);
        return;
      }
      inFlight = false;
      finish(spu);
      dlg.close();
      UI.ok(`SPU「${spu.name}」已创建（#${spu.id}）`);
    });
    submit.onclick = doSubmit;
    nameEl.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      doSubmit();
    });

    dlg.showModal();
    setTimeout(() => {
      nameEl.focus();
      if (nameEl.value) nameEl.select();
    }, 50);
  });
}

async function openEdit({ spu, fields = [], onSaved } = {}) {
  let detail;
  try {
    detail = await XM.get(`/spus/${spu.id}`);
  } catch (error) {
    UI.apiFail(error, "加载 SPU 详情失败");
    return null;
  }

  const wrap = document.createElement("div");
  wrap.className = "dlg-form dlg-form-wide";
  wrap.innerHTML = `
    <label>SPU 名称 <span class="fieldreq">*</span></label>
    <input data-spu-name maxlength="200" value="${esc(detail.name)}" autocomplete="off">
    ${valuesSectionHtml(fields, detail.values, detail.calculations)}
    <div class="hint">所属 SKU 数：${detail.sku_count}</div>
    <div data-result></div>`;
  const { dlg, foot } = UI.baseDialog(`编辑 SPU #${detail.id}`, wrap);
  const nameEl = wrap.querySelector("[data-spu-name]");
  const resultBox = wrap.querySelector("[data-result]");

  const cancel = UI.mkBtn("取消", "btn-outline");
  const save = UI.mkBtn("保存修改");
  cancel.type = save.type = "button";
  foot.append(cancel, save);
  cancel.onclick = () => dlg.close();
  dlg.addEventListener("close", () => dlg.remove());

  save.onclick = () => UI.withBusy(save, async () => {
    const body = {};
    const name = nameEl.value.trim();
    if (!name) {
      UI.fail("请填写 SPU 名称");
      nameEl.focus();
      return;
    }
    if (name !== detail.name) body.name = name;
    const collected = AttributeValuesForm.collectChanges(fields, wrap, detail.values || {});
    if (collected.bad) {
      resultBox.innerHTML = UI.warnHtml("属性值格式不正确", "请检查数值输入后重试。");
      return;
    }
    if (Object.keys(collected.values).length) body.values = collected.values;
    if (!Object.keys(body).length) {
      UI.toast("没有任何修改");
      dlg.close();
      return;
    }
    let updated;
    try {
      updated = await XM.patch(`/spus/${detail.id}`, body);
    } catch (error) {
      resultBox.innerHTML = UI.apiFailHtml(error);
      return;
    }
    UI.ok(`SPU「${updated.name}」已更新`);
    dlg.close();
    if (onSaved) onSaved(updated);
  });

  dlg.showModal();
  setTimeout(() => nameEl.focus(), 50);
  return null;
}

export const SpuForm = { openCreate, openEdit };
