"use strict";
/* 桌面版程序更新：匿名查询本项目 GitHub 正式 Release，下载完整版本 ZIP，
   在后台解压为新的版本槽，准备完成后由用户决定何时重启应用。

   - 检查与准备都只在本进程内完成，renderer 只能拿状态、只能请求安装「已确认的 tag」
   - 安装资产的完整性直接使用 GitHub 提供的 sha256 digest，下载时用 node:crypto
     边写边算，不一致不进入解压阶段
   - 下载与解压不停止服务、不替换当前程序、不改业务数据；进度只来自实际字节
   - 准备结果写入版本选择资料；renderer 通过快照与订阅拿到只读进度 */
const crypto = require("node:crypto");
const fs = require("node:fs");
const https = require("node:https");
const path = require("node:path");
const { spawn } = require("node:child_process");
const paths = require("./paths");
const store = require("./version-store");

const REPOSITORY = "ximeng-y/XM-IMS";
const USER_AGENT = "XM-IMS-Desktop";
const API_LATEST = `https://api.github.com/repos/${REPOSITORY}/releases/latest`;
const ASSET_PATTERN = /^XM-IMS-(\d+\.\d+\.\d+)-win-x64\.zip$/;
const VERSION_PATTERN = /^v?(\d+)\.(\d+)\.(\d+)$/;
const MAX_REDIRECTS = 5;
const PREPARE_TIMEOUT = 30 * 60 * 1000;

let currentVersion = "0.0.0";
let confirmed = null;   // {tag, version, assetName, assetSize, digest}
// 检查结果缓存：页面每次完整加载都会自动检查，而 GitHub 匿名 REST 配额按出口 IP
// 每小时 60 次；只有用户手动检查（force）才跳过缓存。
let cached = null;      // {state, at}
const CACHE_TTL = 10 * 60 * 1000;
const ERROR_TTL = 60 * 1000;

// 权威进度快照：renderer 只读，主进程是唯一写入者
let revision = 0;
let job = null;         // {id, targetVersion, stage, ...}
let lastStage = "idle"; // 作业结束后的最终阶段：prepared / failed
let lastFailure = "";
let fallbackNotice = null;
const listeners = new Set();

function setCurrentVersion(version) {
  currentVersion = String(version);
}

function parseVersion(value) {
  const match = VERSION_PATTERN.exec(String(value || ""));
  return match ? match.slice(1).map(Number) : null;
}

function compare(left, right) {
  for (let i = 0; i < 3; i += 1) {
    if (left[i] !== right[i]) return left[i] - right[i];
  }
  return 0;
}

function emptySnapshot(extra) {
  return {
    revision: 0,
    current_version: currentVersion,
    target_version: null,
    target_tag: null,
    stage: "idle",
    download_done: 0,
    download_total: null,
    install_done: 0,
    install_total: null,
    can_restart: false,
    preparing: false,
    job_id: null,
    last_error: "",
    fallback: null,
    ...extra,
  };
}

function snapshot() {
  const prepared = store.preparedCandidate();
  // 作业结束后仍保留最终阶段；正常启动前 stage 一直停留在这里
  const stage = job ? job.stage : (prepared ? "prepared" : lastStage);
  return emptySnapshot({
    revision,
    stage,
    preparing: !!job,
    // 只有没有进行中的作业、且选择资料确实有待应用候选时才允许重启
    can_restart: !job && stage === "prepared" && !!prepared,
    target_version: job ? job.targetVersion : (prepared ? prepared.version : null),
    target_tag: job ? job.targetTag : (prepared ? prepared.tag : null),
    last_error: lastFailure,
    fallback: fallbackNotice,
    job_id: job ? job.id : null,
    download_done: job ? job.downloadDone : 0,
    download_total: job ? job.downloadTotal : null,
    install_done: job ? job.installDone : 0,
    install_total: job ? job.installTotal : null,
  });
}

function publish(patch) {
  if (job && patch) Object.assign(job, patch);
  revision += 1;
  const value = snapshot();
  for (const callback of [...listeners]) {
    try { callback(value); } catch (_) { /* 单个订阅回调失败不影响其他订阅 */ }
  }
}
function subscribe(callback) {
  if (typeof callback !== "function") return () => {};
  listeners.add(callback);
  return () => listeners.delete(callback);
}

/** 启动时读取一次回退结果；旧版重开后只展示一次。 */
function takeFallbackNotice() {
  fallbackNotice = store.consumeFallbackNotice();
  if (fallbackNotice) revision += 1;
  return fallbackNotice;
}

/** 只接受 https 与有限的同站重定向；返回 response 与最终地址。 */
function open(url, redirects = 0) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, {
      headers: { "User-Agent": USER_AGENT, Accept: "application/vnd.github+json" },
      timeout: 30000,
    }, response => {
      if (response.statusCode >= 300 && response.statusCode < 400 && response.headers.location) {
        response.resume();
        const next = new URL(response.headers.location, url);
        if (redirects >= MAX_REDIRECTS || next.protocol !== "https:") {
          reject(new Error("更新下载地址无法验证"));
          return;
        }
        resolve(open(next.toString(), redirects + 1));
        return;
      }
      resolve({ response, url });
    });
    request.on("timeout", () => request.destroy(new Error("网络请求超时")));
    request.on("error", reject);
  });
}

async function requestJson(url) {
  const { response } = await open(url);
  if (response.statusCode === 404) {
    response.resume();
    return { missing: true };
  }
  if (response.statusCode !== 200) {
    response.resume();
    throw new Error(`HTTP ${response.statusCode}`);
  }
  let body = "";
  response.setEncoding("utf8");
  for await (const chunk of response) body += chunk;
  return { data: JSON.parse(body) };
}

function baseState(extra) {
  return {
    checked: true,
    current_version: currentVersion,
    latest_version: null,
    latest_tag: null,
    has_update: false,
    availability: "no_release",
    error: null,
    ...extra,
  };
}

/** 查询最新正式 Release；所有失败都折算成可展示的状态，不抛给 renderer。

    force 为 false 时（页面自动检查）优先复用未过期的结果。 */
async function check(force = false) {
  if (!force && cached) {
    const ttl = cached.state.availability === "error" ? ERROR_TTL : CACHE_TTL;
    if (Date.now() - cached.at < ttl) return cached.state;
  }
  const state = await fetchLatest();
  cached = { state, at: Date.now() };
  return state;
}

async function fetchLatest() {
  let result;
  try {
    result = await requestJson(API_LATEST);
  } catch (error) {
    return baseState({ checked: true, availability: "error", error: "网络不可用，暂时无法检查更新" });
  }
  if (result.missing) {
    return baseState({ checked: true, availability: "no_release" });
  }
  const release = result.data || {};
  const tag = typeof release.tag_name === "string" ? release.tag_name : "";
  const version = parseVersion(tag);
  if (!version) {
    return baseState({ checked: true, availability: "no_release" });
  }
  const latest = version.join(".");
  const assets = release.assets || [];
  const asset = assets.find(item => {
    const match = ASSET_PATTERN.exec(String(item && item.name));
    return match && match[1] === latest;
  });
  const state = baseState({
    latest_version: latest,
    latest_tag: tag,
    has_update: compare(version, parseVersion(currentVersion) || [0, 0, 0]) > 0,
  });
  if (!asset) {
    // 只有旧版 Setup 资产时不再回到覆盖安装，按暂无可安装更新处理
    state.availability = "no_asset";
    return state;
  }
  // 准备只使用本次检查确认的目标：下载地址由 tag 与文件名重新拼出，不接受 renderer 传入。
  state.availability = "available";
  confirmed = {
    tag,
    version: latest,
    assetName: asset.name,
    assetSize: Number.isFinite(asset.size) && asset.size > 0 ? asset.size : null,
    digest: typeof asset.digest === "string" ? asset.digest : "",
  };
  return state;
}

function fail(message) {
  return { ok: false, user_message: message };
}

/** 下载并校验已确认的更新包；只有校验通过才返回本地路径。 */
async function download(target, onProgress) {
  const digest = /^sha256:([0-9a-f]{64})$/i.exec(target.digest);
  if (!digest) return fail("该版本缺少完整性信息，已停止更新");
  const folder = paths.updateStaging(target.version);
  const finalPath = path.join(folder, target.assetName);
  const partPath = finalPath + ".part";
  try {
    fs.mkdirSync(folder, { recursive: true });
    fs.rmSync(partPath, { force: true });
  } catch (_) {
    return fail("无法写入更新目录，请检查磁盘空间后重试");
  }
  try {
    const { response } = await open(assetDownloadUrl(target));
    if (response.statusCode !== 200) {
      response.resume();
      return fail("下载更新包失败，请检查网络后重试");
    }
    const length = Number(response.headers["content-length"]);
    const total = target.assetSize || (Number.isFinite(length) && length > 0 ? length : null);
    onProgress(0, total);
    const hash = crypto.createHash("sha256");
    const output = fs.createWriteStream(partPath);
    let received = 0;
    await new Promise((resolve, reject) => {
      response.on("data", chunk => {
        hash.update(chunk);
        received += chunk.length;
        onProgress(received, total);
      });
      response.on("error", reject);
      output.on("error", reject);
      output.on("finish", resolve);
      response.pipe(output);
    });
    if (hash.digest("hex").toLowerCase() !== digest[1].toLowerCase()) {
      fs.rmSync(partPath, { force: true });
      return fail("下载的更新包校验失败，已停止更新");
    }
    fs.renameSync(partPath, finalPath);
    return { ok: true, path: finalPath };
  } catch (_) {
    try { fs.rmSync(partPath, { force: true }); } catch (__) { /* 忽略 */ }
    return fail("下载更新包失败，请检查网络后重试");
  }
}

function assetDownloadUrl(target) {
  return `https://github.com/${REPOSITORY}/releases/download/${encodeURIComponent(target.tag)}/${encodeURIComponent(target.assetName)}`;
}

/** 用当前稳定版的嵌入式 Python 解压；隐藏窗口，只按 stdout 的实际字节更新进度。 */
function extract(target, archivePath, slot) {
  return new Promise(resolve => {
    if (!fs.existsSync(paths.pythonExe) || !fs.existsSync(paths.updateExtract)) {
      resolve(fail("程序文件不完整，请重新安装 XM-IMS"));
      return;
    }
    const staging = path.join(paths.updateStaging(target.version), "staging-" + slot);
    const child = spawn(paths.pythonExe, [
      "-B", paths.updateExtract,
      "--zip", archivePath,
      "--target", staging,
      "--slot", slot,
      "--slot-root", paths.versionsRoot,
    ], { cwd: paths.backendRoot, windowsHide: true, stdio: ["ignore", "pipe", "pipe"] });
    let buffered = "";
    let settled = false;
    const finish = result => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const timer = setTimeout(() => {
      try { child.kill(); } catch (_) { /* 已退出 */ }
      finish(fail("安装准备超时，请检查磁盘空间后重试"));
    }, PREPARE_TIMEOUT);
    child.stderr.resume();
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", chunk => {
      buffered += chunk;
      let index;
      while ((index = buffered.indexOf("\n")) >= 0) {
        const line = buffered.slice(0, index).trim();
        buffered = buffered.slice(index + 1);
        const parts = line.split(" ");
        if (parts[0] !== "XM-IMS-EXTRACT") continue;
        if (parts[1] === "extracting" || parts[1] === "checking") {
          publish({
            stage: "installing",
            installDone: Number(parts[2]) || 0,
            installTotal: Number(parts[3]) || null,
          });
        }
      }
    });
    child.once("error", () => finish(fail("无法启动安装准备，请稍后重试")));
    child.once("exit", code => {
      if (code === 0) finish({ ok: true, slot });
      else finish(fail("安装准备失败，请检查磁盘空间后重试"));
    });
  });
}

function buildSlot(version) {
  // 每次准备产生唯一槽名：同一版本重复准备或便携版重复构建不会混合覆盖
  return `${version}-${crypto.randomBytes(16).toString("hex")}`;
}

/** 准备作业：下载 → 校验 → 解压 → 保存 prepared。返回接受结果，完成由快照表达。 */
async function prepare(tag) {
  if (job) return { ok: false, user_message: "更新正在准备中，请稍候" };
  const prepared = store.preparedCandidate();
  if (prepared) {
    return { ok: true, accepted: true, existing: true, version: prepared.version,
             user_message: "新版本已准备就绪，可重启应用更新" };
  }
  // 先占住作业槽再等远端检查：检查期间再次请求会看到进行中的作业，
  // 不会与本次并发下载到同一个临时文件
  job = {
    id: crypto.randomBytes(8).toString("hex"),
    targetVersion: null,
    targetTag: tag,
    stage: "checking",
    downloadDone: 0,
    downloadTotal: null,
    installDone: 0,
    installTotal: null,
  };
  lastFailure = "";
  lastStage = "checking";
  publish({});
  try {
    const state = await check(true);
    if (!state || state.availability !== "available" || state.latest_tag !== tag) {
      return finishJob("暂时无法确认这个版本，请重新检查更新");
    }
    const target = confirmed;
    if (!target) return finishJob("暂时无法确认要安装的版本，请重新检查更新");
    try {
      recordUpdateRequest(tag);
    } catch (_) {
      return finishJob("无法保存更新记录，请确认数据目录可写后重试");
    }
    Object.assign(job, {
      targetVersion: target.version,
      targetTag: target.tag,
      stage: "downloading",
      downloadTotal: target.assetSize,
    });
    lastStage = "downloading";
    publish({});
    runPrepareJob(target, buildSlot(target.version));
    return { ok: true, accepted: true, version: target.version,
             user_message: "正在准备更新，可以继续使用，完成后会提示重启" };
  } catch (error) {
    return finishJob(error && error.message ? error.message : "更新准备失败，请稍后重试");
  }
}

/** 后台作业：主进程持有 Promise，权威快照在结束或失败时更新。 */
async function runPrepareJob(target, slot) {
  const downloaded = await download(target, (done, total) => {
    publish({ stage: "downloading", downloadDone: done, downloadTotal: total });
  });
  if (!downloaded.ok) return finishJob(downloaded.user_message);
  publish({ stage: "installing", downloadDone: target.assetSize || job.downloadDone,
            downloadTotal: target.assetSize || job.downloadTotal,
            installDone: 0, installTotal: null });
  const extracted = await extract(target, downloaded.path, slot);
  if (!extracted.ok) return finishJob(extracted.user_message);
  let failure = "";
  try {
    const saved = store.savePreparedCandidate({ slot, version: target.version, tag: target.tag });
    if (!saved.ok) failure = "程序状态已经变化，请稍后重新检查更新";
  } catch (_) {
    failure = "无法保存更新准备结果，请确认数据目录可写后重试";
  }
  finishJob(failure);
}

/** 结束本次作业：清空作业槽并发布最终阶段；失败文案保留在快照里。 */
function finishJob(failure) {
  job = null;
  lastFailure = failure || "";
  lastStage = failure ? "failed" : "prepared";
  publish({});
  return failure ? fail(lastFailure) : { ok: true };
}

/** 用户请求重启：只使用已准备目标，不停留中间状态。 */
function requestRestart() {
  const prepared = store.preparedCandidate();
  if (!prepared) return { ok: false, user_message: "没有待重启的新版本" };
  const marked = store.markPending();
  if (!marked.ok) return marked;
  return { ok: true, version: prepared.version };
}

function reset() {
  confirmed = null;
  cached = null;
}

/** 更新记录：只保存最近一次被接受的更新请求，位于数据根目录的更新资料区。

    写入先落到同目录临时文件，刷新关闭后再替换正式文件，失败时保留原记录。 */
function readUpdateRecord() {
  let text;
  try {
    text = fs.readFileSync(paths.updateRecord, "utf8");
  } catch (_) {
    return null;   // 缺失或不可读：没有记录可处理
  }
  try {
    const record = JSON.parse(text);
    return record && typeof record === "object" && !Array.isArray(record) ? record : null;
  } catch (_) {
    return null;
  }
}

function writeUpdateRecord(record) {
  const folder = path.dirname(paths.updateRecord);
  const temporary = paths.updateRecord + ".writing";
  fs.mkdirSync(folder, { recursive: true });
  const fd = fs.openSync(temporary, "w");
  try {
    fs.writeFileSync(fd, JSON.stringify(record, null, 2) + "\n", "utf8");
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(temporary, paths.updateRecord);
}

/** 保存本次已确认目标的接受记录；失败时抛出，由调用方停止更新。 */
function recordUpdateRequest(tag) {
  const target = confirmed;
  if (!target || target.tag !== tag) {
    throw new Error("要安装的版本不是已确认的最新版本");
  }
  const from = (parseVersion(currentVersion) || []).join(".");
  if (!from) {
    throw new Error("当前版本无法确认");
  }
  writeUpdateRecord({
    from_version: from,
    to_version: target.version,
    confirmed_at: new Date().toISOString(),
    status: "accepted",
  });
}

/** 启动核对：新进程实际运行版本等于记录目标时，把记录状态改为安装完成。

    只处理已存在的接受记录；读取或写入问题不改坏记录，也不阻断启动。 */
function confirmInstalledUpdate() {
  const record = readUpdateRecord();
  if (!record || record.status !== "accepted") return;
  if (typeof record.from_version !== "string" || typeof record.to_version !== "string" ||
      typeof record.confirmed_at !== "string") {
    return;
  }
  if (record.to_version !== currentVersion) return;
  try {
    writeUpdateRecord({ ...record, status: "installed" });
  } catch (error) {
    console.warn("[update] 更新记录完成状态写入失败，保留已保存的接受记录");
  }
}

module.exports = {
  setCurrentVersion,
  check,
  prepare,
  requestRestart,
  reset,
  parseVersion,
  compare,
  recordUpdateRequest,
  confirmInstalledUpdate,
  publish,
  subscribe,
  snapshot,
  takeFallbackNotice,
};
