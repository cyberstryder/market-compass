/* Private native workflow controls. Sender activation is guarded; credentials are never displayed. */
(()=>{
 let config=null,routing=null;
 async function api(path,body){
  const r=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const data=await r.json();if(!r.ok)throw Error(typeof data.detail==='string'?data.detail:'Request rejected ('+r.status+'). Check values and sign in.');return data;
 }
 const params=()=>new URLSearchParams({ticker:$('#native-ticker').value.trim(),start:$('#native-start').value,end:$('#native-end').value,week:$('#native-week').value,limit:'100'});
 function differences(pair){return tag(pair.status)+(Object.keys(pair.differences||{}).length?'<details><summary>Show field differences</summary><pre>'+esc(JSON.stringify(pair.differences,null,2))+'</pre></details>':'');}
 function download(){ $('#native-report-download').href='/api/native/report?'+params()+'&download=true'; }
 for(const id of ['native-ticker','native-start','native-end','native-week'])$('#'+id).onchange=download;
 $('#native-report-load').onclick=async()=>{
  const button=$('#native-report-load'),target=$('#native-report-result');button.disabled=true;target.textContent='Calculating native observations…';download();
  try{
   const r=await api('/api/native/report?'+params()),m=r.morning,s=r.smoothers;
   const cut=Object.entries(m.truncated).filter(([,v])=>v).map(([k])=>'Morning '+k).concat(Object.entries(s.truncated).filter(([,v])=>v).map(([k])=>'Smoothers '+k));
   target.innerHTML='<p>Calculated '+when(r.at)+'. '+esc(m.basis)+'</p>'+(cut.length?'<p><strong>Truncated: '+esc(cut.join(', '))+'. Narrow filters before comparing cohorts.</strong></p>':'')+
    '<h3>Morning native stock and options</h3>'+table(['Signal','Intake','Coverage','Source fields','Contract / source comparison','Option samples'],m.signals.map(x=>[
      esc(x.ticker)+' · '+when(x.at),esc(x.origins.map(o=>o.origin+' ('+o.packets+' packets; '+o.entry_packets+' entry)').join(', ')||'provenance not retained'),tag(x.coverage.status)+' '+x.coverage.recorded+'/60',differences(x.source_signal)+'<details><summary>Candle comparison: '+esc(x.source_candles.status)+'</summary><pre>'+esc(JSON.stringify(x.source_candles,null,2))+'</pre></details>',esc(x.option.contract?.symbol||'Unavailable')+' · '+tag(x.source_option.status),esc(JSON.stringify(x.option_calculations.sample_counts))]))+
    '<h3>Stock exits — complete shared cohorts only</h3>'+table(['Setup / configuration','Paired / total','Excluded','Rule','Mean stock return'],m.stock_groups.flatMap(g=>g.rules.map(v=>[esc(g.setup)+' '+esc(g.config_id.slice(0,10)),g.paired+'/'+g.signals,g.excluded,esc(v.rule),num(v.mean_return_pct)+'%'])))+
    '<h3>Research outcomes</h3>'+table(['Candidate','Kind','5m','15m','30m','60m','120m','180m'],m.candidates.map(x=>[esc(x.ticker),esc(x.record.kind),...[5,15,30,60,120,180].map(h=>tag(x.outcomes[h].status)+' '+num(x.outcomes[h].return_pct)+'%')]))+
    '<h3>Expected alerts and previews</h3><p>A preview is not a delivered message. Source delivery times remain separate.</p>'+table(['Event','Native intent','Original delivery'],m.delivery.map(x=>[esc(x.event_id),tag(x.native_status),tag(x.source_status)+(x.source_delivered_at_ms?' · '+when(x.source_delivered_at_ms/1000):'')]))+
    m.delivery.map(x=>'<details><summary>Preview '+esc(x.event_id)+'</summary><pre>'+esc(JSON.stringify(x.preview,null,2))+'</pre></details>').join('')+
    '<h3>Source signals without native counterparts</h3>'+table(['Signal','State'],m.source_only.map(x=>[esc(x.id),tag(x.status)]))+
    '<h3>Smoothers native week '+esc(s.week)+'</h3><p>Job '+esc(s.job.state||'not started')+'. '+esc(s.basis)+'</p>'+table(['Ticker','Direction / target','Outcome','Premium model','Source comparison'],s.rows.map(x=>[esc(x.native.ticker),esc(x.native.direction)+' / '+num(x.native.target_price),tag(x.native.status),esc(JSON.stringify(x.native.premium_model||{})),differences(x.comparison)]))+
    '<p>Original-only weekly records: '+s.source_only.length+'. '+esc(s.delivery_basis)+'</p>'+
    table(['Weekly event','Native intent','Current format','Original delivery'],s.delivery.map(x=>[esc(x.event_id),tag(x.native_status),tag(x.current_format_matches?'matched':'needs review'),tag(x.source_status)]))+
    s.delivery.map(x=>'<details><summary>Weekly preview '+esc(x.event_id)+'</summary><pre>'+esc(JSON.stringify(x.preview,null,2))+'</pre></details>').join('')+
    '<p>Download JSON for candle outcomes, option quotes, configuration/contract comparisons, differences, and source timestamps.</p>';
  }catch(e){target.textContent=e.message;}finally{button.disabled=false;}
 };
 function choose(){const row=config?.current.configs?.find(x=>x.ticker===$('#native-config-ticker').value);if(!row)return;
  for(const k of ['s1','s2','s3','pm'])$('#native-'+k).value=row[k];$('#native-enabled').checked=row.enabled;
 }
 async function loadConfig(){
  const result=await api('/api/native/smoothers/config');config=result;
  $('#native-config-ticker').innerHTML=(result.current.configs||[]).map(x=>'<option>'+esc(x.ticker)+'</option>').join('');
  $('#native-config-history').innerHTML=result.history.map(x=>'<option value="'+esc(x.id)+'">'+esc(x.id.slice(0,12)+' · '+x.reason)+'</option>').join('');
  $('#native-config-status').textContent='Owner: '+(result.current.owner||'awaiting import')+' · '+(result.current.configs||[]).length+' tickers · revision '+(result.current.revision||'none')+'. '+result.basis;
  $('#native-config-save').disabled=result.current.owner!=='compass';$('#native-config-restore').disabled=result.current.owner!=='compass'||!result.history.length;choose();
 }
 $('#native-config-ticker').onchange=choose;
 $('#native-config-load').onclick=()=>loadConfig().catch(e=>$('#native-config-status').textContent=e.message);
 async function saveConfig(restore=false){
  const status=$('#native-config-status');$('#native-config-save').disabled=true;$('#native-config-restore').disabled=true;
  try{
   if(!config)throw Error('Load configurations first');
   const body={expected_revision:config.current.revision,reason:$('#native-config-reason').value};
   if(restore)body.restore_revision=$('#native-config-history').value;
   else{body.configs=config.current.configs.map(x=>x.ticker!==$('#native-config-ticker').value?x:{...x,...Object.fromEntries(['s1','s2','s3','pm'].map(k=>[k,Number($('#native-'+k).value)])),enabled:$('#native-enabled').checked});}
   await api('/api/native/smoothers/config',body);await loadConfig();
  }catch(e){status.textContent=e.message;}finally{$('#native-config-save').disabled=!config;$('#native-config-restore').disabled=!config;}
 }
 $('#native-config-save').onclick=()=>saveConfig();$('#native-config-restore').onclick=()=>saveConfig(true);
 function showRoute(r){routing=r;$('#native-route-status').textContent=JSON.stringify(r,null,2);$('#native-route-prepare').disabled=false;$('#native-route-cancel').disabled=false;$('#native-route-schedule').disabled=!r.prepared;}
 $('#native-route-load').onclick=async()=>{try{showRoute(await api('/api/native/morning/routing'));}catch(e){$('#native-route-status').textContent=e.message;}};
 async function route(action){
  try{if(!routing)throw Error('Load routing first');showRoute(await api('/api/native/morning/routing',{expected_revision:routing.revision,action,mode:$('#native-route-mode').value,effective_session:$('#native-route-date').value||null,reason:$('#native-route-reason').value}));}
  catch(e){$('#native-route-status').textContent=e.message;}
 }
 $('#native-route-prepare').onclick=()=>route('prepare');$('#native-route-schedule').onclick=()=>route('schedule');$('#native-route-cancel').onclick=()=>route('cancel_future');
 let handoff=null;
 function showHandoff(r){handoff=r.plan||r;$('#native-handoff-status').textContent=JSON.stringify(r,null,2);$('#native-handoff-activate').disabled=handoff.state!=='prepared';}
 async function handoffAction(action){
  try{showHandoff(await api('/api/native/morning/handoff',action?{action,review_session:$('#native-handoff-review').value,effective_session:$('#native-handoff-date').value,plan_id:handoff?.id||'',previous_sender_paused:$('#native-handoff-paused').checked,operator_reviewed:$('#native-handoff-reviewed').checked}:null));}
  catch(e){$('#native-handoff-status').textContent=e.message;}
 }
 $('#native-handoff-load').onclick=()=>handoffAction();$('#native-handoff-prepare').onclick=()=>handoffAction('prepare');$('#native-handoff-activate').onclick=()=>handoffAction('activate');$('#native-handoff-rollback').onclick=()=>handoffAction('rollback');
 let smoothersPlan=null;
 async function smoothersAction(action){
  const status=$('#smoothers-handoff-status');
  try{
   const r=await api('/api/native/smoothers/handoff',action?{action,review_week:$('#smoothers-handoff-review').value,effective_week:$('#smoothers-handoff-date').value,plan_id:smoothersPlan?.id||'',previous_sender_paused:$('#smoothers-handoff-paused').checked,operator_reviewed:$('#smoothers-handoff-reviewed').checked,original_alerts_reviewed:$('#smoothers-handoff-original').checked}:null);
   smoothersPlan=r.plan||r;status.textContent=JSON.stringify(r,null,2);$('#smoothers-handoff-activate').disabled=smoothersPlan.state!=='prepared';
   if(action==='prepare'||action==='rollback')for(const id of ['paused','reviewed','original'])$('#smoothers-handoff-'+id).checked=false;
  }catch(e){status.textContent=e.message;}
 }
 for(const action of ['load','prepare','activate','rollback'])$('#smoothers-handoff-'+action).onclick=()=>smoothersAction(action==='load'?null:action);
})();
