/* 同步状态提示：运行中仅提示，保留当前页面与编辑能力。 */
"use strict";
window.Sync = (() => {
  const names = {checking:"正在检查同步",setup:"需要初始化",local:"已保存本地",initialize:"等待初始化仓库",binding:"请选择数据来源",synced:"已同步远端",behind:"远端有更新",conflict:"有数据冲突",offline:"离线",blocked:"同步需要处理"};
  let local = ['localhost','127.0.0.1','[::1]','::1'].includes(location.hostname);
  function localOnly() {
    local = false;
    const box = document.getElementById('syncStatus');
    if (box) {box.textContent = '同步管理仅限本机'; box.removeAttribute('href'); box.removeAttribute('data-state');}
    const link = document.querySelector('[data-nav="sync"]');
    if (link) link.hidden = true;
  }
  if (!local) localOnly();
  async function api(method, endpoint, body) {
    const response = await fetch('/api/v1/sync' + endpoint, {method, headers:{'Content-Type':'application/json','X-XM-Sync':'1'}, body:body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) {if (data.code === 'SYNC_LOCAL_ONLY') localOnly(); const error = new Error(data.user_message || data.message || '同步请求失败'); error.code = data.code; error.user_message = data.user_message; error.user_hint = data.user_hint; throw error;}
    return data;
  }
  function label(s) {
    if (s.business_ready && !s.configured) return '已保存在本机';
    return s.dirty && s.business_ready ? (s.state === 'behind' || s.state === 'conflict' ? names[s.state] + ' · 本地有修改' : '已保存本地 · 待同步') : names[s.state] || '同步需要处理';
  }
  function render(s) {
    if (!local) {localOnly(); return;}
    const box = document.getElementById('syncStatus');
    if (box) {box.textContent = label(s); box.dataset.state = s.state;}
  }
  async function poll(){if (!local) return; try {render(await api('GET','/status'));} catch (_) {if (!local) return; const el=document.getElementById('syncStatus');if(el)el.textContent='同步状态暂不可用';}}
  async function ensureReady(){
    // 局域网只使用业务接口；启动和写入门禁仍由后端强制执行。
    if (!local) return true;
    let s;
    try {s=await api('GET','/status');} catch (error) {if (!local) return true; throw error;}
    render(s);
    if(!s.business_ready){
      const first=await api('GET','/onboarding/first-use/status');
      if (!local) return true;
      const onboarding=['choose_source','create_shop','restore_pending'].includes(first.phase) ||
        (first.phase==='waiting'&&!first.has_local_data);
      location.replace(onboarding?'/onboarding':'/sync');return false;
    }
    setInterval(poll, 5000);return true;
  }
  return {api, label, render, ensureReady, get local() {return local;}};
})();
