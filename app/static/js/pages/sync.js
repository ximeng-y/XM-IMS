/* JSONL 三方结果的 GUI 展示；所有写入均先落冲突草稿。
   labels 只用于把后端返回的记录键翻成中文说明：库表列名（created_at、spu_id 等）不进界面，
   未收录的键按「该记录的一项内容」笼统呈现，不显示原始键名。 */
"use strict";
let syncState=null, conflictSession=null, revision=0, offset=0, total=0, changesOffset=0, settingsLoaded=false, savedSettings=null;
const limit=20;
const $s=id=>document.getElementById(id);
const text=(node,value)=>{node.textContent=value;};
const labels={name:'名称',type:'类型',scale:'小数位数',allow_negative:'允许负数',formula_json:'公式',formula:'公式',qty:'数量',unit_price:'单价',value_int:'数值',value_text:'文本值',created_at:'创建时间',updated_at:'修改时间',actor:'操作者',note:'备注',batch_no:'批次',supplier:'供应商',channel:'渠道',customer:'客户',definition:'字段定义',values:'关联值',seq:'组内序号',spu_id:'所属 SPU',item_id:'所属 SKU',field_id:'自定义字段',entity_type:'对象类型',entity_id:'事件发生时编号（不可用于查找当前对象）',entity_uid:'对象稳定标识',detail:'审计详情'};
// 库表里的金额列以分为单位存储，直接输出会与「单价」标签的元单位对不上，这里统一换算
const money=(x)=>x===null||x===undefined?'空值':(Number(x)/100).toFixed(2)+' 元';
function printable(v){if(v===null||v===undefined)return '不存在／已删除';if(typeof v!=='object')return String(v);return Object.entries(v).map(([k,x])=>k==='unit_price'?labels[k]+'：'+money(x):(labels[k]||'该记录的一项内容')+'：'+(typeof x==='object'?JSON.stringify(x,null,2):String(x??'空值'))).join('\n');}
async function action(button,fn){await UI.withBusy(button,async()=>{try{await fn();}catch(e){UI.fail(e.user_message||'同步操作暂未完成',{title:UI.codeTitle(e.code)||'同步操作暂未完成',hint:e.user_hint||''});}await refresh().catch(showRefreshError);});}
function showLocalOnly(){
  text($s('syncHeading'),'同步管理仅限本机');
  text($s('syncMessage'),'请在运行本程序的电脑上打开此页面来管理数据同步。');
  document.querySelectorAll('.sync-page button,.sync-page input').forEach(el=>{el.disabled=true;});
  $s('settingsPanel').hidden=true;$s('conflictPanel').hidden=true;$s('initializeBox').hidden=true;
  $s('offlineEnter').hidden=true;$s('enterApp').hidden=false;$s('enterApp').textContent='返回应用';
}
function showRefreshError(error){
  if(!Sync.local){showLocalOnly();return;}
  text($s('syncHeading'),'同步状态暂不可用');
  text($s('syncMessage'),(error.user_message||'连接同步服务暂未完成')+'。请确认程序正在运行，页面会自动重试。');
  const badge=$s('syncStatus');if(badge)badge.textContent='同步状态暂不可用';
}
async function refresh(){
  if(!Sync.local){showLocalOnly();return;}
  syncState=await Sync.api('GET','/status');Sync.render(syncState);
  if(!settingsLoaded){
    const settings=await Sync.api('GET','/settings');savedSettings=settings;
    $s('remoteUrl').value=settings.remote;$s('dataBranch').value=settings.branch;$s('deviceName').value=settings.device;$s('syncInterval').value=settings.interval;$s('autoSync').checked=settings.auto;
    settingsLoaded=true;document.querySelectorAll('#syncSettings input,#syncSettings button').forEach(el=>{el.disabled=false;});
  }
  text($s('syncHeading'),Sync.label(syncState));
  let message=syncState.error?.user_message||({setup:'选择开始方式，即可准备第一间店铺。',checking:'正在准备数据，请稍候…',behind:'远端有更新。点击立即同步后重新核对并更新。',conflict:'双方都有修改。点击立即同步进入冲突处理。',synced:'本机与远端的备份已同步。',offline:'连接暂未完成。可重试连接，或按当前状态选择离线进入。',initialize:'仓库准备好后，即可继续首次备份。',binding:'请核对本机与仓库中的数据，在高级维护中选择使用的数据。',local:'数据已保存在本机。'}[syncState.state]||'');
  if(syncState.legacy_present && syncState.state==='setup')message+=' 检测到旧数据库，原文件已保留。';
  text($s('syncMessage'),message);
  $s('enterApp').hidden=!syncState.business_ready;
  const first=await Sync.api('GET','/onboarding/first-use/status');
  const firstEntry=['choose_source','create_shop','restore_pending'].includes(first.phase)||(first.phase==='waiting'&&!first.has_local_data);
  const firstBackup=first.has_local_data&&!syncState.business_ready&&['initialize','binding'].includes(syncState.state);
  $s('continueFirstUse').hidden=!firstEntry;
  if(firstEntry)$s('enterApp').hidden=true;
  $s('offlineEnter').hidden=syncState.state!=='offline'||!first.has_local_data;
  $s('offlineEnter').disabled=syncState.busy;
  $s('initializeBox').hidden=!['setup','initialize','binding'].includes(syncState.state);
  $s('useRemote').hidden=!syncState.configured;
  $s('syncNow').hidden=firstEntry||firstBackup;$s('checkNow').hidden=firstEntry||firstBackup;$s('startOnboarding').hidden=firstEntry;
  text($s('startOnboarding'),firstBackup?'继续配置备份':'配置备份');
  $s('syncNow').disabled=syncState.busy;$s('checkNow').disabled=syncState.busy;
  $s('startOnboarding').disabled=syncState.busy;$s('restartOnboarding').disabled=syncState.busy;
  updateConnectionState();
  const active=syncState.session&&!syncState.business_ready;
  $s('conflictPanel').hidden=!active;
  if(active&&conflictSession!==syncState.session){conflictSession=syncState.session;offset=0;try{await loadConflicts();}catch(error){conflictSession=null;throw error;}}
}
function settingsValues(){return {remote:$s('remoteUrl').value.trim(),branch:$s('dataBranch').value.trim(),device:$s('deviceName').value.trim(),interval:Number($s('syncInterval').value),auto:$s('autoSync').checked};}
function settingsDirty(){const values=settingsValues();return !savedSettings||!$s('syncInterval').value.trim()||Object.keys(values).some(key=>values[key]!==savedSettings[key]);}
function updateConnectionState(){const dirty=settingsDirty();$s('testConnection').disabled=!settingsLoaded||dirty||!!syncState?.busy;text($s('connectionSaveHint'),dirty?'先保存后测试':'');}
async function loadConflicts(){
  if(!conflictSession)return;
  const p=await Sync.api('GET','/conflicts/'+conflictSession+'?offset='+offset+'&limit='+limit+'&unresolved='+$s('onlyUnresolved').checked);
  revision=p.revision;total=p.total;
  text($s('conflictSummary'),'未解决 '+p.unresolved+' 项；当前筛选共 '+p.total+' 项；候选自动修改 '+p.changes_total+' 条。');
  // 渲染期任何异常都不能让 revision 与服务端失同步：逐张卡片兜底，坏掉的卡片显示原因
  $s('conflictList').replaceChildren(...p.conflicts.map(c=>{try{return card(c);}catch(error){console.error(error);const box=document.createElement('section');box.className='conflict-card';const h=document.createElement('h3');h.textContent='冲突项 '+(c&&c.id||'')+' 无法显示';const pre=document.createElement('pre');pre.textContent=error.message||String(error);box.append(h,pre);return box;}}));
  await loadChanges();
  $s('previousConflicts').disabled=offset===0;
  $s('nextConflicts').disabled=offset+limit>=total;
  $s('applyConflict').disabled=!p.validated;
}
function card(c){
  const box=document.createElement('section');box.className='conflict-card';
  const title=document.createElement('h3');title.textContent=(c.resolved?'✓ ':'待处理 · ')+(c.label||'冲突项')+' · '+(labels[c.field]||({'$value':'字段值','$inventory':'库存复核','$record':'整条记录','$placement':'归属与编号','$definition_and_values':'字段定义及关联值','$idempotency':'重复操作','$unique_name':'唯一名称','$references':'关联记录','$immutable':'不可变历史'}[c.field]||'冲突项'));box.append(title);
  const grid=document.createElement('div');grid.className='conflict-grid';
  ['共同版本','本地','远端','处理结果'].forEach((name,i)=>{const col=document.createElement('div');const h=document.createElement('strong');h.textContent=name;const pre=document.createElement('pre');pre.textContent=c.display_values&&i<3?String(c.display_values[i]):i===3&&c.display_result!==undefined?c.display_result:printable([c.base,c.local,c.remote,c.result][i]);col.append(h,pre);grid.append(col);});box.append(grid);
  if(c.kind==='inventory'){const p=document.createElement('p');p.textContent='叠加候选库存：'+c.proposed_qty+'。请选择或输入最终目标库存；必要时追加调整流水。';box.append(p);}
  if(c.affected){const p=document.createElement('p');p.textContent='涉及 '+c.affected+' 条关联记录。';const b=document.createElement('button');b.className='btn-outline btn-sm';b.textContent='查看关联记录';b.onclick=()=>showDetails(c.id);box.append(p,b);}
  const controls=document.createElement('div');controls.className='sync-actions';
  const choices=c.kind==='immutable'?[['base','保留共同版本']]:[['local','采用本地'],['remote','采用远端'],['base','采用共同版本']];
  for(const [source,label] of choices){const button=document.createElement('button');button.className='btn-outline btn-sm';button.textContent=label;const reason=c.disabled_sources?.[source];if(reason){button.disabled=true;button.title=reason;button.textContent+='（不可用）';}button.onclick=()=>saveChoice(button,c.id,{source});controls.append(button);}
  if(c.editable){const input=document.createElement('input');input.placeholder=c.kind==='inventory'?'最终目标库存':'输入处理后的值';input.setAttribute('aria-label','手动处理值');const b=document.createElement('button');b.textContent='采用输入值';b.className='btn-sm';b.onclick=()=>saveChoice(b,c.id,{source:'custom',value:input.value});controls.append(input,b);}
  box.append(controls);if(c.kind==='name'){const note=document.createElement('p');note.className='muted';note.textContent='不存在此对象的版本不能提供名称；请至少为其中一条记录输入不同名称，以便同时保留。';box.append(note);}return box;
}
async function saveChoice(button,id,choice){await action(button,async()=>{await Sync.api('PATCH','/conflicts/'+conflictSession,{revision,choices:{[id]:choice}});await loadConflicts();$s('mergePreview').replaceChildren();});}
window.PAGE_INIT=async()=>{
  if(!Sync.local){showLocalOnly();return;}
  document.querySelectorAll('#syncSettings input,#syncSettings button').forEach(el=>{el.disabled=true;});
  $s('syncSettings').oninput=updateConnectionState;
  $s('syncSettings').onchange=updateConnectionState;
  $s('syncSettings').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{savedSettings=await Sync.api('PATCH','/settings',settingsValues());UI.ok('设置已保存');});};
  $s('testConnection').onclick=e=>{if(settingsDirty())return;action(e.currentTarget,async()=>{await Sync.api('POST','/test-connection');UI.ok('已连接到备份仓库');});};
  $s('syncNow').onclick=e=>action(e.currentTarget,async()=>{await Sync.api('POST','/run',{synchronize:true});conflictSession=null;});
  $s('checkNow').onclick=e=>action(e.currentTarget,()=>Sync.api('POST','/run',{synchronize:false}));
  $s('startOnboarding').onclick=()=>openOnboarding('backup');
  $s('restartOnboarding').onclick=()=>openOnboarding('maintenance');
  $s('offlineEnter').onclick=e=>action(e.currentTarget,async()=>{await Sync.api('POST','/offline');location.href='/';});
  for(const [id,source] of [['useLocal','local'],['useRemote','remote']])$s(id).onclick=e=>action(e.currentTarget,async()=>{if(await UI.confirmDialog({title:'确认初始化来源',bodyHtml:source==='local'?'将使用本地数据（没有数据时新建）。如远端已有数据，其当前内容将被本地版本替换，旧版本保留在历史中。':'将从远端恢复整套数据，当前本地数据会先保留副本。',confirmText:'确认使用',danger:true}))await Sync.api('POST','/initialize',{source,confirm:true});});
  $s('previousChanges').onclick=e=>action(e.currentTarget,async()=>{changesOffset=Math.max(0,changesOffset-limit);await loadChanges();});
  $s('nextChanges').onclick=e=>action(e.currentTarget,async()=>{changesOffset+=limit;await loadChanges();});
  $s('onlyUnresolved').onchange=()=>{offset=0;loadConflicts().catch(e=>UI.fail(e.message));};
  $s('previousConflicts').onclick=e=>action(e.currentTarget,async()=>{offset=Math.max(0,offset-limit);await loadConflicts();});
  $s('nextConflicts').onclick=e=>action(e.currentTarget,async()=>{offset+=limit;await loadConflicts();});
  $s('cancelConflict').onclick=e=>action(e.currentTarget,async()=>{await Sync.api('POST','/conflicts/'+conflictSession+'/cancel');location.href='/';});
  $s('validateConflict').onclick=e=>action(e.currentTarget,async()=>{const p=await Sync.api('POST','/conflicts/'+conflictSession+'/validate',{revision});const el=document.createElement('pre');el.textContent='校验通过。候选修改 '+p.changes+' 条。\n编号变化：\n'+(p.renumber.length?p.renumber.map(r=>(r.label||r.table)+'：'+r.old+' → '+r.new).join('\n'):'无');$s('mergePreview').replaceChildren(el);await loadConflicts();});
  $s('applyConflict').onclick=e=>action(e.currentTarget,async()=>{if(await UI.confirmDialog({title:'确认应用合并结果',bodyHtml:'请确认修改总览、编号变化和最终库存。推送成功后将整套替换本地数据库。',confirmText:'确认并应用',danger:true})){await Sync.api('POST','/conflicts/'+conflictSession+'/apply',{revision,confirm:true});conflictSession=null;}});
  await refresh().catch(showRefreshError);setInterval(()=>refresh().catch(showRefreshError),5000);
};

async function showDetails(id){
  let start=0;
  const body=document.createElement('div');const {dlg,foot}=UI.baseDialog('关联记录对照',body);
  const prev=UI.mkBtn('上一页','btn-outline'),next=UI.mkBtn('下一页','btn-outline'),close=UI.mkBtn('关闭','btn-outline');foot.append(prev,next,close);
  const load=async()=>{try{const p=await Sync.api('GET','/conflicts/'+conflictSession+'/details?conflict_id='+encodeURIComponent(id)+'&offset='+start+'&limit=20');body.replaceChildren(...p.rows.map(r=>{const div=document.createElement('section');const h=document.createElement('h3');h.textContent=r.label;const pre=document.createElement('pre');pre.textContent='共同版本：\n'+printable(r.base)+'\n本地：\n'+printable(r.local)+'\n远端：\n'+printable(r.remote);div.append(h,pre);return div;}));prev.disabled=start===0;next.disabled=start+20>=p.total;}catch(e){UI.fail(e.user_message||e.message);}};
  prev.onclick=()=>{start=Math.max(0,start-20);load();};next.onclick=()=>{start+=20;load();};close.onclick=()=>dlg.close();dlg.addEventListener('close',()=>dlg.remove());dlg.showModal();await load();
}

async function loadChanges(){
  if(!conflictSession)return;
  const p=await Sync.api('GET','/conflicts/'+conflictSession+'/changes?offset='+changesOffset+'&limit='+limit);
  $s('automaticChanges').replaceChildren(...p.rows.map(c=>{const el=document.createElement('pre');el.textContent=c.label+'\n共同版本：'+printable(c.base)+'\n候选结果：'+(c.note||printable(c.result));return el;}));
  $s('previousChanges').disabled=changesOffset===0;$s('nextChanges').disabled=changesOffset+limit>=p.total;
}

// 打开备份配置向导：草稿以已保存设置为准，不用未保存的表单内容隐式应用。
async function openOnboarding(intent){
  let settings;
  try{settings=await Sync.api('GET','/settings');}
  catch(e){UI.fail(e.user_message||'读取备份设置暂未完成');return;}
  BackupOnboarding.open({intent,settings,request:Sync.api,onComplete:reloadSettings,
    onCancel:context=>context.result?.settings_applied?reloadSettings():refresh().catch(showRefreshError)});
}
// 向导结束后明确重读设置表单：重置 settingsLoaded，随后刷新同步状态。
async function reloadSettings(){settingsLoaded=false;await refresh().catch(showRefreshError);}
