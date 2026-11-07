/* 本地备份历史：列表、分页、立即备份与按版本还原。
   请求函数与状态刷新由同步页传入；本组件不导入页面模块，也不持有页面内部状态。 */
"use strict";

import { UI } from "xm/ui";

const REASONS = {scheduled: "定时备份", manual: "手动备份", sync: "同步前备份",
  protect: "还原前保护", restore: "还原结果"};
const limit = 10;

export function mountLocalBackups({ list, pager, backupButton, request, onChanged, onRestored }) {
  let offset = 0;
  // 同一次正式写操作的重试复用同一个幂等键；用户开始新操作才换键。
  const keys = new Map();
  const key = (name) => {
    if (!keys.has(name)) keys.set(name, crypto.randomUUID());
    return keys.get(name);
  };

  function row(record) {
    const item = document.createElement("div");
    item.className = "local-backup-row";
    const info = document.createElement("div");
    info.className = "local-backup-info";
    const title = document.createElement("strong");
    title.textContent = UI.fmtTime(record.created_at);
    const meta = document.createElement("span");
    meta.className = "muted";
    meta.textContent = (REASONS[record.reason] || "备份") + (record.is_current ? " · 当前数据的最近备份" : "");
    info.append(title, meta);
    const restore = UI.mkBtn("还原", "btn-outline btn-sm");
    restore.type = "button";
    restore.onclick = () => restoreVersion(restore, record);
    item.append(info, restore);
    return item;
  }

  async function load() {
    const page = await request("GET", "/backups?offset=" + offset + "&limit=" + limit);
    if (!page.total) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "还没有本地备份。点击「立即备份」保存当前数据的第一个版本。";
      list.replaceChildren(empty);
      pager.replaceChildren();
      return;
    }
    list.replaceChildren(...page.rows.map(row));
    UI.renderPager("#" + pager.id, page, (next) => {
      offset = next;
      load().catch((error) => UI.apiFail(error, "备份列表暂未加载"));
    });
  }

  async function backup(button) {
    await UI.withBusy(button, async () => {
      try {
        const result = await request("POST", "/backups", { idempotency_key: key("backup") });
        keys.delete("backup");
        UI.ok(result.created ? "已保存一份本地备份。" : "数据没有变化，已保留最近的备份版本。");
        offset = 0;
        // 备份已经成功，列表刷新失败不应把它显示成备份失败；下一次刷新会恢复列表。
        await load().catch(() => {});
        await onChanged();
      } catch (error) {
        UI.fail(error.user_message || "本地备份暂未完成",
          { title: UI.codeTitle(error.code) || "本地备份暂未完成", hint: error.user_hint || "" });
      }
    });
  }

  async function restoreVersion(button, record) {
    const crossDataset = record.same_dataset === false;
    const confirmed = await UI.confirmDialog({
      title: "还原整套数据",
      bodyHtml: "<p>将把整套数据恢复到所选备份，当前数据会先保存一份备份。</p>" +
        (crossDataset ? "<p>这份备份与当前数据不是同一套，恢复后需要重新确认云端绑定。</p>" : ""),
      confirmText: "确认还原",
      danger: true,
    });
    if (!confirmed) return;
    await UI.withBusy(button, async () => {
      try {
        const result = await request("POST", "/backups/" + encodeURIComponent(record.id) + "/restore",
          { confirm: true, idempotency_key: key("restore-" + record.id) });
        if (result.dry_run) {
          keys.delete("restore-" + record.id);
          UI.ok("已确认这份备份可以还原。");
          return;
        }
        if (result.restored !== true) {
          // 还原尚未生效：保留幂等键，用户重试时会继续完成同一次还原。
          UI.fail("还原还没有完成",
            { title: "还原还没有完成", hint: "请稍后重试；未完成前数据保持不变" });
          return;
        }
        keys.delete("restore-" + record.id);
        UI.ok("数据已还原到所选备份。");
        await onRestored(result);
      } catch (error) {
        UI.fail(error.user_message || "还原暂未完成",
          { title: UI.codeTitle(error.code) || "还原暂未完成", hint: error.user_hint || "" });
      }
    });
  }

  backupButton.type = "button";
  backupButton.addEventListener("click", () => backup(backupButton));
  return { load };
}
