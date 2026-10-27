/* 备份配置向导：有序步骤注册表 + 每次打开独立的草稿。
   只通过 /onboarding/* 接口工作；打开、返回、取消都不修改已保存的配置，
   只有最后「确认并开始备份」才会应用。所有动态文本走 DOM 文本节点，
   凭据类内容不进入页面；本文件不替换 window.PAGE_INIT，由 sync.js 统一初始化。 */
"use strict";
window.SyncOnboarding = (() => {
  let active = false;

  function open({ settings, onDone }) {
    if (active) return;
    active = true;
    const ctx = {
      opened: { remote: settings.remote || '', branch: settings.branch || 'main' },
      mode: settings.remote ? 'existing' : null,
      draft: {
        remote: settings.remote || '', branch: settings.branch || 'main',
        device: settings.device || '本机', interval: settings.interval || 10,
        auto: settings.auto !== false, github_account: settings.github_account || '',
        repository: 'xm-ims-backup',
      },
      env: null, github: null, check: null, source: null, result: null,
      busy: false, stepIndex: 0,
    };
    const invalidateCheck = c => { c.check = null; c.source = null; };

    // 步骤注册表：稳定 id、标题、when(适用路径)、enter(进入检测)、render(展示)、canNext(阻塞原因)。
    // 增删步骤只改这张表与对应步骤对象，不修改硬编码编号。
    const STEPS = [
      {
        id: 'git-environment', title: '准备备份工具', when: () => true,
        async enter(c) {
          // 每次进入都重新检测环境，不用任何「安装完成」缓存决定跳过。
          c.env = await Sync.api('GET', '/onboarding/environment');
        },
        render(c, box) {
          const env = c.env;
          const line = document.createElement('p');
          if (env.git.available) {
            line.textContent = '本机已安装 Git' + (env.git.version ? '（' + env.git.version + '）' : '') + '，可以直接继续。';
            box.append(line);
          } else {
            line.textContent = '本机还没有可用的 Git。';
            const note = document.createElement('p'); note.className = 'sync-onboarding-note';
            if (env.one_click_install) {
              note.textContent = '可以由本程序从官方来源下载安装包，静默安装到当前用户目录；下载并安装可能需要几分钟。';
              box.append(line, note,
                actionBtn('安装 Git', async () => {
                  c.env = await Sync.api('POST', '/onboarding/install', { component: 'git', confirm: true });
                  draw();
                }));
            } else {
              note.textContent = '当前系统暂不支持自动安装，请从官网下载安装后点击「重新检测」。';
              box.append(line, note, officialLink(),
                actionBtn('重新检测', async () => draw(), 'btn-outline'));
            }
            if (env.one_click_install) box.append(actionBtn('重新检测', async () => draw(), 'btn-outline'));
          }
          const gcm = document.createElement('p'); gcm.className = 'sync-onboarding-note';
          gcm.textContent = env.gcm.available ? 'GitHub 登录组件已就绪。'
            : 'GitHub 登录组件尚未安装；选择「创建 GitHub 备份仓库」时，会在对应步骤提供安装。';
          box.append(gcm);
        },
        canNext(c) {
          if (!c.env) return '正在检测本机环境…';
          if (!c.env.git.available) return '需要先准备好 Git 才能继续';
          return null;
        },
      },
      {
        id: 'backup-method', title: '选择备份方式', when: () => true,
        enter() {},
        render(c, box) {
          box.append(methodOption('github-new', '创建 GitHub 备份仓库',
            '在程序内完成准备，用系统浏览器登录 GitHub，最后自动创建一个独立的私有空仓库并连接。'));
          box.append(methodOption('existing', '连接已有仓库',
            '填写标准 HTTPS 或 SSH 仓库地址。支持非 GitHub 平台，但不提供自动登录或自动建库。'));
        },
        canNext(c) { return c.mode ? null : '请先选择一种备份方式'; },
      },
      {
        id: 'github-account', title: '登录 GitHub', when: c => c.mode === 'github-new',
        async enter(c) {
          // 本步骤自行确认前置：GCM 缺失时就地提供安装动作，不假设某个固定步骤已执行。
          if (!c.env || !c.env.gcm.available) c.env = await Sync.api('GET', '/onboarding/environment');
          if (c.env.gcm.available) c.github = await Sync.api('GET', '/onboarding/github');
        },
        render(c, box) {
          if (!c.env.gcm.available) {
            const note = document.createElement('p'); note.className = 'sync-onboarding-note';
            note.textContent = 'GitHub 登录组件（凭据管理器）还没有安装。安装后即可用系统浏览器完成 GitHub 授权。';
            box.append(note, actionBtn('安装登录组件', async () => {
              c.env = await Sync.api('POST', '/onboarding/install', { component: 'gcm', confirm: true });
              draw();
            }));
            return;
          }
          const g = c.github || { accounts: [], verified: false };
          if (g.verified) {
            box.append(okBox('已登录 GitHub：' + (g.verified_as || g.selected)));
            box.append(actionBtn('重新登录', async () => {
              c.github = await Sync.api('POST', '/onboarding/github/login',
                { confirm: true, force: true, account: g.verified_as || g.selected });
              c.draft.github_account = c.github.verified_as || '';
              invalidateCheck(c);
              draw();
            }, 'btn-outline'));
            return;
          }
          if (g.accounts.length) {
            const note = document.createElement('p'); note.className = 'sync-onboarding-note';
            note.textContent = '本机保存了 GitHub 账号，请选择用于创建备份仓库的账号：';
            box.append(note);
            const list = document.createElement('div'); list.className = 'sync-onboarding-options';
            for (const name of g.accounts) {
              const item = document.createElement('label'); item.className = 'sync-onboarding-option';
              const radio = document.createElement('input');
              radio.type = 'radio'; radio.name = 'sync-onboarding-account'; radio.value = name;
              radio.checked = c.draft.github_account === name;
              radio.onchange = () => { if (radio.checked) selectAccount(c, name); };
              item.append(radio, Object.assign(document.createElement('span'), { textContent: name }));
              list.append(item);
            }
            box.append(list);
          } else {
            const note = document.createElement('p'); note.className = 'sync-onboarding-note';
            note.textContent = '点击「登录 GitHub」后，会打开系统浏览器进入 GitHub 官方授权页面；完成后回到本页。';
            box.append(note);
          }
          if (g.selected && g.error) box.append(errBox(g.error.user_message || g.error.message || '这个账号的登录状态无效', g.error.user_hint || ''));
          box.append(actionBtn(g.accounts.length ? '重新登录' : '登录 GitHub', async () => {
            c.github = await Sync.api('POST', '/onboarding/github/login',
              { confirm: true, force: g.accounts.length > 0, account: c.draft.github_account || null });
            c.draft.github_account = c.github.verified_as || '';
            invalidateCheck(c);
            draw();
          }));
        },
        canNext(c) {
          if (!c.env || !c.env.gcm.available) return '需要先安装登录组件';
          const g = c.github;
          if (!g) return '正在读取 GitHub 账号状态…';
          if (g.verified) return null;
          if (g.accounts.length && !c.draft.github_account) return '请选择一个账号';
          if (g.accounts.length && c.draft.github_account) return '所选账号还没有通过验证，请重新登录';
          return '需要先登录 GitHub';
        },
      },
      {
        id: 'repository', title: '设置仓库', when: c => !!c.mode,
        enter() {},
        render(c, box) {
          if (c.mode === 'github-new') {
            const acc = document.createElement('p'); acc.className = 'sync-onboarding-note';
            acc.textContent = '将使用账号：' + ((c.github && c.github.verified_as) || c.draft.github_account)
              + '。仓库会创建为私有空仓库。';
            box.append(acc, inputRow('仓库名称', c.draft.repository || 'xm-ims-backup', 'xm-ims-backup',
              v => { c.draft.repository = v.trim(); invalidateCheck(c); }));
            box.append(Object.assign(document.createElement('p'),
              { className: 'sync-onboarding-note', textContent: '数据分支固定使用 main。' }));
          } else {
            box.append(inputRow('仓库地址', c.draft.remote, 'https://github.com/用户名/私有数据仓库.git',
              v => { c.draft.remote = v.trim(); invalidateCheck(c); }));
            box.append(Object.assign(document.createElement('p'), { className: 'sync-onboarding-note',
              textContent: '请使用一个独立的私有仓库。HTTPS 复用本机已保存的账号凭证，SSH 复用本机密钥；地址里不要包含账号、密码或访问令牌。' }));
            box.append(inputRow('数据分支', c.draft.branch, 'main',
              v => { c.draft.branch = v.trim(); invalidateCheck(c); }));
          }
          box.append(inputRow('本机名称', c.draft.device, '', v => { c.draft.device = v.trim(); }));
          const grid = document.createElement('div'); grid.className = 'sync-onboarding-grid';
          grid.append(inputRow('间隔（分钟）', String(c.draft.interval), '', v => {
            const n = Number(v); if (Number.isInteger(n) && n >= 1) c.draft.interval = n;
          }, 'number'));
          const auto = document.createElement('label'); auto.className = 'sync-onboarding-check';
          const autoBox = document.createElement('input'); autoBox.type = 'checkbox'; autoBox.checked = c.draft.auto;
          autoBox.onchange = () => { c.draft.auto = autoBox.checked; };
          auto.append(autoBox, Object.assign(document.createElement('span'), { textContent: '自动检查、提交和推送' }));
          grid.append(auto);
          box.append(grid);
        },
        canNext(c) {
          if (c.mode === 'github-new') return c.draft.repository ? null : '请填写仓库名称';
          return c.draft.remote ? null : '请填写仓库地址';
        },
      },
      {
        id: 'review', title: '检查并确认', when: c => !!c.mode,
        async enter(c) {
          // 每次进入都重新做只读检查；修改过地址、分支、仓库名或账号后旧结果不会沿用。
          c.check = await Sync.api('POST', '/onboarding/check', draftPayload(c));
          const sources = c.check.sources || ['local'];
          c.source = sources.includes(c.source) ? c.source : (sources.length === 1 ? sources[0] : null);
        },
        render(c, box) {
          const check = c.check;
          if (check.mode === 'github-new') {
            box.append(okBox('将创建私有空仓库 ' + check.owner + ' / ' + check.repository + '，并使用本机数据开始首次备份。'));
          } else if (check.snapshot === null) {
            box.append(okBox('该仓库当前是空的。将使用本机数据开始备份。'));
          } else {
            box.append(Object.assign(document.createElement('p'),
              { textContent: '已检测到有效的备份数据。请选择使用哪一侧的数据：' }),
              sourceChoice('remote', '从远端恢复', '用仓库里的备份替换本机数据；替换前会保留当前数据副本。'),
              sourceChoice('local', '使用本机数据', '远端当前内容将改为本机数据，旧版本保留在历史中。'));
          }
          if (changingRepo(c)) {
            box.append(Object.assign(document.createElement('p'), { className: 'sync-onboarding-change',
              textContent: '原仓库：' + (c.opened.remote || '（未配置）') + '（分支 ' + c.opened.branch + '）'
                + ' → 新仓库：' + check.remote + '（分支 ' + check.branch + '）' }),
              Object.assign(document.createElement('p'), { className: 'sync-onboarding-note',
                textContent: '更换仓库会清空本机的同步历史与基线；远端与本机的数据关系将按上面选择的来源重新建立。' }));
          }
        },
        canNext(c) {
          if (!c.check) return '正在检查仓库…';
          if (c.check.mode === 'existing' && c.check.snapshot && !c.source) return '请选择数据来源';
          return null;
        },
        primaryLabel() { return '确认并开始备份'; },
        async onPrimary(c) {
          // 最后确认：服务端会重新校验候选与当前绑定，成功后应用配置并执行首次同步。
          c.result = await Sync.api('POST', '/onboarding/complete', {
            ...draftPayload(c), source: c.source, confirm: true,
            expected_remote: c.opened.remote, expected_branch: c.opened.branch,
            remote_tip: c.check ? c.check.remote_tip : null,
          });
          c.stepIndex++;
          draw();
        },
      },
      {
        id: 'complete', title: '完成配置', when: c => !!c.mode,
        enter(c) {
          if (!c.result) c.result = { completed: false, settings_applied: false,
            error: { user_message: '还没有执行最后确认', user_hint: '请回到上一步重新检查' } };
        },
        render(c, box) {
          const r = c.result;
          if (r.completed) {
            box.append(okBox('备份配置完成。数据同步已按新配置运行。'));
            return;
          }
          const bits = [];
          if (r.settings_applied) bits.push('新的仓库设置已保存。');
          if (r.created_repository_url) bits.push('GitHub 仓库已创建：' + r.created_repository_url);
          const msg = (r.error && (r.error.user_message || r.error.message)) || '最后一步没有完成。';
          const hint = (r.error && r.error.user_hint) || '';
          box.append(errBox(bits.join('') + '尚未完成：' + msg, hint));
          box.append(Object.assign(document.createElement('p'), { className: 'sync-onboarding-note',
            textContent: r.settings_applied
              ? '配置已生效但首次同步没有完成；回到同步页面后可点击「立即同步」重试。'
              : '配置没有保存。若上方显示仓库已创建，可重新打开向导，改用「连接已有仓库」并填入该地址继续。' }));
        },
        canNext() { return null; },
        primaryLabel() { return '关闭向导'; },
      },
    ];

    const body = document.createElement('div');
    const { dlg, foot } = UI.baseDialog('备份配置向导', body);
    dlg.classList.add('sync-onboarding');
    const counter = document.createElement('div'); counter.className = 'sync-onboarding-counter';
    const content = document.createElement('div'); content.className = 'sync-onboarding-steps';
    const notice = document.createElement('p'); notice.className = 'sync-onboarding-note';
    const errorBox = document.createElement('div'); errorBox.className = 'sync-onboarding-error'; errorBox.hidden = true;
    body.append(counter, content, notice, errorBox);
    const back = UI.mkBtn('上一步', 'btn-outline');
    const next = UI.mkBtn('下一步');
    const cancel = UI.mkBtn('取消', 'btn-outline');
    for (const b of [back, next, cancel]) b.type = 'button';
    foot.append(cancel, back, next);
    // 忙碌期间 Esc 与取消都不可绕过
    dlg.addEventListener('cancel', e => { if (ctx.busy) e.preventDefault(); });
    cancel.onclick = () => { if (!ctx.busy) dlg.close(); };
    back.onclick = () => {
      if (ctx.busy) return;
      ctx.stepIndex = Math.max(0, ctx.stepIndex - 1);
      draw();
    };
    next.onclick = () => { if (!ctx.busy) advance(); };
    dlg.addEventListener('close', () => {
      dlg.remove();
      active = false;
      if (onDone) { const done = onDone; onDone = null; done(); }
    });

    function visible() { return STEPS.filter(s => s.when(ctx)); }
    function current() {
      const list = visible();
      ctx.stepIndex = Math.max(0, Math.min(ctx.stepIndex, list.length - 1));
      return list[ctx.stepIndex];
    }

    function draftPayload(c) {
      const d = c.draft;
      const payload = { mode: c.mode, device: d.device, interval: d.interval, auto: d.auto };
      if (c.mode === 'existing') {
        payload.remote = d.remote; payload.branch = d.branch; payload.github_account = d.github_account;
      } else {
        payload.repository = d.repository;
        payload.github_account = (c.github && c.github.verified_as) || d.github_account;
      }
      return payload;
    }

    function changingRepo(c) {
      return c.mode === 'existing' &&
        ((c.draft.remote && c.draft.remote !== c.opened.remote) || c.draft.branch !== c.opened.branch);
    }

    function showApiError(e) {
      console.warn('[onboarding]', e && e.code, e && e.message);
      errorBox.replaceChildren();
      errorBox.append(Object.assign(document.createElement('strong'),
        { textContent: (e && (e.user_message || e.message)) || '操作未完成' }));
      if (e && e.user_hint) errorBox.append(Object.assign(document.createElement('p'), { textContent: e.user_hint }));
      errorBox.hidden = false;
    }

    function updateNav() {
      const step = current();
      next.disabled = ctx.busy || !!step.canNext(ctx);
      back.disabled = ctx.busy || ctx.stepIndex === 0;
      cancel.disabled = ctx.busy;
      next.textContent = step.primaryLabel ? step.primaryLabel(ctx) : '下一步';
    }

    async function draw() {
      const step = current();
      errorBox.hidden = true;
      notice.textContent = '';
      counter.textContent = '第 ' + (ctx.stepIndex + 1) + ' 步，共 ' + visible().length + ' 步 · ' + step.title;
      content.replaceChildren();
      updateNav();
      try {
        await step.enter(ctx);
      } catch (e) {
        showApiError(e);
        const retry = UI.mkBtn('重试', 'btn-outline'); retry.type = 'button';
        retry.onclick = () => { if (!ctx.busy) draw(); };
        content.append(retry);
        return;
      }
      step.render(ctx, content);
      updateNav();
    }

    async function advance() {
      const step = current();
      const blocked = step.canNext(ctx);
      if (blocked) { showApiError({ message: blocked }); return; }
      if (step.onPrimary) {
        await withBusy(() => step.onPrimary(ctx));
      } else if (ctx.stepIndex + 1 < visible().length) {
        ctx.stepIndex++;
        draw();
      } else {
        dlg.close();
      }
    }

    async function withBusy(fn, message) {
      if (ctx.busy) return;
      ctx.busy = true;
      back.disabled = next.disabled = cancel.disabled = true;
      notice.textContent = message || '正在处理，请稍候…';
      try {
        await fn();
      } catch (e) {
        showApiError(e);
      } finally {
        ctx.busy = false;
        notice.textContent = '';
        updateNav();
      }
    }

    // ---------- 小构件 ----------

    function actionBtn(label, fn, cls) {
      const b = UI.mkBtn(label, cls || '');
      b.type = 'button';
      b.onclick = () => withBusy(fn,
        label === '安装 Git' || label === '安装登录组件' ? '正在下载并安装，可能需要几分钟…' : undefined);
      return b;
    }

    function officialLink() {
      const a = document.createElement('a');
      a.href = 'https://git-scm.com/download/win';
      a.target = '_blank'; a.rel = 'noopener';
      a.textContent = '打开 Git 官网下载页';
      return a;
    }

    function okBox(text) {
      const box = document.createElement('div'); box.className = 'sync-onboarding-ok';
      box.textContent = text;
      return box;
    }

    function errBox(text, hint) {
      const box = document.createElement('div'); box.className = 'sync-onboarding-error';
      box.append(Object.assign(document.createElement('strong'), { textContent: text }));
      if (hint) box.append(Object.assign(document.createElement('p'), { textContent: hint }));
      return box;
    }

    function methodOption(value, title, desc) {
      const label = document.createElement('label'); label.className = 'sync-onboarding-option';
      const radio = document.createElement('input');
      radio.type = 'radio'; radio.name = 'sync-onboarding-method'; radio.checked = ctx.mode === value;
      radio.onchange = () => {
        if (!radio.checked || ctx.mode === value) return;
        invalidateCheck(ctx);
        ctx.mode = value;
      };
      const wrap = document.createElement('span');
      wrap.append(Object.assign(document.createElement('strong'), { textContent: title }),
        Object.assign(document.createElement('small'), { textContent: desc }));
      label.append(radio, wrap);
      return label;
    }

    function sourceChoice(value, title, desc) {
      const label = document.createElement('label'); label.className = 'sync-onboarding-option';
      const radio = document.createElement('input');
      radio.type = 'radio'; radio.name = 'sync-onboarding-source'; radio.checked = ctx.source === value;
      radio.onchange = () => { if (radio.checked) { ctx.source = value; updateNav(); } };
      const wrap = document.createElement('span');
      wrap.append(Object.assign(document.createElement('strong'), { textContent: title }),
        Object.assign(document.createElement('small'), { textContent: desc }));
      label.append(radio, wrap);
      return label;
    }

    function inputRow(labelText, value, placeholder, onInput, type) {
      const label = document.createElement('label');
      label.append(Object.assign(document.createElement('span'), { textContent: labelText }));
      const input = document.createElement('input');
      input.value = value == null ? '' : value;
      if (placeholder) input.placeholder = placeholder;
      if (type) input.type = type;
      input.autocomplete = 'off';
      input.oninput = () => onInput(input.value);
      label.append(input);
      return label;
    }

    async function selectAccount(c, name) {
      c.draft.github_account = name;
      invalidateCheck(c);
      await withBusy(async () => {
        c.github = await Sync.api('GET', '/onboarding/github?account=' + encodeURIComponent(name));
        if (c.github.verified_as) c.draft.github_account = c.github.verified_as;
        draw();
      });
    }

    dlg.showModal();
    draw();
  }

  return { open };
})();
