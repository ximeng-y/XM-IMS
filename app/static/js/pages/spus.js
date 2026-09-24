/* SPU 管理页：列表 / 新建 / 改名 */
"use strict";

async function loadSpus() {
  const tbody = $("#spuRows");
  try {
    const data = await XM.get("/spus");
    if (!data.spus.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="t-empty">还没有 SPU；可在「库存」页用「新建 SPU」创建，或在新建单品时按名称自动创建</td></tr>`;
      return;
    }
    tbody.innerHTML = data.spus.map((s) => `
      <tr>
        <td class="num mono">${s.id}</td>
        <td>${UI.esc(s.name)}</td>
        <td class="num">${s.sku_count}</td>
        <td class="muted">${UI.fmtTime(s.updated_at)}</td>
        <td><button type="button" class="btn-outline btn-sm" data-rename="${s.id}" data-name="${UI.esc(s.name)}">改名</button></td>
      </tr>`).join("");
    $$("button[data-rename]", tbody).forEach((b) => b.addEventListener("click", renameFlow));
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="t-empty">加载失败：${UI.esc(e.message)}</td></tr>`;
  }
}

async function renameFlow(ev) {
  const btn = ev.currentTarget;
  const id = Number(btn.dataset.rename);
  const oldName = btn.dataset.name;
  const name = await UI.promptDialog({
    title: `SPU #${id} 改名`, label: "新名称（全局一处生效）",
    value: oldName, maxLength: 200,
  });
  if (name === null || name === oldName) return;
  try {
    await XM.patch(`/spus/${id}`, { name });
    UI.ok(`SPU #${id} 已改名为「${name}」`, { title: "SPU_RENAMED" });
    loadSpus();
  } catch (e) { UI.apiFail(e, "改名失败"); }
}

window.PAGE_INIT = async function () {
  if (!UI.requireShop()) return;

  $("#fCreate").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const btn = ev.target.querySelector("button[type=submit]");
    const name = $("#spuName").value.trim();
    if (!name) { UI.fail("请填写 SPU 名称"); return; }
    UI.withBusy(btn, async () => {
      try {
        const spu = await XM.post("/spus", { name });
        UI.resultBox("#createResult", {
          html: `<div class="headline"><span class="badge">SPU_CREATED</span> ` +
                `「${UI.esc(spu.name)}」已创建（#${spu.id}）</div>`,
        });
        $("#spuName").value = "";
        loadSpus();
      } catch (e) {
        UI.resultBox("#createResult", { error: true, html: UI.apiFailHtml(e) });
      }
    });
  });

  await loadSpus();
};
