/* 共享组件：自定义属性值输入、变更收集与计算结果显示。
   只处理字段定义和值的呈现，不发起业务请求，也不按具体字段名分支。 */
"use strict";

import { UI } from "xm/ui";

const esc = UI.esc;
const isComputed = (field) => !!(field && field.computed);
const plainFields = (fields) => (fields || []).filter((field) => !isComputed(field));

function calcText(error) {
  return [error && (error.user_message || error.message),
          error && (error.user_hint || "")].filter(Boolean).join("；");
}

function calcHtml(calc) {
  if (!calc) return `<span class="muted">—</span>`;
  if (calc.error) {
    const tip = esc(calcText(calc.error));
    const label = UI.codeTitle(calc.error.code) || "无法计算";
    return `<span class="badge red" title="${tip}">${esc(label)}</span>`;
  }
  return `<span class="mono">${esc(calc.display)}</span>`;
}

function calcBlockHtml(calc) {
  return calc && calc.error
    ? `<div class="calc-block">${calcHtml(calc)}<span class="calc-msg">${
        esc(calcText(calc.error))}</span></div>`
    : calcHtml(calc);
}

function computedRowHtml(field, calc) {
  return `<div>
      <label>${esc(field.name)} <span class="badge gray">计算</span></label>
      <div class="calc-cell">${calcBlockHtml(calc)}</div>
    </div>`;
}

function valueText(field, values) {
  const value = values ? values[field.name] : undefined;
  if (value === null || value === undefined || value === "") return "—";
  if (field.type === "bool") return value ? "是" : "否";
  if (field.type === "decimal") return Number(value).toFixed(field.scale ?? 2);
  if (field.type === "percent") return `${value}%`;
  return String(value);
}

function fieldInputHtml(field, value, { withClear = false } = {}) {
  const name = esc(field.name);
  let input;
  if (field.type === "string") {
    input = `<input data-fld="${name}" type="text" value="${esc(value ?? "")}" autocomplete="off">`;
  } else if (field.type === "bool") {
    const shown = value === true ? "yes" : value === false ? "no" : "";
    input = `<select data-fld="${name}">
      <option value="" ${shown === "" ? "selected" : ""}>（不设置）</option>
      <option value="yes" ${shown === "yes" ? "selected" : ""}>是</option>
      <option value="no" ${shown === "no" ? "selected" : ""}>否</option>
    </select>`;
  } else if (field.type === "int") {
    input = `<input data-fld="${name}" type="number" step="1" ${
      field.allow_negative ? "" : 'min="0"'} value="${value ?? ""}">`;
  } else if (field.type === "decimal") {
    input = `<input data-fld="${name}" type="number" step="${
      Math.pow(10, -field.scale)}" ${field.allow_negative ? "" : 'min="0"'} value="${value ?? ""}">`;
  } else {
    input = `<input data-fld="${name}" type="number" step="0.01" ${
      field.allow_negative ? "" : 'min="0"'} value="${value ?? ""}">`;
  }
  const typeHint = field.type === "percent" ? "（百分数，如 25）" :
                   field.type === "decimal" ? `（${field.scale} 位小数）` : "";
  const clear = withClear
    ? `<span class="form-check" style="padding:0"><input type="checkbox" data-clear="${name}"><label style="margin:0;font-size:.78rem">清除</label></span>`
    : "";
  return `<div>
    <label>${name}${typeHint ? ` <span class="muted" style="font-size:.75rem">${typeHint}</span>` : ""}</label>
    <div style="display:flex;gap:.5rem;align-items:center">${input}${clear}</div>
  </div>`;
}

function readFieldInput(field, element) {
  const raw = element.value.trim();
  if (raw === "") return undefined;
  if (field.type === "bool") return element.value === "yes";
  if (field.type === "string") return raw;
  const number = Number(raw);
  return Number.isFinite(number) ? number : null;
}

function gridHtml(fields, values = {}, { withClear = false, emptyText = "" } = {}) {
  const plain = plainFields(fields);
  if (!plain.length) {
    return emptyText ? `<div class="hint">${esc(emptyText)}</div>` : "";
  }
  return plain.map((field) =>
    fieldInputHtml(field, values[field.name], { withClear })).join("");
}

function computedGridHtml(fields, calculations = {}) {
  const computed = (fields || []).filter(isComputed);
  return computed.map((field) => computedRowHtml(field, calculations[field.name])).join("");
}

function collectChanges(fields, root, currentValues = {}) {
  const values = {};
  let bad = false;
  const boolText = (value) => value === true ? "yes" : value === false ? "no" : "";
  plainFields(fields).forEach((field) => {
    const input = root.querySelector(`[data-fld="${CSS.escape(field.name)}"]`);
    const clear = root.querySelector(`[data-clear="${CSS.escape(field.name)}"]`);
    if (!input) return;
    if (clear && clear.checked) {
      values[field.name] = null;
      return;
    }
    const original = currentValues[field.name];
    const originalText = field.type === "bool"
      ? boolText(original) : String(original ?? "");
    const value = readFieldInput(field, input);
    if (field.type === "bool" && value === undefined) {
      if (typeof original === "boolean") values[field.name] = null;
      return;
    }
    if (value === null) {
      bad = true;
      return;
    }
    if (value !== undefined && input.value.trim() !== originalText) {
      values[field.name] = value;
    }
  });
  return { values, bad };
}

export const AttributeValuesForm = {
  isComputed,
  plainFields,
  calcText,
  calcHtml,
  calcBlockHtml,
  computedRowHtml,
  valueText,
  fieldInputHtml,
  readFieldInput,
  gridHtml,
  computedGridHtml,
  collectChanges,
};
