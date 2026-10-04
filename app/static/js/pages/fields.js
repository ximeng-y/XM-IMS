/* 自定义字段管理页：列表 / 新建 / 改名 / 结构转换（预览报告→二次确认→执行）/ 删除确认 */
"use strict";

const TYPE_LABEL = {
  string: "文本", bool: "开关", int: "整数",
  decimal: "小数", percent: "百分比",
};
const ROUND_LABEL = { ceil: "向上取整", floor: "向下取整", round: "四舍五入" };

function typeText(f) {
  return TYPE_LABEL[f.type] + (f.type === "decimal" ? `（${f.scale} 位）` : "") +
         (f.allow_negative ? "，允许负数" : "");
}

let fieldCache = [];

async function loadFields() {
  const tbody = $("#fieldRows");
  try {
    const data = await XM.get("/fields");
    fieldCache = data.fields;
    if (!data.fields.length) {
      tbody.innerHTML = `<tr><td colspan="7" class="t-empty">（异常：系统字段缺失）</td></tr>`;
      return;
    }
    tbody.innerHTML = data.fields.map((f, i) => {
      if (f.system) {
        return `<tr>
          <td><span class="badge fill">系统</span></td>
          <td class="muted mono">—</td>
          <td>${UI.esc(f.name)}</td>
          <td>${TYPE_LABEL[f.type] || UI.esc(f.type)}</td>
          <td class="muted">—</td><td class="num muted">—</td>
          <td class="muted">只读</td>
        </tr>`;
      }
      return `<tr>
        <td><span class="badge">自定义</span></td>
        <td class="num mono">${f.id}</td>
        <td>${UI.esc(f.name)}</td>
        <td>${typeText(f)}</td>
        <td>${f.allow_negative ? "是" : "否"}</td>
        <td class="num">${f.type === "decimal" ? f.scale : "—"}</td>
        <td>
          <button type="button" class="btn-outline btn-sm" data-act="rename" data-idx="${i}">改名</button>
          <button type="button" class="btn-outline btn-sm" data-act="convert" data-idx="${i}">转换</button>
          <button type="button" class="btn-danger-outline btn-sm" data-act="del" data-idx="${i}">删除</button>
        </td>
      </tr>`;
    }).join("");
    $$("button[data-act]", tbody).forEach((b) => {
      const f = fieldCache[Number(b.dataset.idx)];
      if (b.dataset.act === "rename") b.addEventListener("click", () => renameFlow(f));
      if (b.dataset.act === "convert") b.addEventListener("click", () => convertFlow(f));
      if (b.dataset.act === "del") b.addEventListener("click", () => deleteFlow(f));
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="7" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
  }
}

async function renameFlow(f) {
  const name = await UI.promptDialog({
    title: `字段「${f.name}」改名`, label: "新名称（数据不动）",
    value: f.name, maxLength: 50,
  });
  if (name === null || name === f.name) return;
  try {
    await XM.patch(`/fields/${f.id}`, { name });
    UI.ok(`字段已改名为「${name}」`, { title: "FIELD_RENAMED" });
    loadFields();
  } catch (e) { UI.apiFail(e, "改名失败"); }
}

async function deleteFlow(f) {
  const confirmed = await UI.confirmDialog({
    title: `删除字段「${f.name}」`,
    bodyHtml: `<p>将删除该字段（<b>${typeText(f)}</b>）并<b style="color:var(--danger)">级联清空所有单品的该字段值</b>，操作不可恢复。</p>`,
    confirmText: "确认删除", danger: true,
  });
  if (!confirmed) return;
  try {
    const res = await XM.del(`/fields/${f.id}?confirm=true`);
    UI.ok(`字段「${res.field.name}」已删除，连带清除 ${res.deleted_values} 个字段值`,
          { title: "FIELD_DELETED" });
    loadFields();
  } catch (e) { UI.apiFail(e, "删除失败"); }
}

// ---------- 结构转换：预览报告 → 用户选择取整/二次确认清空 → 执行 ----------
function convertFlow(f) {
  let done = false;
  // 已就「清空值」明确警告过的选项签名；选项一变即失效，避免按旧选项二次确认
  let clearAcked = null;
  const wrap = document.createElement("div");
  wrap.innerHTML = `
    <div class="form-grid" style="grid-template-columns:1fr 1fr">
      <div>
        <label>目标类型</label>
        <select id="cvType">
          <option value="">（保持：${TYPE_LABEL[f.type]}）</option>
          ${Object.entries(TYPE_LABEL).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}
        </select>
      </div>
      <div>
        <label>目标小数位（仅小数类型）</label>
        <input id="cvScale" type="number" min="0" max="6" placeholder="${f.type === "decimal" ? "默认沿用 " + f.scale : "默认 2"}">
      </div>
      <div>
        <label>允许负数</label>
        <select id="cvNeg">
          <option value="">（保持：${f.allow_negative ? "允许" : "不允许"}）</option>
          <option value="yes">允许负数</option>
          <option value="no">不允许负数</option>
        </select>
      </div>
      <div>
        <label>取整方式（精度损失时）</label>
        <select id="cvRound">
          <option value="">（未指定）</option>
          ${Object.entries(ROUND_LABEL).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}
        </select>
      </div>
    </div>
    <div id="cvWarn"></div>
    <div id="cvReport"></div>
    <div id="cvError"></div>`;
  const { dlg, foot } = UI.baseDialog(`转换字段「${f.name}」（${typeText(f)}）`, wrap);

  // 当前选项签名：任一选项变化都会重置「已警告」状态
  const opts = () => [$("#cvType", wrap).value, $("#cvScale", wrap).value,
                      $("#cvNeg", wrap).value, $("#cvRound", wrap).value].join("|");
  const body = (confirmClear) => {
    const t = $("#cvType", wrap).value;
    const s = $("#cvScale", wrap).value;
    const neg = $("#cvNeg", wrap).value;
    const r = $("#cvRound", wrap).value;
    const out = {
      rounding: r || null,
      confirm_clear: confirmClear,
      dry_run: false,
    };
    if (t) out.new_type = t;
    if (s !== "") out.new_scale = Number(s);
    if (neg) out.new_allow_negative = neg === "yes";
    return out;
  };

  const reset = () => {
    clearAcked = null;
    $("#cvWarn", wrap).innerHTML = "";
    $("#cvReport", wrap).innerHTML = "";
    $("#cvError", wrap).innerHTML = "";
  };
  ["#cvType", "#cvScale", "#cvNeg", "#cvRound"].forEach((sel) =>
    $(sel, wrap).addEventListener("change", reset));

  const preview = UI.mkBtn("预览", "btn-outline");
  const execute = UI.mkBtn("执行转换");
  const close = UI.mkBtn("关闭", "btn-outline");
  preview.type = execute.type = close.type = "button";
  foot.append(preview, execute, close);

  close.onclick = () => { done = true; dlg.close(); };
  dlg.addEventListener("close", () => { done = true; dlg.remove(); });

  preview.onclick = () => UI.withBusy(preview, async () => {
    // 预览即撤销旧的清空确认，避免失败时残留 confirm_clear 状态
    clearAcked = null;
    $("#cvError", wrap).innerHTML = "";
    $("#cvWarn", wrap).innerHTML = "";
    try {
      const res = await XM.post(`/fields/${f.id}/convert`, { ...body(false), dry_run: true });
      if (res.rounding_required) {
        $("#cvReport", wrap).innerHTML = "";
        $("#cvError", wrap).innerHTML = UI.warnHtml(
          "本次转换存在精度损失，请先选择取整方式",
          `将影响 ${res.affected_items} 个值；请选择取整方式后重新点击「预览」。`);
        $("#cvRound", wrap).focus();
        return;
      }
      // 清空类转换：预览照常展示内容，仅在报告上方加一条警告带
      if (res.requires_clear) {
        $("#cvWarn", wrap).innerHTML = UI.warnHtml("转换将清空该字段的全部值",
          `「${TYPE_LABEL[f.type] || f.type}」与目标类型不可互转，` +
          `${res.affected_items} 个值将被清空；确认无误后点击「执行转换」。`);
      }
      renderReport($("#cvReport", wrap), res, true);
    } catch (e) {
      $("#cvReport", wrap).innerHTML = "";
      $("#cvError", wrap).innerHTML = UI.apiFailHtml(e);
      // 有损但未选取整：提示用户选择取整方式
      if (e.code === "ROUNDING_REQUIRED") $("#cvRound", wrap).focus();
    }
  });

  execute.onclick = () => UI.withBusy(execute, async () => {
    $("#cvError", wrap).innerHTML = "";
    // 同一选项已被警告过清空风险 → 本次点击即用户确认，附带 confirm_clear
    const confirmed = clearAcked === opts();
    try {
      const res = await XM.post(`/fields/${f.id}/convert`, body(confirmed));
      UI.ok(`字段「${f.name}」转换完成`, { title: "FIELD_CONVERTED" });
      done = true; dlg.close();
      loadFields();
    } catch (e) {
      if (e.code === "CONVERT_REQUIRES_CLEAR") {
        clearAcked = opts();
        $("#cvError", wrap).innerHTML =
          UI.warnHtml("确认清空并转换请再次点击「执行转换」",
            "该操作会清空字段的全部值且不可恢复；如需核对将受影响的值，请先点击「预览」。");
      } else {
        $("#cvError", wrap).innerHTML = UI.apiFailHtml(e);
      }
    }
  });

  dlg.showModal();
}

// 转换报告 / 执行结果渲染（预览与正式执行共用结构）
function renderReport(box, res, isPreview) {
  const bits = [];
  bits.push(isPreview || res.dry_run ? `<span class="badge">预览</span>` : `<span class="badge fill">已执行</span>`);
  // 清空类转换没有值换算可言，不做「无损/有损」判定，只标「清空值」
  const cleared = res.requires_clear || res.cleared;
  if (cleared) bits.push(`<span class="badge red">清空值</span>`);
  else if (res.lossy) bits.push(`<span class="badge red">有损</span>`);
  else if (res.converted || res.dry_run) bits.push(`<span class="badge green">无损</span>`);

  let html = `<div class="result-box"><div class="headline">${bits.join(" ")}</div>`;
  if (res.description) html += `<div style="margin-top:.4rem">换算规则：${UI.esc(res.description)}</div>`;
  if (res.note) html += `<div style="margin-top:.4rem">${UI.esc(res.note)}</div>`;
  html += `<div style="margin-top:.4rem">受影响单品：<b>${res.affected_items}</b> 个</div>`;

  if (res.samples && res.samples.length) {
    html += `<table class="mini-table" style="margin-top:.6rem">
      <thead><tr><th>单品</th><th>旧值</th><th>新值</th></tr></thead><tbody>` +
      res.samples.map((s) => `
        <tr><td>${UI.esc(s.item_id)} ${UI.esc(s.item_name)}</td>
            <td>${UI.fmtVal(s.old)}</td>
            <td>${s.new === null ? `<span class="muted">清空</span>` : UI.fmtVal(s.new)}</td></tr>`).join("") +
      `</tbody></table>`;
    if (res.affected_items > res.samples.length) {
      html += `<div class="hint">仅展示前 ${res.samples.length} 条样例</div>`;
    }
  } else {
    html += `<div class="hint">（该字段暂无值，仅更新结构定义）</div>`;
  }
  if (res.rounding) html += `<div class="hint">取整方式：${ROUND_LABEL[res.rounding]}</div>`;
  box.innerHTML = html + `</div>`;
}

// ---------- 新建字段表单联动 ----------
window.PAGE_INIT = async function () {
  if (!UI.requireShop()) return;

  const typeSel = $("#fldType"), scaleInp = $("#fldScale"), negChk = $("#fldNeg");
  const syncType = () => {
    const t = typeSel.value;
    scaleInp.disabled = t !== "decimal";
    negChk.disabled = t === "string" || t === "bool";
    if (negChk.disabled) negChk.checked = false;
  };
  typeSel.addEventListener("change", syncType);
  syncType();

  $("#fCreate").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const btn = ev.target.querySelector("button[type=submit]");
    const name = $("#fldName").value.trim();
    if (!name) { UI.fail("请填写字段名称"); return; }
    const body = {
      name, type: typeSel.value,
      allow_negative: negChk.checked && !negChk.disabled,
    };
    if (typeSel.value === "decimal" && scaleInp.value !== "") {
      body.scale = Number(scaleInp.value);
    }
    UI.withBusy(btn, async () => {
      try {
        const f = await XM.post("/fields", body);
        UI.resultBox("#createResult", {
          html: `<div class="headline"><span class="badge">FIELD_CREATED</span> ` +
                `「${UI.esc(f.name)}」（${typeText(f)}）已创建</div>`,
        });
        $("#fldName").value = "";
        loadFields();
      } catch (e) {
        UI.resultBox("#createResult", { error: true, html: UI.apiFailHtml(e) });
      }
    });
  });

  await loadFields();
};
