/* 同步状态提示：运行中仅提示，保留当前页面与编辑能力。 */
"use strict";
window.Sync = (() => {
  const names = {checking:"正在检查同步",setup:"需要初始化",local:"已保存本地",initialize:"等待初始化仓库",binding:"请选择数据来源",synced:"已同步远端",behind:"远端有更新",conflict:"有数据冲突",offline:"离线",blocked:"同步需要处理"};
  async function api(method, endpoint, body) {
    const response = await fetch('/api/v1/sync' + endpoint, {method, headers:{'Content-Type':'application/json','X-XM-Sync':'1'}, body:body === undefined ? undefined : JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) {const error = new Error(data.message || '同步请求失败'); error.code = data.code; throw error;}
    return data;
  }
  function label(s) {return s.dirty && s.business_ready ? (s.state === 'behind' || s.state === 'conflict' ? names[s.state] + ' · 本地有修改' : '已保存本地 · 待同步') : names[s.state] || s.state;}
  function render(s) {
    const box = document.getElementById('syncStatus');
    if (box) {box.textContent = label(s); box.dataset.state = s.state;}
  }
  async function poll(){try {render(await api('GET','/status'));} catch (_) {const el=document.getElementById('syncStatus');if(el)el.textContent='同步状态暂不可用';}}
  async function ensureReady(){
    const s=await api('GET','/status'); render(s);
    if(!s.business_ready){location.replace('/sync');return false;}
    setInterval(poll, 5000);return true;
  }
  return {api, label, render, ensureReady};
})();
