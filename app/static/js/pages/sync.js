/* JSONL 三方结果的 GUI 展示；所有写入均先落冲突草稿。 */
"use strict";
let syncState=null, conflictSession=null, revision=0, offset=0, total=0, changesOffset=0, settingsLoaded=false;
const limit=20;
const $s=id=>document.getElementById(id);
const text=(node,value)=>{node.textContent=value;};
const labels={name:'名称',type:'类型',scale:'小数位数',allow_negative:'允许负数',formula_json:'公式',formula:'公式',qty:'数量',unit_price:'单价（分）',value_int:'数值编码',value_text:'文本值',created_at:'创建时间',updated_at:'修改时间',actor:'操作者',note:'备注',batch_no:'批次',supplier:'供应商',channel:'渠道',customer:'客户',definition:'字段定义',values:'关联值',seq:'组内序号',spu_id:'所属 SPU',item_id:'所属 SKU',field_id:'自定义字段',entity_type:'对象类型',entity_id:'事件发生时编号（不可用于查找当前对象）',entity_uid:'对象稳定标识',detail:'审计详情'};
function printable(v){if(v===null||v===undefined)return '不存在／已删除';if(typeof v!=='object')return String(v);return Object.entries(v).map(([k,x])=>(labels[k]||k)+'：'+(typeof x==='object'?JSON.stringify(x,null,2):String(x??'空值'))).join('\n');}
async function action(button,fn){await UI.withBusy(button,async()=>{try{await fn();}catch(e){UI.fail(e.message,{title:e.code||'SYNC_ERROR'});}await refresh().catch(showRefreshError);});}
function showLocalOnly(){
  text($s('syncHeading'),'同步管理仅限本机');
  text($s('syncMessage'),'请在运行应用的电脑上通过 localhost 或 127.0.0.1 打开此页面，管理数据同步。');
  document.querySelectorAll('.sync-page button,.sync-page input').forEach(el=>{el.disabled=true;});
  $s('settingsPanel').hidden=true;$s('conflictPanel').hidden=true;$s('initializeBox').hidden=true;
  $s('offlineEnter').hidden=true;$s('enterApp').hidden=false;$s('enterApp').textContent='返回应用';
}
function showRefreshError(error){
  if(!Sync.local){showLocalOnly();return;}
  text($s('syncHeading'),'同步状态暂不可用');
  text($s('syncMessage'),(error.message||'连接同步服务失败')+'。请检查后端服务，页面会自动重试。');
  const badge=$s('syncStatus');if(badge)badge.textContent='同步状态暂不可用';
}
async function refresh(){
  if(!Sync.local){showLocalOnly();return;}
  syncState=await Sync.api('GET','/status');Sync.render(syncState);
  if(!settingsLoaded){
    const settings=await Sync.api('GET','/settings');
    $s('remoteUrl').value=settings.remote;$s('dataBranch').value=settings.branch;$s('deviceName').value=settings.device;$s('syncInterval').value=settings.interval;$s('autoSync').checked=settings.auto;
    settingsLoaded=true;document.querySelectorAll('#syncSettings input,#syncSettings button').forEach(el=>{el.disabled=false;});
  }
  text($s('syncHeading'),Sync.label(syncState));
  let message=syncState.error?.message||({setup:'首次运行：请新建数据集，或配置仓库后从远端恢复。',checking:'检查完成前业务接口保持关闭。',behind:'远端有更新。点击立即同步后重新比较并更新。',conflict:'双方都有修改。点击立即同步进入冲突处理。',synced:'本地已确认与远端同步。',offline:'无法确认远端状态。可重试连接；没有已知冲突时可主动离线进入。',initialize:'远端尚未初始化，请确认使用本地数据。',binding:'本地与远端没有共同基线，请明确选择初始化来源。',local:'数据已保存到本机；配置私有 Git 仓库后可同步。'}[syncState.state]||'');
  if(syncState.legacy_present && syncState.state==='setup')message+=' 检测到旧数据库：文件原样保留，本版本不自动迁移。';
  text($s('syncMessage'),message);
  $s('enterApp').hidden=!syncState.business_ready;
  $s('offlineEnter').hidden=syncState.state!=='offline';
  $s('offlineEnter').disabled=syncState.busy;
  $s('initializeBox').hidden=!['setup','initialize','binding'].includes(syncState.state);
  $s('useRemote').hidden=!syncState.configured;
  $s('syncNow').disabled=syncState.busy;
  $s('checkNow').disabled=syncState.busy;
  text($s('credentialStatus'),'读取：'+(syncState.read_verified?'已验证':'未验证')+'；写入：'+(syncState.write_verified?'已通过实际推送验证':'待首次实际推送验证'));
  text($s('retentionInfo'),'本地同步数据占用约 '+((syncState.storage_bytes||0)/1048576).toFixed(1)+' MB；未上传提交：'+syncState.pending_commits+'。'+(syncState.retention_exception?'正在保留必要的未上传历史或冲突基线，暂时可能超过三份。':'当前采用三份快照与浅克隆保留策略。')+(syncState.cleanup_pending?'历史收缩待重试。':''));
  $s('snapshotList').replaceChildren(...syncState.snapshots.slice().reverse().map(h=>{const li=document.createElement('li');li.textContent=h.slice(0,16);return li;}));
  const active=syncState.session&&!syncState.business_ready;
  $s('conflictPanel').hidden=!active;
  if(active&&conflictSession!==syncState.session){conflictSession=syncState.session;offset=0;try{await loadConflicts();}catch(error){conflictSession=null;throw error;}}
  if(!syncState.configured)$s('settingsPanel').open=true;
}
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
  const title=document.createElement('h3');title.textContent=(c.resolved?'✓ ':'待处理 · ')+(c.label||c.key)+' · '+(labels[c.field]||({'$value':'字段值','$inventory':'库存复核','$record':'整条记录','$placement':'归属与编号','$definition_and_values':'字段定义及关联值','$idempotency':'幂等操作','$unique_name':'唯一名称','$references':'关联记录','$immutable':'不可变历史'}[c.field]||c.field));box.append(title);
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
  $s('syncSettings').onsubmit=e=>{e.preventDefault();action(e.submitter,async()=>{await Sync.api('PATCH','/settings',{remote:$s('remoteUrl').value.trim(),branch:$s('dataBranch').value.trim(),device:$s('deviceName').value.trim(),interval:Number($s('syncInterval').value),auto:$s('autoSync').checked});UI.ok('设置已保存');});};
  $s('testConnection').onclick=e=>action(e.currentTarget,async()=>{await Sync.api('POST','/test-connection');UI.ok('读取连接通过；写权限以实际推送为准');});
  $s('syncNow').onclick=e=>action(e.currentTarget,async()=>{await Sync.api('POST','/run',{synchronize:true});conflictSession=null;});
  $s('checkNow').onclick=e=>action(e.currentTarget,()=>Sync.api('POST','/run',{synchronize:false}));
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
  const load=async()=>{try{const p=await Sync.api('GET','/conflicts/'+conflictSession+'/details?conflict_id='+encodeURIComponent(id)+'&offset='+start+'&limit=20');body.replaceChildren(...p.rows.map(r=>{const div=document.createElement('section');const h=document.createElement('h3');h.textContent=r.label;const pre=document.createElement('pre');pre.textContent='共同版本：\n'+printable(r.base)+'\n本地：\n'+printable(r.local)+'\n远端：\n'+printable(r.remote);div.append(h,pre);return div;}));prev.disabled=start===0;next.disabled=start+20>=p.total;}catch(e){UI.fail(e.message);}};
  prev.onclick=()=>{start=Math.max(0,start-20);load();};next.onclick=()=>{start+=20;load();};close.onclick=()=>dlg.close();dlg.addEventListener('close',()=>dlg.remove());dlg.showModal();await load();
}

async function loadChanges(){
  if(!conflictSession)return;
  const p=await Sync.api('GET','/conflicts/'+conflictSession+'/changes?offset='+changesOffset+'&limit='+limit);
  $s('automaticChanges').replaceChildren(...p.rows.map(c=>{const el=document.createElement('pre');el.textContent=c.label+'\n共同版本：'+printable(c.base)+'\n候选结果：'+(c.note||printable(c.result));return el;}));
  $s('previousChanges').disabled=changesOffset===0;$s('nextChanges').disabled=changesOffset+limit>=p.total;
}
