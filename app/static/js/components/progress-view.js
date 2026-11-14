/* 通用双阶段进度展示组件：只接展示数据与回调，不导入 Desktop、同步或页面模块。

   - 下载与安装两条原生 <progress>，各有阶段文字与数值
   - 未知总量时不设置 value（原生不确定进度），也不按人为权重合并两个阶段
   - 业务文字、何时显示重启按钮由调用方决定 */
"use strict";

function percent(done, total) {
  if (!total || total <= 0) return null;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value < 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let size = value, index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size >= 100 || index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`;
}

/** 创建进度区域；返回 { root, update(state) }。 */
export function createProgressView() {
  const root = document.createElement("div");
  root.className = "progress-view";
  root.innerHTML =
    "<div class='progress-row' data-row='download' hidden>" +
    "  <div class='progress-head'><span data-label='download'>下载更新</span><span data-value='download'></span></div>" +
    "  <progress data-bar='download' max='100'></progress>" +
    "</div>" +
    "<div class='progress-row' data-row='install' hidden>" +
    "  <div class='progress-head'><span data-label='install'>安装准备</span><span data-value='install'></span></div>" +
    "  <progress data-bar='install' max='100'></progress>" +
    "</div>" +
    "<div class='progress-note' data-note hidden></div>";

  const bars = {
    download: root.querySelector("[data-bar='download']"),
    install: root.querySelector("[data-bar='install']"),
  };
  const rows = {
    download: root.querySelector("[data-row='download']"),
    install: root.querySelector("[data-row='install']"),
  };

  function apply(stage, done, total, label) {
    const row = rows[stage], bar = bars[stage];
    row.querySelector("[data-label]").textContent = label;
    const value = row.querySelector("[data-value]");
    const ratio = percent(done, total);
    if (ratio === null) {
      bar.removeAttribute("value");
      value.textContent = done > 0 ? formatBytes(done) : "";
    } else {
      bar.value = ratio;
      value.textContent = `${ratio}%`;
    }
  }

  function update(state) {
    if (!state) return;
    rows.download.hidden = !state.showDownload;
    rows.install.hidden = !state.showInstall;
    if (state.showDownload) apply("download", state.downloadDone, state.downloadTotal, state.downloadLabel || "下载更新");
    if (state.showInstall) {
      if (state.installComplete) {
        bars.install.value = 100;
        rows.install.querySelector("[data-value]").textContent = "100%";
        rows.install.querySelector("[data-label]").textContent = state.installLabel || "安装准备";
      } else {
        apply("install", state.installDone, state.installTotal, state.installLabel || "安装准备");
      }
    }
    const note = root.querySelector("[data-note]");
    note.hidden = !state.note;
    note.textContent = state.note || "";
  }

  return { root, update };
}
