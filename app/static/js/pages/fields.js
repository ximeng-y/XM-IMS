/* 自定义字段管理页：列表 / 新建（普通字段或数学计算字段）/ 改名 /
   编辑公式 / 结构转换（预览报告→二次确认→执行）/ 删除确认
   计算字段没有「转换」入口：公式、结果类型与显示位数走「编辑公式」端点。 */
"use strict";

const TYPE_LABEL = {
  string: "文本", bool: "开关", int: "整数",
  decimal: "小数", percent: "百分比",
};
const ROUND_LABEL = { ceil: "向上取整", floor: "向下取整", round: "四舍五入" };

const COMPUTED_VALUE = "__computed__";

// 计算字段的类型呈现：类型 + 显示位数 + 是否允许负数
function computedText(f) {
  return `数学计算 · ${TYPE_LABEL[f.type] || f.type}` +
         (f.type === "decimal" ? `（${f.scale} 位）` : "") +
         (f.allow_negative ? "，允许负数" : "");
}

function typeText(f) {
  if (f.computed) return computedText(f);
  return TYPE_LABEL[f.type] + (f.type === "decimal" ? `（${f.scale} 位）` : "") +
         (f.allow_negative ? "，允许负数" : "");
}

// 公式 token → 人类可读文本（与编辑器同一套符号呈现）
function formulaText(f) {
  const tokens = (f.formula && f.formula.tokens) || [];
  if (!tokens.length) return "";
  const defs = fieldCache;
  return tokens.map((t) => {
    if (t.kind === "number") return t.value;
    if (t.kind === "system") return { qty: "库存数量" }[t.key] || t.key;
    if (t.kind === "field") {
      const ref = defs.find((x) => x.uid === t.uid);
      return ref ? ref.name : "已失效的字段";
    }
    return FormulaEditor.SYMBOL_TEXT[t.value] || t.value;
  }).join(" ");
}

let fieldCache = [];

async function loadFields() {
  const tbody = $("#fieldRows");
  try {
    const data = await XM.get("/fields");
    fieldCache = data.fields;
    if (!data.fields.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="t-empty">字段列表为空，请刷新页面后重试</td></tr>`;
      return;
    }
    tbody.innerHTML = data.fields.map((f, i) => {
      if (f.system) {
        return `<tr>
          <td><span class="badge fill">系统</span></td>
          <td class="muted mono">—</td>
          <td title="${UI.esc(f.name)}">${UI.esc(f.name)}</td>
          <td title="${UI.esc(TYPE_LABEL[f.type] || f.type)}">${TYPE_LABEL[f.type] || UI.esc(f.type)}</td>
          <td class="muted">—</td>
          <td class="muted">—</td><td class="num muted">—</td>
          <td class="muted">只读</td>
        </tr>`;
      }
      // 计算字段无结构转换：公式、结果类型与精度都在「编辑公式」里改
      const actions = f.computed
        ? `<button type="button" class="btn-outline btn-sm" data-act="rename" data-idx="${i}">改名</button>
           <button type="button" class="btn-outline btn-sm" data-act="formula" data-idx="${i}">编辑公式</button>
           <button type="button" class="btn-danger-outline btn-sm" data-act="del" data-idx="${i}">删除</button>`
        : `<button type="button" class="btn-outline btn-sm" data-act="rename" data-idx="${i}">改名</button>
           <button type="button" class="btn-outline btn-sm" data-act="convert" data-idx="${i}">转换</button>
           <button type="button" class="btn-danger-outline btn-sm" data-act="del" data-idx="${i}">删除</button>`;
      return `<tr>
        <td><span class="badge">${f.computed ? "计算" : "自定义"}</span></td>
        <td class="num mono">${f.id}</td>
        <td title="${UI.esc(f.name)}">${UI.esc(f.name)}</td>
        <td title="${UI.esc(typeText(f))}">${typeText(f)}</td>
        <td class="fx-cell" title="${f.computed ? UI.esc(formulaText(f)) : ""}">${f.computed ? UI.esc(formulaText(f)) : `<span class="muted">—</span>`}</td>
        <td>${f.allow_negative ? "是" : "否"}</td>
        <td class="num">${f.computed ? f.scale : (f.type === "decimal" ? f.scale : "—")}</td>
        <td>${actions}</td>
      </tr>`;
    }).join("");
    $$("button[data-act]", tbody).forEach((b) => {
      const f = fieldCache[Number(b.dataset.idx)];
      if (b.dataset.act === "rename") b.addEventListener("click", () => renameFlow(f));
      if (b.dataset.act === "convert") b.addEventListener("click", () => convertFlow(f));
      if (b.dataset.act === "formula") b.addEventListener("click", () => formulaFlow(f));
      if (b.dataset.act === "del") b.addEventListener("click", () => { void deleteFlow(f); });
    });
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
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
    UI.ok(`字段已改名为「${name}」`);
    loadFields();
  } catch (e) { UI.apiFail(e, "改名失败"); }
}

function deleteFlow(f) {
  return DeleteFlow.run({
    title: `删除字段「${f.name}」`,
    bodyHtml: `<p>将删除该字段（<b>${typeText(f)}</b>）并<b style="color:var(--danger)">级联清空所有单品的该字段值</b>，操作不可恢复。</p>`,
    path: `/fields/${f.id}?confirm=true`,
    okText: (res) => `字段「${res.field.name}」已删除，连带清除 ${res.deleted_values} 个字段值`,
    onDone: loadFields,
  });
}

// ---------- 计算字段：公式编辑器小窗（新建与编辑共用） ----------
// field 为 null 时是新建：名称初始取外侧表单已填写的名称（可在窗内修改），公式、结果类型、
// 显示位数、允许负数一次性提交。保存失败一律保留当前输入（名称、公式 token、常量输入框、
// 结果格式），只更新错误区域，不重建编辑器、不关闭对话框。
function formulaDialog({ field = null, initialName = "", onSaved }) {
  const creating = field === null;
  const others = fieldCache.filter((f) => !f.system && f.uid !== (field && field.uid));
  const initials = field && field.formula ? field.formula.tokens : [];

  const wrap = document.createElement("div");
  wrap.className = "dlg-form dlg-form-wide dlg-form-formula";
  wrap.innerHTML = `
    <div class="form-grid" style="grid-template-columns:1fr 1fr">
      <div>
        <label>字段名称 <span class="fieldreq">*</span></label>
        <input data-fx-name maxlength="50" value="${UI.esc(field ? field.name : initialName)}"
               placeholder="如：毛利 / 毛利率" autocomplete="off">
      </div>
      <div>
        <label>计算结果类型</label>
        <select data-fx-type>
          <option value="int">整数</option>
          <option value="decimal">小数</option>
          <option value="percent">百分比（显示时带 % 号）</option>
        </select>
      </div>
      <div data-fx-scale-box>
        <label>显示小数位（0~6，默认 2）</label>
        <input data-fx-scale type="number" min="0" max="6" value="2">
      </div>
    </div>
    <div class="hint" data-fx-scale-hint hidden>整数结果按四舍五入取整，其他公式引用时使用取整后的值。</div>
    <div class="form-check" style="margin-top:.8rem">
      <input data-fx-neg type="checkbox"><label style="margin:0">允许结果为负数</label>
    </div>
    <div class="hint" style="margin:.7rem 0 .2rem">公式（点击下方按钮依次加入；保存时统一校验语法、引用与循环）</div>
    <div data-fx-editor></div>
    <div data-fx-result></div>`;
  const { dlg, foot } = UI.baseDialog(creating ? "新建数学计算字段" : `编辑公式「${field.name}」`, wrap);
  dlg.classList.add("dlg-formula");

  // 结果格式控件：编辑时回填当前定义；显示位数对小数与百分比都生效
  const nameEl = wrap.querySelector("[data-fx-name]");
  const typeEl = wrap.querySelector("[data-fx-type]");
  const scaleEl = wrap.querySelector("[data-fx-scale]");
  const negEl = wrap.querySelector("[data-fx-neg]");
  const scaleBox = wrap.querySelector("[data-fx-scale-box]");
  const scaleHint = wrap.querySelector("[data-fx-scale-hint]");
  const isInt = () => typeEl.value === "int";
  typeEl.value = field ? field.type : "decimal";
  // 小数与百分比回填当前显示位数；整数没有小数位，回填「切回其它类型时使用的」默认 2 位
  scaleEl.value = String(field && field.type !== "int" ? field.scale : 2);
  negEl.checked = !!(field && field.allow_negative);

  // 整数结果没有小数位：收起并禁用输入框；不销毁其内容，切回小数/百分比即恢复原值
  function syncScale() {
    scaleBox.hidden = isInt();
    scaleHint.hidden = !isInt();
    scaleEl.disabled = isInt();
  }
  typeEl.addEventListener("change", syncScale);
  syncScale();

  const editor = FormulaEditor.create({
    fields: others, excludeUid: field && field.uid,
    tokens: initials, mount: wrap.querySelector("[data-fx-editor]"),
  });

  const resultBox = wrap.querySelector("[data-fx-result]");
  const cancel = UI.mkBtn("取消", "btn-outline");
  const save = UI.mkBtn(creating ? "创建" : "保存公式");
  cancel.type = save.type = "button";
  foot.append(cancel, save);
  cancel.onclick = () => dlg.close();
  dlg.addEventListener("close", () => dlg.remove());

  save.onclick = () => UI.withBusy(save, async () => {
    resultBox.className = "";
    resultBox.innerHTML = "";
    const name = nameEl.value.trim();
    if (creating && !name) {
      resultBox.innerHTML = UI.warnHtml("请填写字段名称", "字段名称用于在单品表与公式中标识该字段。");
      nameEl.focus();
      return;
    }
    if (editor.isEmpty()) {
      resultBox.innerHTML = UI.warnHtml("公式还是空的",
        "请加入字段、系统字段、运算符与数字常量，例如：售价 × 库存数量 − 成本。");
      return;
    }
    const scale = scaleEl.value === "" ? 2 : Number(scaleEl.value);
    // 整数结果没有小数位：提交 type=int, scale=0，并跳过小数位校验
    if (!isInt() && (!Number.isInteger(scale) || scale < 0 || scale > 6)) {
      resultBox.innerHTML = UI.warnHtml("显示小数位必须是 0~6 的整数");
      return;
    }
    const tokens = editor.getTokens();
    const type = typeEl.value;
    try {
      const out = creating
        ? await XM.post("/fields", {
            name, type, scale: type === "int" ? 0 : scale, allow_negative: negEl.checked,
            formula: { version: 1, tokens },
          })
        : await XM.put(`/fields/${field.id}/formula`, {
            formula: { version: 1, tokens },
            type, scale: type === "int" ? 0 : scale, allow_negative: negEl.checked,
          });
      UI.ok(creating
        ? `计算字段「${out.name}」已创建（${typeText(out)}）`
        : `字段「${out.name}」的公式已更新（${typeText(out)}）`);
      dlg.close();
      if (onSaved) onSaved(out);
    } catch (e) {
      // 校验失败：保留名称、公式与结果格式，只显示错误，对话框不关闭
      // 展示用户版文案；e.code/e.hint 是给 Agent 与日志用的，不进界面
      if (e.code) console.warn("[fields] 公式保存失败", e.code, e.message);
      resultBox.className = "result-box error-box";
      resultBox.innerHTML =
        `<div class="headline"><span class="badge red">${
          UI.esc(UI.codeTitle(e.code) || "公式有误")}</span> ${UI.esc(e.display || e.message)}</div>` +
        (e.displayHint ? `<div class="hint">${UI.esc(e.displayHint)}</div>` : "") +
        `<div class="hint">当前填写内容已保留，可直接修改公式后再次保存。</div>`;
    }
  });

  dlg.showModal();
  setTimeout(() => { if (creating) nameEl.focus(); }, 50);
}

// 列表里的「编辑公式」
function formulaFlow(f) {
  formulaDialog({ field: f, onSaved: () => loadFields() });
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
  if (cleared) bits.push(`<span class="badge red">将清空全部值</span>`);
  else if (res.lossy) bits.push(`<span class="badge red">精度会变化</span>`);
  else if (res.converted || res.dry_run) bits.push(`<span class="badge green">数值不变</span>`);

  let html = `<div class="result-box"><div class="headline">${bits.join(" ")}</div>`;
  if (res.description_text) {
    html += `<div style="margin-top:.4rem">换算方式：${UI.esc(res.description_text)}</div>`;
  }
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
  const scaleBox = $("#fldScaleBox"), negBox = $("#fldNegBox"), computedHint = $("#fldComputedHint");
  const createBtn = $("#fCreateBtn");
  const isComputed = () => typeSel.value === COMPUTED_VALUE;

  // 计算字段用「打开公式编辑器」小窗创建（那里有公式面板与校验反馈），表单里不再重复一份；
  // 入口按钮只负责打开编辑器，不创建字段，外侧已填写的名称会带入窗内
  function openCreateComputed() {
    if (!fieldCache.some((f) => !f.system)) {
      UI.toast("该店铺还没有可引用的数值字段；公式仍可用数字常量与库存数量组成");
    }
    formulaDialog({
      field: null,
      initialName: $("#fldName").value.trim(),
      onSaved: () => {
        // 创建成功后清空外侧名称；取消或保存失败都不动它
        $("#fldName").value = "";
        loadFields();
      },
    });
  }

  const syncType = () => {
    const t = typeSel.value;
    const computed = isComputed();
    // 计算字段的名称、公式与结果格式都在小窗里填，表单里对应的控件一并收起；
    // 「创建」按钮随后打开公式小窗，因此表单自身不再做「必须填名称」的拦截
    scaleBox.hidden = computed;
    negBox.hidden = computed;
    computedHint.hidden = !computed;
    // 入口按钮的文案随类型切换：数学计算是「打开公式编辑器」，其余仍是「创建」
    createBtn.textContent = computed ? "打开公式编辑器" : "创建";
    scaleInp.disabled = t !== "decimal";
    negChk.disabled = t === "string" || t === "bool";
    if (negChk.disabled) negChk.checked = false;
  };
  typeSel.addEventListener("change", syncType);
  syncType();

  $("#fCreate").addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (isComputed()) { openCreateComputed(); return; }
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
          html: UI.okHtml(`「${f.name}」（${typeText(f)}）已创建`),
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
