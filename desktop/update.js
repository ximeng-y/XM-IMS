"use strict";
/* 桌面版程序更新：匿名查询本项目 GitHub 正式 Release，下载并交接给安装程序。

   检查与安装都只在本进程内完成，renderer 只能拿状态、只能请求安装「已确认的 tag」。
   安装资产的完整性直接使用 GitHub 提供的 sha256 digest，下载时用 node:crypto 边写边算，
   不一致不进入安装阶段。 */
const crypto = require("node:crypto");
const fs = require("node:fs");
const https = require("node:https");
const path = require("node:path");
const { spawn } = require("node:child_process");
const paths = require("./paths");

const REPOSITORY = "ximeng-y/XM-IMS";
const USER_AGENT = "XM-IMS-Desktop";
const API_LATEST = `https://api.github.com/repos/${REPOSITORY}/releases/latest`;
const ASSET_PATTERN = /^XM-IMS-(\d+\.\d+\.\d+)-win-x64-Setup\.exe$/;
const VERSION_PATTERN = /^v?(\d+)\.(\d+)\.(\d+)$/;
const MAX_REDIRECTS = 5;

let currentVersion = "0.0.0";
let confirmed = null;   // {tag, version, assetName, assetId, digest}
// 检查结果缓存：页面每次完整加载都会自动检查，而 GitHub 匿名 REST 配额按出口 IP
// 每小时 60 次；只有用户手动检查（force）才跳过缓存。
let cached = null;      // {state, at}
const CACHE_TTL = 10 * 60 * 1000;
const ERROR_TTL = 60 * 1000;

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

    force 为 false 时（页面自动检查、安装前的再次确认）优先复用未过期的结果。 */
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
  const asset = (release.assets || []).find(item => {
    const match = ASSET_PATTERN.exec(String(item && item.name));
    return match && match[1] === latest;
  });
  const state = baseState({
    latest_version: latest,
    latest_tag: tag,
    has_update: compare(version, parseVersion(currentVersion) || [0, 0, 0]) > 0,
  });
  if (!asset) {
    state.availability = "no_asset";
    return state;
  }
  // 安装只使用本次检查确认的目标：下载地址由 tag 与文件名重新拼出，不接受 renderer 传入。
  state.availability = "available";
  confirmed = {
    tag,
    version: latest,
    assetName: asset.name,
    digest: typeof asset.digest === "string" ? asset.digest : "",
  };
  return state;
}

function fail(message) {
  return { ok: false, user_message: message };
}

/** 下载并校验已确认的安装包；只有校验通过才返回可安装的本地路径。 */
async function download() {
  const target = confirmed;
  if (!target) return fail("暂时无法确认要安装的版本，请重新检查更新");
  const digest = /^sha256:([0-9a-f]{64})$/i.exec(target.digest);
  if (!digest) return fail("该版本缺少完整性信息，已停止安装");
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
      return fail("下载安装包失败，请检查网络后重试");
    }
    const hash = crypto.createHash("sha256");
    const output = fs.createWriteStream(partPath);
    await new Promise((resolve, reject) => {
      response.on("data", chunk => hash.update(chunk));
      response.on("error", reject);
      output.on("error", reject);
      output.on("finish", resolve);
      response.pipe(output);
    });
    if (hash.digest("hex").toLowerCase() !== digest[1].toLowerCase()) {
      fs.rmSync(partPath, { force: true });
      return fail("下载的安装包校验失败，已停止安装");
    }
    fs.renameSync(partPath, finalPath);
    return { ok: true, path: finalPath };
  } catch (_) {
    try { fs.rmSync(partPath, { force: true }); } catch (__) { /* 忽略 */ }
    return fail("下载安装包失败，请检查网络后重试");
  }
}

function assetDownloadUrl(target) {
  return `https://github.com/${REPOSITORY}/releases/download/${encodeURIComponent(target.tag)}/${encodeURIComponent(target.assetName)}`;
}

/** 交接给安装程序：等待当前 Electron 退出后再替换程序文件。

    安装目录通过环境变量传递：NSIS 的内置 /D= 必须位于最后且会吞掉其后所有参数，
    路径含空格时还会被整体加引号，两者都与自定义参数冲突。/XM_PID 让安装器等待
    本进程退出后再覆盖程序文件。 */
function launchInstaller(installerPath) {
  return spawn(installerPath, ["/S", `/XM_PID=${process.pid}`], {
    detached: true,
    stdio: "ignore",
    windowsHide: false,
    env: { ...process.env, XM_UPDATE_DIR: paths.installRoot },
  });
}

function reset() {
  confirmed = null;
  cached = null;
}

module.exports = { setCurrentVersion, check, download, launchInstaller, reset, parseVersion, compare };
