/* Read-only, pinned report pages and append-only paired quote paths. */
(()=>{
 const el=id=>document.getElementById('spy-study-'+id);
 const phases={preopen:'Pre-open plan',opening:'08:45 opening check',followup_30:'09:00 later check',followup_45:'09:15 later check',followup_60:'09:30 final check'};
 let report={},page=null,loading=false,failed=false,token=0,index=0,cursors=[''],cohort=el('cohort').value;
 let pathId=null,pathPage=null,pathIndex=0,pathToken=0,pathLoading=false;
 const pct=value=>value==null?'Not measured':num(value,2)+'%';
 function controls(){el('prev').disabled=loading||index===0;el('next').disabled=loading||!page?.has_more;el('newest').disabled=loading;}
 function groups(){
  const rows=(report.groups||[]).filter(r=>r.dimension===el('dimension').value);
  el('groups').innerHTML=rows.length?table(['Group','Candidates','Pending','Open','Completed','Excluded','Unresolved','Wins','Net P&L','Mean net return','Mean best / worst'],rows.map(r=>[
   esc(phases[r.bucket]||r.bucket||'Not recorded'),num(r.candidates,0),num(r.pending,0),num(r.open,0),num(r.completed,0),num(r.excluded,0),num(r.unresolved,0),num(r.wins,0),
   r.net_pnl==null?'Not measured':'$'+num(r.net_pnl),pct(r.mean_net_return_pct),pct(r.mean_best_pct)+' / '+pct(r.mean_worst_pct)])):
   empty('Awaiting prospective option observations','Historical plans have no reconstructed entries or option returns.');
 }
 function renderPage(){
  el('page-status').textContent=(page.records.length?'Records '+num(index*100+1,0)+'–'+num(index*100+page.records.length,0):'0 records')+' of '+num(page.total,0)+'. Sessions '+page.since_day+'–'+page.through_day+'. Registration cutoff '+when(page.asof)+'. Outcomes may advance.';
  el('records').innerHTML=page.records.map(r=>{
   const p=r.report||{},c=p.context||{},f=p.frozen_premarket||{},o=r.option;
   return '<details><summary>'+esc(r.day)+' · '+esc(phases[r.phase]||r.phase)+' · '+esc(p.decision||r.reason||r.decision_state)+' · '+esc(r.origin)+'</summary>'+
    '<p>Saved report '+esc(r.source_key)+' · Registered '+when(r.registered_at)+' · '+tag(o?.status||r.record_kind)+'</p>'+
    '<p>Plan delivery '+esc(r.delivery_status||'Not observed')+' · '+when(r.delivery?.at)+' · Message '+esc(r.delivery?.message_id||'Not confirmed')+'</p>'+
    (r.record_kind==='schedule_gap'?'<p>'+esc(r.reason)+' · Scheduled window '+when(r.window_start)+' to '+when(r.deadline)+'</p>':
     '<p>Generated '+when(p.generated_at)+' · Valid until '+when(p.expires_at)+' · Frozen PM high / low '+num(f.high)+' / '+num(f.low)+' · Confirmation close '+num(c.confirmation_candle?.close)+'</p><p>Plan version '+esc(p.version)+' · '+esc((p.data_blocks||[]).join(' · ')||'No recorded plan data blocks')+'</p>')+
    '<p>'+esc(r.eligibility_reason||r.reason||'Linked prospective assessment')+'</p>'+
    (o?'<p>Contract '+esc(o.contract?.symbol||'Not selected')+' · '+esc(o.contract?.expiry||'Same-day required')+' · '+esc(o.underlying_side)+' · Reference / stop / target '+num(o.signal_price)+' / '+num(o.underlying_stop)+' / '+num(o.underlying_target)+'</p>'+
     '<p>Entry '+num(o.entry)+' at '+when(o.opened_at)+' · Exit '+num(o.exit)+' at '+when(o.finished_at)+' · Reason '+esc(o.exit_reason||o.waiting_reason||'Observing')+'</p>'+
     '<p>Net option P&L '+(o.pnl==null?'Not measured':'$'+num(o.pnl))+' · Net return '+pct(o.return_pct)+' · Best / worst sampled '+pct(o.best_pct)+' / '+pct(o.worst_pct)+' · Underlying directional move '+pct(o.underlying_directional_pct)+'</p>'+
     '<p>Entry spread $'+num(o.entry_spread)+' ('+pct(o.entry_spread_pct)+') · Delay after report / delivery '+age(o.entry_delay_seconds)+' / '+age(o.entry_delay_after_delivery)+' · Entry debit with fee $'+num(o.entry_debit)+' · Paired samples '+num(o.samples,0)+' · Maximum feed gap '+age(o.max_gap_seconds)+'</p>'+
     '<p>Latest entry diagnostic '+esc(o.last_attempt?.reason||'—')+' · Option collection '+esc(o.collection_version||'Not entered')+' · Planned session exit '+when(o.flatten_at)+'</p>'+
     (o.opened_at?'<button type="button" data-spy-path="'+esc(r.id)+'">View recorded path</button>':'')+
     '<details><summary>Frozen entry and lifecycle evidence</summary><pre>'+esc(JSON.stringify(o,null,2))+'</pre></details>':'')+
    (r.report?'<details><summary>Complete frozen morning report</summary><pre>'+esc(JSON.stringify(r.report,null,2))+'</pre></details>':'')+
    '<p class="fine">'+esc(r.version)+' · '+esc(r.id)+'</p></details>';
  }).join('')||empty('No records in this cohort','New saved SPY reports and audited schedule slots appear here.');
  el('records').querySelectorAll('[data-spy-path]').forEach(button=>button.onclick=()=>loadPath(button.dataset.spyPath,'',0));
 }
 async function load(cursor,pageIndex,asof){
  const current=++token;loading=true;failed=false;controls();el('page-status').textContent='Loading reports…';
  const params=new URLSearchParams({cohort,limit:'100'});if(cursor)params.set('cursor',cursor);else if(asof)params.set('asof',String(asof));
  try{const response=await fetch('/api/spy-study/records?'+params,{cache:'no-store'});if(!response.ok)throw new Error('Request failed ('+response.status+')');
   const data=await response.json();if(current!==token)return;page=data;index=pageIndex;if(!index)cursors=[''];cursors[index]=cursor;renderPage();
  }catch(error){if(current!==token)return;failed=true;el('page-status').textContent=error.message+'. Use Newest records to retry.';}
  finally{if(current===token){loading=false;controls();}}
 }
 async function loadPath(id,cursor,pageIndex){
  const current=++pathToken;pathLoading=true;pathId=id;el('path-next').disabled=true;el('path-reset').disabled=true;el('path-status').textContent='Loading recorded path…';
  const params=new URLSearchParams({limit:'100'});if(cursor)params.set('cursor',cursor);
  try{const response=await fetch('/api/spy-study/marks/'+id+'?'+params,{cache:'no-store'});if(!response.ok)throw new Error('Path request failed ('+response.status+')');
   const data=await response.json();if(current!==pathToken)return;pathPage=data;pathIndex=pageIndex;
   el('path-status').textContent='Observation '+id.slice(0,16)+' · '+num(data.total,0)+' paired marks at '+when(data.asof)+' · Page '+(pageIndex+1)+'. Each page shows up to 100.';
   el('path').innerHTML=table(['Source decision time','Event','Option bid / ask','SPY bid / ask','Net liquidation P&L','Net return','Stock move','Recorded option / SPY'],data.records.map(r=>{const p=r.payload,q=p.option_quote,u=p.underlying_quote;return [when(r.at),esc(p.event),num(q.bid)+' / '+num(q.ask),num(u.bid)+' / '+num(u.ask),'$'+num(p.net_pnl),pct(p.net_return_pct),pct(p.underlying_directional_pct),when(q.recorded_at||q.received)+' / '+when(u.recorded_at||u.received)];}));
  }catch(error){if(current!==pathToken)return;pathPage=null;el('path-status').textContent=error.message;}
  finally{if(current===pathToken){pathLoading=false;el('path-next').disabled=!pathPage?.has_more;el('path-reset').disabled=false;}}
 }
 window.renderSPYStudy=data=>{
  report=data||{};const n=report.counts||{},i=report.inventory||{},c=report.collection||{},p=report.protocol||{};
  const statuses=Object.fromEntries((report.statuses||[]).map(r=>[r.status,r.count]));
  el('summary').innerHTML=[['Prospective reports',n.prospective_reports],['Confirmed checks',n.confirmations],['Completed option paths',report.version?(statuses.closed||0):null],['Unresolved paths',report.version?(statuses.unresolved||0):null]].map(([label,value])=>'<div class="stat"><div class="label">'+esc(label)+'</div><div class="value">'+num(value,0)+'</div><div class="fine">Full 30-day session window</div></div>').join('');
  el('coverage').innerHTML='<p>Report '+when(report.at)+' · Worker '+when(report.worker?.at)+' · '+esc(report.version||'Awaiting report')+'</p><p>Lifetime saved '+num(i.saved_reports,0)+' · Registered '+num(i.registered,0)+' · Awaiting registration '+num(i.unregistered,0)+' · Historical reports in window '+num(n.historical_reports,0)+'</p><p>WAIT checks '+num(n.waits,0)+' · NO ENTRY checks '+num(n.no_entry,0)+' · Missed or unused schedule slots '+num(n.schedule_slots_without_report,0)+' · Requested contracts in subscriptions '+num(c.in_subscription,0)+' / '+num(c.requested,0)+'</p><p class="fine">'+esc(report.note||'Awaiting first worker cycle.')+'</p>'+table(['Origin','Record type','Check','Decision','Count'],(report.decisions||[]).map(r=>[esc(r.origin),esc(r.kind),esc(phases[r.phase]||r.phase),esc(r.decision),num(r.count,0)]))+table(['Option path state','Count'],(report.statuses||[]).map(r=>[tag(r.status),num(r.count,0)]));
  el('rules').innerHTML=table(['Rule','Definition'],['entry','contract','liquidity','continuity','prices','exits','primary','limitations'].map(k=>[esc(k),esc(p[k]||'Awaiting frozen protocol')]));
  timing(report.timeframes||{});groups();if(document.getElementById('spy-study').classList.contains('active')&&!page&&!loading&&!failed)load('',0,report.at);controls();
 };
 function timing(t){
  const cash=x=>x==null?'Not measured':'$'+num(x);
  el('timeframes').innerHTML='<p>Worker '+when(t.worker?.at)+' · Complete calendar sessions '+num(t.completed_calendar_sessions,0)+' / '+num(t.required_sessions,0)+' · Evaluation '+(t.version?(t.evaluation_locked?'results held until '+when(t.activation?.evaluation_end):'period ended; review coverage before conclusions'):'awaiting activation')+'</p>'+
   '<p>'+esc(t.note||'Awaiting prospective capture; no timeframe has been established as best.')+'</p>'+
   table(['Cohort','Minutes','Days','Path states','Blocked / missed checks','Completed net P&L'],(t.groups||[]).map(r=>[esc(r.cohort),num(r.timeframe,0),num(r.days,0),esc(JSON.stringify(r.statuses)),num(r.blocked_checks,0)+' / '+num(r.missed_checks,0),r.cohort==='evaluation'&&t.evaluation_locked?'Held out':cash(r.results?.net_pnl)]))+
   '<h3>Matched days — all four timeframes observed</h3>'+table(['Cohort','Minutes','Matched days','No-signal days','Mean daily net','Net P&L','Drawdown','Win rate','Profit factor','Mean hold','Mean giveback'],(t.paired||[]).map(r=>[esc(r.cohort),num(r.timeframe,0),num(r.matched_days,0),num(r.no_signal_days,0),cash(r.mean_daily_net),cash(r.results?.net_pnl),cash(r.results?.closed_equity_drawdown),pct(r.results?.win_rate==null?null:r.results.win_rate*100),r.results?.profit_factor==null?'Not measured':num(r.results.profit_factor),r.results?.mean_hold_seconds==null?'Not measured':age(r.results.mean_hold_seconds),r.results?.mean_giveback_pct_points==null?'Not measured':num(r.results.mean_giveback_pct_points)+' pp']))+
   '<h3>Paired daily differences versus 15 minutes</h3>'+table(['Minutes','Matched days','Status','Mean difference','Adjusted bootstrap interval'],(t.comparisons||[]).map(r=>[num(r.timeframe,0),num(r.matched_days,0),esc(r.status),cash(r.mean_difference),r.interval?cash(r.interval[0])+' to '+cash(r.interval[1]):'Not available']))+
   '<details><summary>Time of day, opening gap and frozen definitions</summary>'+table(['Cohort','Minutes','Cut','Group','Completed','Net P&L'],(t.cuts||[]).map(r=>[esc(r.cohort),num(r.timeframe,0),esc(r.dimension),esc(r.bucket),num(r.results.completed,0),cash(r.results.net_pnl)]))+'<pre>'+esc(JSON.stringify(t.protocol||{},null,2))+'</pre></details>';
 }
 let timingOffset=0,timingBusy=false;
 async function timingPage(reset){
  if(timingBusy)return;timingBusy=true;const filter=document.getElementById('spy-timing-filter');
  const next=document.getElementById('spy-timing-next'),refresh=document.getElementById('spy-timing-refresh');
  if(reset)timingOffset=0;next.disabled=true;refresh.disabled=true;filter.disabled=true;
  try{const response=await fetch('/api/spy-timeframes/records?timeframe='+filter.value+'&offset='+timingOffset);
   if(!response.ok)throw Error('Decision audit unavailable');const d=await response.json();
   document.getElementById('spy-timing-audit').innerHTML='<p>'+num(d.offset+1,0)+'–'+num(d.offset+d.records.length,0)+' of '+num(d.total,0)+' · '+esc(d.note)+'</p>'+table(['Day','Minutes','Candle end','Observed','Decision','Close','Blocks'],d.records.map(r=>[esc(r.day),num(r.timeframe,0),when(r.boundary),when(r.observed_at),esc(r.status),num(r.close),esc(r.data_blocks.join(', '))]));
   timingOffset=d.offset+d.records.length;next.disabled=!d.has_more;
  }catch(error){document.getElementById('spy-timing-audit').textContent=error.message;}
  finally{timingBusy=false;refresh.disabled=false;filter.disabled=false;}
 }
 document.getElementById('spy-timing-refresh').onclick=()=>timingPage(true);
 document.getElementById('spy-timing-filter').onchange=()=>timingPage(true);
 document.getElementById('spy-timing-next').onclick=()=>timingPage(false);
 el('dimension').onchange=groups;
 el('cohort').onchange=()=>{token++;cohort=el('cohort').value;page=null;loading=false;failed=false;index=0;cursors=[''];el('records').innerHTML='';load('',0);};
 el('prev').onclick=()=>{if(!loading&&index>0)load(cursors[index-1],index-1,page.asof);};
 el('next').onclick=()=>{if(!loading&&page?.has_more)load(page.next_cursor,index+1,page.asof);};
 el('newest').onclick=()=>{if(!loading)load('',0);};
 el('path-next').onclick=()=>{if(!pathLoading&&pathPage?.has_more)loadPath(pathId,pathPage.next_cursor,pathIndex+1);};
 el('path-reset').onclick=()=>{if(!pathLoading&&pathId)loadPath(pathId,'',0);};controls();
})();
