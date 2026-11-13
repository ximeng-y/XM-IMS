/* 仓库、授权与续接集中在备份组件；高级维护复用旧接口，普通流程固定数据来源。 */
"use strict";

import { OnboardingWizard } from "xm/onboarding/wizard";
import { DependencyInstall } from "xm/components/dependency-install";

const BackupOnboarding = (() => {
  let activeDialog = null;
  const sameAccount = (a, b) => (a || "").toLowerCase() === (b || "").toLowerCase();
  const node = (tag, text, cls) => {
    const el = document.createElement(tag);
    if (text !== undefined) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  };
  const note = text => node("p", text, "sync-onboarding-note");
  const ok = text => node("div", text, "sync-onboarding-ok");

  function mount({ container, intent = "backup", settings, request, environment, onComplete, onCancel,
                   completeLabel = "返回数据同步" }) {
    container.classList.add("sync-onboarding");
    const c = {
      intent, opened: { remote: settings.remote || "", branch: settings.branch || "main" },
      mode: intent === "restore" || settings.remote ? "existing" : null,
      draft: { remote: settings.remote || "", branch: settings.branch || "main",
        device: settings.device || "本机", interval: settings.interval ?? 10,
        auto: settings.auto !== false, local_auto: settings.local_auto !== false,
        github_account: settings.github_account || "",
        repository: "xm-ims-backup" },
      // 工具准备已由共享组件完成；这里只接收环境快照，不再检测或安装 Git。
      env: environment || null, github: null, accounts: new Map(),
      check: null, source: null, result: null, key: null, created: null, resume: false,
    };
    let wizard;
    const needsGithub = () => c.mode === "github-new" || /^https:\/\/github\.com(?:\/|$)/i.test(c.draft.remote);
    function invalidate() {
      c.check = null; c.result = null; c.key = null; c.resume = false;
    }
    function invalidateCredentials() {
      // 已确认动作的续接身份保留；重新登录只刷新凭据，不能丢掉创建结果再发起建库。
      if (!c.result || c.result.completed) invalidate();
    }
    function payload() {
      const p = { mode: c.mode, device: c.draft.device, interval: Number(c.draft.interval),
        auto: c.draft.auto, local_auto: c.draft.local_auto, github_account: c.draft.github_account };
      if (c.mode === "existing") { p.remote = c.draft.remote; p.branch = c.draft.branch; }
      else p.repository = c.draft.repository;
      if (c.intent !== "maintenance") p.intent = c.intent;
      return p;
    }
    function confirmedPayload() {
      if (!c.key) c.key = crypto.randomUUID();
      return { ...payload(), confirm: true, idempotency_key: c.key,
        expected_remote: c.opened.remote, expected_branch: c.opened.branch,
        remote_tip: c.check?.remote_tip ?? null,
        ...(c.intent === "maintenance" ? { source: c.source } : {}) };
    }
    function button(label, fn, cls = "btn-outline") {
      const b = node("button", label, cls);
      b.type = "button"; b.onclick = () => wizard.run(fn);
      return b;
    }
    function input(label, value, placeholder, change, { type = "text", maxLength } = {}) {
      const row = node("label"), el = node("input");
      el.type = type; el.value = value ?? ""; el.placeholder = placeholder || ""; el.autocomplete = "off";
      if (maxLength) el.maxLength = maxLength;
      if (type === "number") { el.min = "1"; el.step = "1"; }
      el.oninput = () => { change(el.value); invalidate(); wizard.updateNav(); };
      row.append(node("span", label), el);
      return row;
    }
    function choice(value, title, description, selected, change, group) {
      const label = node("label", undefined, "sync-onboarding-option"), radio = node("input");
      radio.type = "radio"; radio.name = group; radio.checked = selected === value;
      radio.onchange = () => { if (radio.checked) change(value); wizard.updateNav(); };
      const text = node("span");
      text.append(node("strong", title), node("small", description));
      label.append(radio, text); return label;
    }
    function repositoryError() {
      if (!c.mode) return "请选择备份方式";
      if (c.mode === "github-new" && !/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/.test(c.draft.repository))
        return "仓库名使用字母或数字开头，可包含点、下划线和连字符";
      if (c.mode === "existing") {
        if (!c.draft.remote) return "请填写备份仓库地址";
        if (!/^[\w.-]+@[\w.-]+:[\w./-]+$/.test(c.draft.remote)) {
          try {
            const url = new URL(c.draft.remote);
            if (!["https:", "ssh:"].includes(url.protocol) || !url.hostname ||
                url.password || url.search || url.hash || (url.protocol === "https:" && url.username))
              return "请填写标准 HTTPS 或 SSH 仓库地址";
          } catch (_) { return "请填写标准 HTTPS 或 SSH 仓库地址"; }
        }
        if (/\s/.test(c.draft.remote)) return "请检查仓库地址中的空格";
        if (!c.draft.branch || c.draft.branch.startsWith("-")) return "请填写有效分支名，如 main";
      }
      if (!c.draft.device || c.draft.device.length > 80) return "本机名称需为 1–80 个字符";
      if (!String(c.draft.interval).trim() || !Number.isInteger(Number(c.draft.interval)) || Number(c.draft.interval) < 1)
        return "同步间隔需为至少 1 分钟的整数";
      return null;
    }
    async function afterRepository() {
      // Git 前置准备在打开向导之前完成；这里只按仓库模式进入下一步。
      await wizard.go(needsGithub() ? "github-account" : "review");
    }
    function installGcm(next) {
      DependencyInstall.open({
        component: "gcm",
        // GCM 使用独立的安装目标描述；原字段仍描述 Git。
        installation: c.env?.gcm_installation || c.env?.installation,
        install: async (body) => {
          const env = await request("POST", "/onboarding/install", { ...body, confirm: true });
          c.env = env;
          return env;
        },
        onDone: async () => {
          if (next) await wizard.go(next);
          else await wizard.enter();
        },
      });
    }
    async function loadGithub() {
      if (!c.env?.gcm.available) return;
      const selected = c.draft.github_account, cached = c.accounts.get(selected.toLowerCase());
      if (cached) { c.github = cached; return; }
      try {
        c.github = await request("GET", "/onboarding/github" +
          (selected ? "?account=" + encodeURIComponent(selected) : ""));
      } catch (error) {
        if (error.code !== "ONBOARDING_GITHUB_ACCOUNT") throw error;
        c.draft.github_account = "";
        c.github = await request("GET", "/onboarding/github");
      }
      c.accounts.set((c.github.selected || "").toLowerCase(), c.github);
    }
    function acceptGithub(result) {
      c.github = result;
      c.draft.github_account = result.verified_as || result.selected || "";
      c.accounts.set(c.draft.github_account.toLowerCase(), result);
      invalidateCredentials();
    }
    async function selectAccount(name) {
      c.draft.github_account = name; invalidateCredentials(); await loadGithub();
      if (c.github.verified_as) c.draft.github_account = c.github.verified_as;
      wizard.render();
    }
    async function login(force) {
      c.accounts.delete(c.draft.github_account.toLowerCase());
      acceptGithub(await request("POST", "/onboarding/github/login",
        { confirm: true, force, account: c.draft.github_account || null }));
      wizard.render();
    }
    function renderSummary(info, box) {
      if (!info) return;
      const count = info.shop_count ?? info.shops?.length;
      if (count !== undefined) {
        box.append(ok(`备份包含 ${count} 间店铺、${info.sku_count} 个单品、${info.movement_count} 条流水。`));
        if (info.commit_time) {
          const date = new Date(info.commit_time);
          box.append(note("最近备份提交时间：" + (Number.isNaN(date.getTime()) ? info.commit_time : date.toLocaleString("zh-CN"))));
        }
        const details = node("details"), list = node("ul", undefined, "onboarding-summary");
        details.append(node("summary", "查看店铺"));
        for (const shop of info.shops || []) list.append(node("li",
          `${shop.name} · ${shop.sku_count} 个单品 · ${shop.movement_count} 条流水`));
        details.append(list); box.append(details);
      }
      // dataset 用于服务端核对，不把内部数据标识展示给用户。
    }
    function resultError(r, box) {
      if (r.created_repository_url || c.created) box.append(ok("备份仓库已创建。"));
      if (r.settings_applied) box.append(note("仓库设置已保存。"));
      if (r.error) {
        const error = node("div", undefined, "sync-onboarding-error");
        error.append(node("strong", r.error.user_message || "这一步暂未完成"));
        if (r.error.user_hint) error.append(node("p", r.error.user_hint));
        box.append(error);
      }
    }
    async function execute() {
      const body = confirmedPayload();
      const endpoint = c.intent === "maintenance" ? "/onboarding/complete" :
        c.resume ? "/onboarding/flow/resume" : "/onboarding/flow/complete";
      if (c.resume) {
        body.target_remote = c.result?.target_remote || c.check.remote;
        body.created_repository_url = c.created;
      }
      try {
        c.result = await request("POST", endpoint, body);
        c.created = c.result.created_repository_url || c.created;
      } catch (error) {
        // HTTP 回执失败不等于没有副作用：续接按真实状态读回，不能重复创建。
        c.result = { completed: false, settings_applied: false, error,
          target_remote: c.check.remote, target_branch: c.check.branch,
          resume_action: c.mode === "github-new" ? "recheck_remote" : "retry_check" };
      }
      if (["GIT_FAILED", "ONBOARDING_GITHUB_CREDENTIAL", "ONBOARDING_GITHUB_FORBIDDEN"].includes(c.result.error?.code)) {
        c.accounts.delete(c.draft.github_account.toLowerCase()); c.github = null;
      }
      await wizard.go("complete");
    }
    async function resume() {
      const prior = c.result;
      const result = await request("POST", "/onboarding/flow/resume", {
        ...confirmedPayload(), target_remote: prior.target_remote || c.check.remote,
        created_repository_url: c.created,
      });
      c.result = result; c.created = result.created_repository_url || c.created;
      if (result.completed) { wizard.render(); return; }
      if (result.repository_exists === false) {
        c.resume = false; c.result = null; c.check = null; c.lockBack = false;
        await wizard.go("review"); return;
      }
      if (result.check) {
        // 已建仓库转为 existing，检查摘要后再确认；原幂等键继续使用。
        c.mode = result.check.mode; c.draft.remote = result.check.remote; c.draft.branch = result.check.branch;
        c.check = result.check; c.resume = result.settings_applied; c.keepCheck = true;
        await wizard.go("review"); return;
      }
      wizard.render();
    }
    const steps = [
      {
        id: "backup-method", title: c.intent === "maintenance" ? "选择仓库方式" : "配置远端备份",
        when: () => c.intent !== "restore",
        render(_, box) {
          box.append(choice("github-new", "创建 GitHub 私有仓库", "为本机数据准备独立的备份仓库。",
            c.mode, mode => { c.mode = mode; invalidate(); }, "onboarding-method"),
          choice("existing", "连接已有仓库", "填写仓库地址，GitHub 可在下一步登录，SSH 使用本机密钥。",
            c.mode, mode => { c.mode = mode; invalidate(); }, "onboarding-method"));
        },
        canNext: () => c.mode ? null : "请选择备份方式",
      },
      {
        id: "repository", title: c.intent === "restore" ? "连接已有备份" : "填写仓库信息",
        render(_, box) {
          if (c.mode === "github-new") box.append(input("仓库名称", c.draft.repository, "xm-ims-backup",
            value => { c.draft.repository = value.trim(); }, { maxLength: 100 }));
          else box.append(input("备份仓库地址", c.draft.remote, "https://github.com/用户名/仓库.git",
            value => { c.draft.remote = value.trim(); }),
            note("填写独立的私有仓库地址，账号登录在后续步骤完成。"));
          const details = node("details");
          details.append(node("summary", "高级设置"));
          if (c.mode === "existing") details.append(input("数据分支", c.draft.branch, "main",
            value => { c.draft.branch = value.trim(); }));
          details.append(input("本机名称", c.draft.device, "本机",
            value => { c.draft.device = value.trim(); }, { maxLength: 80 }));
          const grid = node("div", undefined, "sync-onboarding-grid");
          grid.append(input("间隔（分钟）", c.draft.interval, "10",
            value => { c.draft.interval = value; }, { type: "number" }));
          const switches = node("div", undefined, "sync-onboarding-switches");
          for (const [field, text] of [["local_auto", "本地自动备份"], ["auto", "自动将备份同步到云端"]]) {
            const row = node("label", undefined, "sync-onboarding-check"), check = node("input");
            check.type = "checkbox"; check.checked = c.draft[field];
            check.onchange = () => { c.draft[field] = check.checked; invalidate(); wizard.updateNav(); };
            row.append(check, node("span", text));
            switches.append(row);
          }
          grid.append(switches); details.append(grid); box.append(details);
          // 底栏显示校验结果，即使高级设置折叠也能看到具体阻塞原因。
        },
        canNext: repositoryError, onPrimary: afterRepository,
      },
      {
        id: "github-account", title: "登录 GitHub", when: needsGithub,
        async enter() {
          c.lockBack = !!c.created || !!c.resume;
          if (!c.env?.gcm.available) c.env = await request("GET", "/onboarding/environment?refresh=true");
          await loadGithub();
        },
        render(_, box) {
          if (!c.env?.gcm.available) {
            box.append(note("安装登录组件后，即可在系统浏览器中登录 GitHub。"));
            if (c.env?.one_click_install) box.append(button("安装登录组件", async () => {
              installGcm("github-account");
            }));
            const link = node("a", "打开登录组件官方安装说明");
            link.href = "https://github.com/git-ecosystem/git-credential-manager/blob/release/docs/install.md";
            link.target = "_blank"; link.rel = "noopener";
            box.append(link, button("重新检测", async () => {
              c.env = await request("GET", "/onboarding/environment?refresh=true"); await loadGithub(); wizard.render();
            }));
            return;
          }
          const g = c.github || { accounts: [] };
          if (g.verified) box.append(ok("已登录 GitHub：" + (g.verified_as || g.selected)));
          if (g.accounts.length) {
            box.append(note("选择用于访问备份仓库的账号："));
            for (const name of g.accounts) box.append(choice(name, name, "",
              sameAccount(c.draft.github_account, name) ? name : "", value => {
                wizard.run(() => selectAccount(value));
              }, "onboarding-account"));
          }
          if (g.error) resultError({ error: g.error }, box);
          box.append(note("登录会打开系统浏览器，完成 GitHub 授权后回到本页。"),
            button(g.verified || g.selected ? "重新登录" : "登录 GitHub", () => login(!!g.selected)));
          if (g.error) box.append(button("重新验证", async () => {
            c.accounts.delete(c.draft.github_account.toLowerCase()); await loadGithub(); wizard.render();
          }));
        },
        canNext: () => !c.env?.gcm.available ? "请先准备登录组件" :
          c.github?.verified ? null : c.github?.accounts.length ? "请选择有效账号，或登录 GitHub" : "请登录 GitHub",
        onPrimary: () => wizard.go(c.result && !c.result.completed ? "complete" : "review"),
      },
      {
        id: "review", title: c.intent === "restore" ? "检查并确认恢复" : "检查并确认",
        async enter() {
          c.lockBack = !!c.created || !!c.resume;
          if (c.keepCheck) { c.keepCheck = false; return; }
          c.check = await request("POST", c.intent === "maintenance" ? "/onboarding/check" : "/onboarding/flow/check", payload());
          if (c.intent === "maintenance") {
            const sources = c.check.sources || ["local"];
            c.source = sources.includes(c.source) ? c.source : sources.length === 1 ? sources[0] : null;
          }
        },
        render(_, box) {
          const check = c.check;
          box.append(note("备份仓库：" + check.remote)); renderSummary(check.snapshot, box);
          if (c.intent === "maintenance") {
            if (check.snapshot) box.append(
              choice("remote", "从远端恢复", "用备份替换本机数据，当前数据副本会保留。",
                c.source, value => { c.source = value; }, "onboarding-source"),
              choice("local", "使用本机数据", "将本机数据上传到远端，远端旧版本保留在历史中。",
                c.source, value => { c.source = value; }, "onboarding-source"));
            else box.append(ok("将使用本机数据开始备份。"));
            if (check.remote !== c.opened.remote || check.branch !== c.opened.branch) box.append(
              note("更换仓库后，将按选择的数据来源重新建立同步关系。"),
              note("原仓库：" + (c.opened.remote || "尚未配置") + " · 分支 " + c.opened.branch),
              note("新分支：" + check.branch));
          } else if (check.decision === "remote_has_data") {
            box.append(note("这个仓库已有一份备份，请选择其他仓库。"));
            const link = node("a", "采用这份备份：进入高级维护"); link.href = "/sync"; box.append(link);
          } else if (check.decision === "remote_empty") box.append(note("这个仓库尚无可恢复备份，请确认仓库地址。"));
          else if (check.decision === "settings_only") box.append(ok("将更新备份设置，继续使用当前数据。"));
          else if (check.decision === "resume_required") { box.append(ok("首次备份已准备好，可以继续上传。")); c.resume = true; }
          else if (c.intent === "restore") box.append(ok("确认后，将这份备份恢复到本机。"));
          else box.append(ok(c.mode === "github-new" ? "确认后创建私有仓库，并上传本机数据。" : "确认后将本机数据备份到这个仓库。"));
          box.append(button("重新检查", async () => { c.check = null; await wizard.enter(); }));
        },
        canNext: () => !c.check ? "正在检查备份仓库…" : c.intent === "maintenance" ?
          (c.check.snapshot && !c.source ? "请选择使用哪一侧的数据" : null) :
          c.check.decision === "remote_has_data" ? "请换一个备份仓库，或进入高级维护恢复" :
          c.check.decision === "remote_empty" ? "请确认已有备份的仓库地址" : null,
        primaryLabel: () => c.intent === "restore" ? "确认恢复到这台电脑" :
          c.intent === "maintenance" ? "确认应用" : c.check?.decision === "resume_required" ? "继续首次上传" : "确认并开始备份",
        onPrimary: execute,
      },
      {
        id: "complete", title: "备份进度",
        render(_, box) {
          const r = c.result;
          c.lockBack = !!r?.settings_applied || !!c.created ||
            (c.mode === "github-new" && r?.resume_action === "recheck_remote");
          if (r.completed) {
            box.append(ok(c.intent === "restore" ? "备份已恢复到本机。" : "备份配置完成。"));
            if (r.local_changes_pending) box.append(note("首次备份已上传，之后的本机修改会在后续同步中上传。"));
            return;
          }
          resultError(r, box);
          if (needsGithub() && !c.github && c.intent !== "maintenance") box.append(
            button("检查 GitHub 登录", async () => { await wizard.go("github-account"); }));
          if (c.intent === "maintenance") { box.append(note("返回数据同步页面，可继续处理当前配置。")); return; }
          if (r.resume_action === "recheck_remote" && c.mode === "github-new")
            box.append(note("创建结果尚待确认，请先检查此仓库。"), button("检查此仓库", resume));
          else if (["ONBOARDING_MAINTENANCE_REQUIRED", "ONBOARDING_REMOTE_CHANGED", "SNAPSHOT_INVALID",
            "SCHEMA_UNSUPPORTED", "GIT_BRANCH_MISSING", "BASE_MISSING"].includes(r.error?.code)) {
            const link = node("a", "进入数据同步处理"); link.href = "/sync"; box.append(link);
          } else box.append(button(r.resume_action === "retry_upload" ? "继续首次上传" :
            r.resume_action === "reuse_created" ? "继续使用已创建仓库" : "重试当前步骤", resume));
        },
        canNext: () => c.result?.completed || c.intent === "maintenance" ? null : "按上方提示继续处理",
        primaryLabel: () => c.intent === "maintenance" ? "返回数据同步" : completeLabel,
        onPrimary: () => wizard.finish(),
      },
    ];
    wizard = new OnboardingWizard({ steps, context: c, container, onComplete, onCancel });
    wizard.enter();
    return wizard;
  }
  function open(options) {
    if (activeDialog) return;
    const dialog = node("dialog", undefined, "sync-onboarding"), container = node("div");
    dialog.append(container); document.body.append(dialog); activeDialog = dialog;
    const close = callback => context => { dialog.close(); return callback?.(context); };
    const wizard = mount({ ...options, container,
      onComplete: close(options.onComplete), onCancel: close(options.onCancel) });
    dialog.addEventListener("cancel", event => {
      event.preventDefault();
      if (!wizard.busy) close(options.onCancel)(wizard.context);
    });
    dialog.addEventListener("close", () => { dialog.remove(); activeDialog = null; });
    dialog.showModal();
    return wizard;
  }
  return { mount, open };
})();

export { BackupOnboarding };
