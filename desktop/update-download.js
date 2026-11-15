"use strict";
/* 桌面更新包下载：先并行实测各线路对本次 ZIP 的传输速度，再按结果下载。

   - 版本、资产名、大小与摘要仍由更新编排模块从 GitHub 正式 API 确认
   - 三个候选只传输同一个官方资产；镜像不参与版本或完整性判断
   - 探测与正式下载都限制实际读取字节，不在内存中保存完整安装包
   - 每次换源都从头写入新的 .part，摘要重新计算，不拼接不同源的片段 */
const crypto = require("node:crypto");
const fs = require("node:fs");
const https = require("node:https");
const path = require("node:path");

const USER_AGENT = "XM-IMS-Desktop";
const PROBE_BYTES = 2 * 1024 * 1024;
const PROBE_TIMEOUT = 6 * 1000;
const CONNECT_TIMEOUT = 10 * 1000;
const IDLE_TIMEOUT = 10 * 1000;
const SWITCH_WINDOW = 10 * 1000;
const SPEED_SAMPLE = 250;
const MAX_REDIRECTS = 5;

const NETWORK_MESSAGE = "下载更新包失败，请检查网络后重试";
const WRITE_MESSAGE = "无法写入更新目录，请检查磁盘空间后重试";

function failure(message) {
  return { ok: false, user_message: message };
}

function monoMs() {
  return Number(process.hrtime.bigint()) / 1e6;
}

function positiveNumber(value) {
  return Number.isFinite(value) && value > 0 ? value : null;
}

function isRedirect(status) {
  return status >= 300 && status < 400;
}

function requestBinary(url, headers, signal, redirects = 0) {
  return new Promise((resolve, reject) => {
    let request;
    try {
      request = https.get(url, { headers, signal }, response => {
        if (isRedirect(response.statusCode) && response.headers.location) {
          let next;
          try {
            next = new URL(response.headers.location, url);
          } catch (_) {
            response.destroy();
            reject(new Error("更新下载地址无法验证"));
            return;
          }
          if (redirects >= MAX_REDIRECTS || next.protocol !== "https:") {
            response.destroy();
            reject(new Error("更新下载地址无法验证"));
            return;
          }
          response.resume();
          resolve(requestBinary(next.toString(), headers, signal, redirects + 1));
          return;
        }
        resolve({ response, url });
      });
    } catch (error) {
      reject(error);
      return;
    }
    request.on("error", reject);
  });
}

function fileHeaders(extra) {
  return {
    "User-Agent": USER_AGENT,
    Accept: "application/zip, application/octet-stream, */*",
    "Accept-Encoding": "identity",
    ...extra,
  };
}

function errorContentType(headers) {
  const type = String(headers["content-type"] || "").split(";", 1)[0].trim().toLowerCase();
  return type === "application/json" || type.startsWith("text/");
}

function parseContentRange(value) {
  const match = /^bytes\s+0-(\d+)\/(\d+|\*)$/i.exec(String(value || "").trim());
  if (!match) return null;
  const end = Number(match[1]);
  const total = match[2] === "*" ? null : Number(match[2]);
  if (!Number.isSafeInteger(end) || end < 0) return null;
  if (total !== null && (!Number.isSafeInteger(total) || total <= end)) return null;
  return { end, total };
}

function probeResponse(response) {
  if (response.statusCode === 200) return { ok: true, total: positiveNumber(Number(response.headers["content-length"])) };
  if (response.statusCode !== 206) return { ok: false };
  const range = parseContentRange(response.headers["content-range"]);
  if (!range) return { ok: false };
  return { ok: true, total: range.total };
}

function isZipPrefix(value) {
  if (!value || value.length < 4 || value[0] !== 0x50 || value[1] !== 0x4b) return false;
  return (value[2] === 0x03 && value[3] === 0x04) ||
    (value[2] === 0x05 && value[3] === 0x06) ||
    (value[2] === 0x07 && value[3] === 0x08);
}

function addPrefix(prefix, chunk) {
  if (prefix.length >= 4) return prefix;
  return Buffer.concat([prefix, chunk.subarray(0, 4 - prefix.length)]);
}

async function closeStream(stream) {
  if (!stream || stream.closed) {
    if (stream && !stream.destroyed) stream.destroy();
    return;
  }
  const closed = new Promise(resolve => stream.once("close", resolve));
  if (!stream.destroyed) stream.destroy();
  await closed;
}

async function writeChunk(file, chunk, position) {
  let offset = 0;
  while (offset < chunk.length) {
    const { bytesWritten } = await file.write(chunk, offset, chunk.length - offset, position + offset);
    if (!Number.isSafeInteger(bytesWritten) || bytesWritten <= 0) {
      throw new Error("更新包写入未取得进展");
    }
    offset += bytesWritten;
  }
  return position + chunk.length;
}

function candidates(originUrl) {
  let origin;
  try {
    origin = new URL(originUrl);
  } catch (_) {
    return [];
  }
  if (origin.protocol !== "https:") return [];
  return [
    { index: 0, name: "GitHub", url: origin.toString() },
    { index: 1, name: "ghproxy.net", url: "https://ghproxy.net/" + origin.toString() },
    { index: 2, name: "gh-proxy.org", url: "https://gh-proxy.org/" + origin.toString() },
  ];
}

function probeCandidate(candidate, expectedSize) {
  return new Promise(resolve => {
    const limit = expectedSize && expectedSize < PROBE_BYTES ? expectedSize : PROBE_BYTES;
    const startedHr = process.hrtime.bigint();
    const controller = new AbortController();
    let response = null;
    let settled = false;
    let bytes = 0;
    let prefix = Buffer.alloc(0);
    let total = expectedSize || null;

    const timer = setTimeout(() => {
      finish(true);
      controller.abort();
    }, PROBE_TIMEOUT);

    const finish = allowPartial => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const elapsed = Math.max(1, Number(process.hrtime.bigint() - startedHr) / 1e6);
      const valid = allowPartial && bytes > 0 && isZipPrefix(prefix);
      if (response) {
        response.destroy();
        if (response.req) response.req.destroy();
      }
      resolve(valid ? {
        ...candidate,
        speed: bytes / (elapsed / 1000),
        total: total || null,
      } : null);
    };

    requestBinary(candidate.url, fileHeaders({
      Range: `bytes=0-${Math.max(0, limit - 1)}`,
    }), controller.signal).then(({ response: value }) => {
      response = value;
      if (settled) {
        response.destroy();
        return;
      }
      const accepted = probeResponse(response);
      if (!accepted.ok || errorContentType(response.headers || {})) {
        finish(false);
        return;
      }
      total = expectedSize || accepted.total || null;
      response.on("data", chunk => {
        if (settled) return;
        const needed = Math.min(chunk.length, limit - bytes);
        if (needed <= 0) {
          finish(true);
          return;
        }
        prefix = addPrefix(prefix, chunk);
        if (prefix.length >= 4 && !isZipPrefix(prefix)) {
          finish(false);
          return;
        }
        bytes += needed;
        if (bytes >= limit) finish(true);
      });
      response.once("end", () => finish(true));
      response.once("error", () => finish(false));
      response.once("aborted", () => finish(false));
    }).catch(() => finish(false));
  });
}

function rankedCandidates(originUrl, expectedSize) {
  return Promise.all(candidates(originUrl).map(candidate => probeCandidate(candidate, expectedSize)))
    .then(results => results.filter(Boolean).sort((left, right) =>
      right.speed - left.speed || left.index - right.index));
}

function reportState(callback, value) {
  if (typeof callback !== "function") return;
  try {
    callback(value);
  } catch (_) {
    // 页面状态回调失败不能中断后台下载
  }
}

function bestRemaining(ranked, attempted) {
  return ranked.find(candidate => !attempted.has(candidate.index)) || null;
}

async function downloadCandidate(candidate, options) {
  const {
    partPath, expectedSize, expectedDigest, ranked, attempted, onState,
  } = options;
  const controller = new AbortController();
  const hash = crypto.createHash("sha256");
  let response = null;
  let outputFile = null;
  let writePosition = 0;
  let stopReason = "";
  let connectionTimer = null;
  let idleTimer = null;
  let windowTimer = null;
  let firstData = false;
  let prefix = Buffer.alloc(0);
  let prefixValid = false;
  let received = 0;
  let total = expectedSize || null;
  let responseLength = null;
  let sampleAt = monoMs();
  let sampleBytes = 0;
  let windowAt = 0;
  let windowBytes = 0;
  let lastPublish = 0;

  const clearTimers = () => {
    clearTimeout(connectionTimer);
    clearTimeout(idleTimer);
    clearTimeout(windowTimer);
    connectionTimer = null;
    idleTimer = null;
    windowTimer = null;
  };

  const closeOutput = async () => {
    if (!outputFile) return true;
    const file = outputFile;
    outputFile = null;
    try {
      await file.close();
      return true;
    } catch (_) {
      return false;
    }
  };

  const abort = reason => {
    if (!stopReason) stopReason = reason;
    controller.abort();
  };

  const publish = force => {
    const now = monoMs();
    if (!force && now - lastPublish < SPEED_SAMPLE) return;
    const elapsed = now - sampleAt;
    const speed = elapsed > 0 ? Math.max(0, (received - sampleBytes) / (elapsed / 1000)) : 0;
    sampleAt = now;
    sampleBytes = received;
    lastPublish = now;
    reportState(onState, {
      phase: "downloading",
      source: candidate.name,
      speed,
      done: received,
      total,
    });
  };

  const resetIdle = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => abort("idle"), IDLE_TIMEOUT);
  };

  const maybeSwitch = () => {
    windowTimer = null;
    if (stopReason || firstData === false) return;
    const elapsed = Math.max(1, monoMs() - windowAt);
    const currentSpeed = windowBytes / (elapsed / 1000);
    const remaining = bestRemaining(ranked, attempted);
    windowAt = monoMs();
    windowBytes = 0;
    windowTimer = setTimeout(maybeSwitch, SWITCH_WINDOW);
    if (!remaining || currentSpeed <= 0 || remaining.speed < currentSpeed * 2) return;
    if (total) {
      const remainingBytes = Math.max(0, total - received);
      const currentEta = remainingBytes / currentSpeed;
      const backupEta = total / remaining.speed;
      if (currentEta <= backupEta * 1.5) return;
    }
    abort("slow");
  };

  const onAbort = () => {
    if (response && !response.destroyed) response.destroy();
  };
  controller.signal.addEventListener("abort", onAbort, { once: true });

  try {
    connectionTimer = setTimeout(() => abort("connect"), CONNECT_TIMEOUT);
    const opened = await requestBinary(candidate.url, fileHeaders(), controller.signal);
    response = opened.response;
    if (response.statusCode !== 200 || errorContentType(response.headers || {})) {
      stopReason = "response";
      await closeStream(response);
      return { kind: "network" };
    }

    responseLength = positiveNumber(Number(response.headers["content-length"]));
    if (expectedSize && responseLength && expectedSize !== responseLength) {
      stopReason = "length";
      await closeStream(response);
      return { kind: "network" };
    }
    total = expectedSize || responseLength || null;
    if (total !== expectedSize) {
      reportState(onState, {
        phase: "downloading",
        source: candidate.name,
        speed: 0,
        done: 0,
        total,
      });
    }

    try {
      outputFile = await fs.promises.open(partPath, "w");
    } catch (_) {
      stopReason = "write";
      await closeStream(response);
      return { kind: "local" };
    }

    // 响应读取与文件写入分开处理，避免上游网络错误被流管线误报为本地写入失败。
    try {
      for await (const chunk of response) {
        if (!firstData) {
          firstData = true;
          clearTimeout(connectionTimer);
          sampleAt = monoMs();
          sampleBytes = 0;
          lastPublish = sampleAt;
          windowAt = sampleAt;
          windowBytes = 0;
          windowTimer = setTimeout(maybeSwitch, SWITCH_WINDOW);
        }
        resetIdle();

        let data = chunk;
        if (!prefixValid) {
          const combined = prefix.length ? Buffer.concat([prefix, data]) : data;
          if (combined.length < 4) {
            prefix = combined;
            continue;
          }
          if (!isZipPrefix(combined)) {
            abort("payload");
            return { kind: "network" };
          }
          prefixValid = true;
          prefix = Buffer.alloc(0);
          data = combined;
        }

        hash.update(data);
        received += data.length;
        windowBytes += data.length;
        publish(false);

        try {
          writePosition = await writeChunk(outputFile, data, writePosition);
        } catch (_) {
          abort("write");
          return { kind: "local" };
        }
      }
    } catch (_) {
      return { kind: stopReason === "write" ? "local" : "network" };
    }

    if (!await closeOutput()) {
      return { kind: "local" };
    }
    if (stopReason) {
      return { kind: stopReason === "write" ? "local" : "network" };
    }
    if (!firstData || !prefixValid || response.complete === false) {
      return { kind: "network" };
    }
    if (responseLength && received !== responseLength) {
      await closeStream(response);
      return { kind: "network" };
    }
    if (expectedSize && received !== expectedSize) {
      await closeStream(response);
      return { kind: "network" };
    }

    const actual = hash.digest("hex").toLowerCase();
    if (actual !== expectedDigest) return { kind: "digest" };

    publish(true);
    try {
      await fs.promises.rename(partPath, options.finalPath);
    } catch (_) {
      return { kind: "local" };
    }
    return { kind: "success" };
  } catch (_) {
    await closeStream(response);
    return { kind: stopReason === "write" ? "local" : "network" };
  } finally {
    if (outputFile) {
      try { await outputFile.close(); } catch (_) { /* 忽略收尾错误 */ }
    }
    clearTimers();
    controller.signal.removeEventListener("abort", onAbort);
  }
}

/** 下载、测速、换源并校验一个已确认的更新包。 */
async function downloadAsset(options, onState) {
  const originUrl = String(options && options.originUrl || "");
  const finalPath = options && options.finalPath ? String(options.finalPath) : "";
  const expectedSize = positiveNumber(options && options.expectedSize);
  const digest = /^sha256:([0-9a-f]{64})$/i.exec(String(options && options.expectedDigest || ""));
  if (!digest) return failure("该版本缺少完整性信息，已停止更新");
  if (!originUrl) return failure(NETWORK_MESSAGE);
  if (!finalPath) return failure(WRITE_MESSAGE);

  const partPath = finalPath + ".part";
  try {
    await fs.promises.mkdir(path.dirname(finalPath), { recursive: true });
    await fs.promises.rm(partPath, { force: true });
  } catch (_) {
    return failure(WRITE_MESSAGE);
  }

  reportState(onState, {
    phase: "probing",
    source: null,
    speed: 0,
    done: 0,
    total: expectedSize,
  });

  let ranked;
  try {
    ranked = await rankedCandidates(originUrl, expectedSize);
  } catch (_) {
    ranked = [];
  }
  if (!ranked.length) {
    try { await fs.promises.rm(partPath, { force: true }); } catch (_) { /* 忽略 */ }
    return failure(NETWORK_MESSAGE);
  }

  const attempted = new Set();
  let current = ranked[0];
  while (current) {
    attempted.add(current.index);
    reportState(onState, {
      phase: "downloading",
      source: current.name,
      speed: 0,
      done: 0,
      total: expectedSize || current.total || null,
    });

    const result = await downloadCandidate(current, {
      finalPath,
      partPath,
      expectedSize,
      expectedDigest: digest[1].toLowerCase(),
      ranked,
      attempted,
      onState,
    });
    if (result.kind === "success") return { ok: true, path: finalPath };
    if (result.kind === "digest") {
      try { await fs.promises.rm(partPath, { force: true }); } catch (_) { /* 忽略 */ }
      return failure("下载的更新包校验失败，已停止更新");
    }
    if (result.kind === "local") {
      try { await fs.promises.rm(partPath, { force: true }); } catch (_) { /* 忽略 */ }
      return failure(WRITE_MESSAGE);
    }

    try {
      await fs.promises.rm(partPath, { force: true });
    } catch (_) {
      return failure(WRITE_MESSAGE);
    }
    current = bestRemaining(ranked, attempted);
  }

  return failure(NETWORK_MESSAGE);
}

module.exports = { downloadAsset };
