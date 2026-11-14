"use strict";
/* 桌面版本选择资料的具体操作：selection.ini、本次首次启动交接资料与只读快照。

   - 只写 ASCII 键和值；不保存绝对路径、中文或 JSON
   - 所有写入先落同目录临时文件、关闭后替换正式文件
   - 启动阶段（pending / starting / committing）只由根启动器推进；Node 只读
   - Electron 通过本模块的具体操作访问，不直接改内部对象 */
const fs = require("node:fs");
const path = require("node:path");
const paths = require("./paths");

const SECTION = "selection";
const ATTEMPT_SECTION = "request";
const CONTROL_SECTION = "control";
const STATUS_SECTION = "status";
const PHASES = new Set(["stable", "prepared", "pending", "starting", "committing"]);
const SLOT_PATTERN = /^(legacy|\d+\.\d+\.\d+-[0-9a-f]{32})$/;
const SAFE_VALUE = /^[\w.\-]*$/;
const STATUS_PHASES = new Set([
  "booting", "copying", "local_ready", "commit_ready", "data_committed", "failed",
]);

function validSlot(value) {
  return value === "" || SLOT_PATTERN.test(String(value));
}

/** 当前运行的版本槽名；便携版与旧平铺布局没有版本子目录，返回空串。 */
function currentSlot() {
  const parent = path.dirname(paths.runtimeRoot);
  if (path.basename(parent) !== "versions") return "";
  const name = path.basename(paths.runtimeRoot);
  return paths.slotPattern.test(name) ? name : "";
}

function readIni(file, section) {
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch (_) {
    return null;
  }
  const values = {};
  let active = false;
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith(";") || line.startsWith("#")) continue;
    if (line.startsWith("[") && line.endsWith("]")) {
      active = line.slice(1, -1) === section;
      continue;
    }
    if (!active) continue;
    const index = line.indexOf("=");
    if (index <= 0) continue;
    values[line.slice(0, index).trim()] = line.slice(index + 1).trim();
  }
  return values;
}

function writeIni(file, section, values) {
  for (const [key, value] of Object.entries(values)) {
    if (!/^[A-Za-z][A-Za-z0-9_]*$/.test(key) || !SAFE_VALUE.test(String(value))) {
      throw new Error("版本选择资料包含不允许的键或值");
    }
  }
  const text = "[" + section + "]\r\n" +
    Object.entries(values).map(([key, value]) => `${key}=${value}`).join("\r\n") + "\r\n";
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = file + ".writing";
  const fd = fs.openSync(temporary, "w");
  try {
    fs.writeFileSync(fd, text, "ascii");
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(temporary, file);
}

const EMPTY_SELECTION = {
  protocol: "1", phase: "stable", active: "", previous: "", candidate: "",
  candidate_version: "", candidate_tag: "", attempt: "",
  fallback_error: "", fallback_target: "", fallback_version: "",
};

/** 读取程序选择资料；缺失、协议不符或字段非法时返回 null。 */
function readSelection() {
  const values = readIni(paths.selectionFile, SECTION);
  if (!values || values.protocol !== "1" || !PHASES.has(values.phase)) return null;
  for (const key of ["active", "previous", "candidate"]) {
    if (!validSlot(values[key] || "")) return null;
  }
  return { ...EMPTY_SELECTION, ...values };
}

function saveSelection(selection) {
  const values = {};
  for (const key of Object.keys(EMPTY_SELECTION)) values[key] = selection[key] || "";
  writeIni(paths.selectionFile, SECTION, values);
  return { ...EMPTY_SELECTION, ...values };
}

/** 保存准备结果；已有其它待重启候选时不替换，返回现有目标。 */
function savePreparedCandidate({ slot, version, tag }) {
  if (!validSlot(slot) || !slot) throw new Error("版本槽名不合法");
  const current = readSelection();
  if (!current) throw new Error("程序选择资料不可读");
  if (current.phase === "prepared" && current.candidate && current.candidate !== slot) {
    return { ok: true, existing: true, candidate: current.candidate,
             version: current.candidate_version, tag: current.candidate_tag };
  }
  if (current.phase !== "stable" && current.phase !== "prepared") {
    return { ok: false, reason: current.phase };
  }
  saveSelection({ ...current, phase: "prepared", candidate: slot,
                  candidate_version: version, candidate_tag: tag });
  return { ok: true, existing: false, candidate: slot, version, tag };
}
/** 用户请求重启：进入 pending，之后不再接受其他更新操作。 */
function markPending() {
  const current = readSelection();
  if (!current || current.phase !== "prepared" || !current.candidate) {
    return { ok: false, user_message: "没有待重启的新版本" };
  }
  saveSelection({ ...current, phase: "pending" });
  return { ok: true, version: current.candidate_version };
}

/** 当前待应用候选（用于普通启动恢复展示）；没有则为 null。 */
function preparedCandidate() {
  const current = readSelection();
  if (!current || !current.candidate || current.phase === "stable") return null;
  if (current.phase !== "prepared") return null;
  return { slot: current.candidate, version: current.candidate_version, tag: current.candidate_tag };
}

/** 最近一次自动回退提示；读取后清空，避免每次启动重复展示。 */
function consumeFallbackNotice() {
  const current = readSelection();
  if (!current || !current.fallback_target) return null;
  const notice = {
    target: current.fallback_target,
    version: current.fallback_version,
    error: current.fallback_error,
  };
  saveSelection({ ...current, fallback_target: "", fallback_version: "", fallback_error: "" });
  return notice;
}

/** 本次首次启动资料；request.ini 与本次标识不一致时返回 null。 */
function readAttempt(attempt) {
  const id = String(attempt || "");
  if (!/^[0-9a-z]+$/.test(id)) return null;
  const directory = paths.attemptDir(id);
  const request = readIni(path.join(directory, "request.ini"), ATTEMPT_SECTION);
  if (!request || request.protocol !== "1" || request.attempt !== id) return null;
  if (!validSlot(request.to_slot || "")) return null;
  return {
    id,
    directory,
    fromSlot: request.from_slot || "",
    toSlot: request.to_slot || "",
    controlFile: path.join(directory, "control.ini"),
    statusFile: path.join(directory, "status.ini"),
    dataFile: path.join(directory, "data.json"),
  };
}

function readControl(attempt) {
  return readIni(attempt.controlFile, CONTROL_SECTION);
}

/** 候选进程写本次阶段；只允许固定阶段名与实际推进值。 */
function writeStatus(attempt, phase, extra = {}) {
  if (!STATUS_PHASES.has(phase)) throw new Error("启动阶段不合法");
  const values = { protocol: "1", attempt: attempt.id, phase };
  if (extra.fail) values.fail = extra.fail;
  if (Number.isFinite(extra.progress) && extra.progress >= 0) {
    values.progress = String(Math.floor(extra.progress));
    if (Number.isFinite(extra.total) && extra.total >= 0) {
      values.total = String(Math.floor(extra.total));
    }
  }
  writeIni(attempt.statusFile, STATUS_SECTION, values);
}

module.exports = {
  currentSlot,
  validSlot,
  readSelection,
  savePreparedCandidate,
  markPending,
  preparedCandidate,
  consumeFallbackNotice,
  readAttempt,
  readControl,
  writeStatus,
};
