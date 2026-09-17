/* Read-only study browsing. Aggregate refreshes never replace an older page. */
(()=>{
 let report=null,page=null,loading=false,failed=false,token=0,index=0,cursors=[''];
 const el=id=>document.getElementById(id);
 let cohort=el('tm-cohort').value;
 const labels={all_matched:'All matched records',tm_only:'TM selected',compass_only:'Compass selected',both:'Both selected',tm_rejected:'TM rejected',compass_rejected:'Compass rejected',neither:'Neither selected'};
 function controls(){
  el('tm-previous').disabled=loading||index===0;
  el('tm-next').disabled=loading||!page?.has_more;
  el('tm-newest').disabled=loading;
 }
 function resultTable(rows,label){
  return table([label,'Records','Measured','Pending','Unavailable','Unsigned','Mean directional move','Positive moves','≥ +0.25%','≤ −0.25%'],rows.map(r=>[
   esc(labels[r.arm]||r.bucket||'unknown'),num(r.records,0),num(r.measured,0),num(r.pending,0),num(r.unavailable,0),num(r.unsigned,0),
   r.mean_directional_pct==null?'Not measured':num(r.mean_directional_pct,3)+'%',
   r.positive_fraction==null?'Not measured':num(r.positive_fraction*100,1)+'%',num(r.move_up_025,0),num(r.move_down_025,0)]));
 }
 function renderComparisons(){
  const horizon=el('tm-horizon').value,dimension=el('tm-dimension').value;
  const rows=(report?.comparisons||[]).filter(r=>r.horizon===horizon);
  el('tm-study-comparisons').innerHTML=rows.length?resultTable(rows,'Selection'):empty('Awaiting matched evidence','Both scores must be complete. Checkpoint results will appear as their observation windows finish.');
  const groups=(report?.buckets||[]).filter(r=>r.horizon===horizon&&r.dimension===dimension);
  el('tm-study-buckets').innerHTML=groups.length?resultTable(groups,'Group'):empty('No prospective group observations yet','Historical inventory is separate from forward evaluation.');
 }
 function renderPage(){
  el('tm-record-status').textContent=(page.records.length?'Records '+num(index*100+1,0)+'–'+num(index*100+page.records.length,0):'0 records')+
   ' of '+num(page.total,0)+' in this cohort. Full 30-day receipt window ending '+when(page.asof)+'. Pages show up to 100; outcomes may advance.';
  el('tm-study-records').innerHTML=page.records.map(r=>{
   const a=r.assessment||{},raw=r.first_snapshot||r.inventory_snapshot||{};
   const value=r.compass_score==null?(a.score_range?'Incomplete ('+a.score_range.join('–')+' possible points)':'Not assessed'):num(r.compass_score,0);
   return '<details><summary>'+esc(r.symbol)+' · '+esc(raw.sentiment||'Unknown direction')+' · TM '+num(r.tm_score,0)+' / Compass '+esc(value)+' · '+esc(r.status)+' · '+when(r.first_seen)+'</summary>'+
    '<p>'+tag(r.origin)+' · Source age '+age(r.source_age_at_receipt)+' · '+esc(r.dte_bucket)+' · '+esc(raw.classification||'Unknown flow type')+' · $'+num(raw.premium,0)+' premium</p>'+
    '<p>Source '+when(r.source_ts)+' · Received '+when(r.first_seen)+' · Assessment '+when(r.assessed_at)+' · Reference midpoint '+num(r.entry_mid,4)+'</p>'+
    (a.reason?'<p>'+esc(a.reason)+'</p>':'')+
    table(['Compass component','Points','Maximum'],(a.components||[]).map(x=>[esc(x.name),x.points==null?'Missing input':num(x.points,0),num(x.weight,0)]))+
    table(['Checkpoint','Target','State','Directional move','Missing-data reason'],r.checkpoints.map(x=>[esc(x.horizon),when(x.target_at),tag(x.status),x.directional_return_pct==null?'Not measured':num(x.directional_return_pct,3)+'%',esc(x.reason||'—')]))+
    '<p class="fine">Underlying midpoint changes exclude option pricing, fees and slippage. Unknown direction can have an unsigned price measurement.</p>'+
    '<a href="/api/tm-study/record?id='+encodeURIComponent(r.id)+'" target="_blank" rel="noreferrer">Frozen assessment and latest vendor correction ↗</a></details>';
  }).join('')||empty('No records in this cohort','New receipts and historical registration appear as the collector and study worker run.');
 }
 async function load(cursor,pageIndex,asof){
  const current=++token;loading=true;failed=false;controls();el('tm-record-status').textContent='Loading received records…';
  const params=new URLSearchParams({cohort,limit:'100'});
  if(cursor)params.set('cursor',cursor);else if(asof)params.set('asof',String(asof));
  try{
   const response=await fetch('/api/tm-study/records?'+params,{cache:'no-store'});
   if(!response.ok)throw new Error('Request failed ('+response.status+')');
   const data=await response.json();if(current!==token)return;
   page=data;index=pageIndex;if(!index)cursors=[''];cursors[index]=cursor;renderPage();
  }catch(error){if(current!==token)return;failed=true;el('tm-record-status').textContent=error.message+'. Use Newest records to retry.';}
  finally{if(current===token){loading=false;controls();}}
 }
 window.renderTMStudy=data=>{
  report=data||{};const n=report.counts||{},inv=report.inventory||{};
  el('tm-study-summary').innerHTML=[['Prospective assessments',n.prospective],['Complete Compass scores',n.scored],['Incomplete assessments',n.insufficient_data],['Historical inventory',n.historical_inventory]].map(([name,value])=>'<div class="stat"><div class="label">'+esc(name)+'</div><div class="value">'+num(value,0)+'</div><div class="fine">Full 30-day receipt window</div></div>').join('');
  const source=report.source_coverage||{},collection=report.collection||{};
  el('tm-study-coverage').innerHTML='<p>Report '+when(report.at)+' · Worker '+when(report.worker?.at)+' · '+esc(report.version||'Awaiting first report')+'</p><p>Lifetime received IDs '+num(inv.received_records,0)+' · Registered '+num(inv.registered_records,0)+' · Awaiting registration '+num(inv.unregistered_records,0)+' · Due checkpoints waiting '+num(report.overdue_checkpoints,0)+'</p><p>Latest feed day '+esc(source.day||'Unknown')+' · Stored '+num(source.unique_rows,0)+' / vendor count '+num(source.vendor_total,0)+' · Estimated missing IDs '+num(source.estimated_gap,0)+' · Rejected observations '+num(source.rejected_observations,0)+' (may repeat across polls)</p><p class="fine">'+esc(report.note||'The new worker will publish coverage after its first cycle.')+'</p>'+table(['Checkpoint','Origin','State','Records'],(report.outcome_counts||[]).map(r=>[esc(r.horizon),esc(r.origin),tag(r.status),num(r.count,0)]));
  if(collection.requested_symbols!=null)el('tm-study-coverage').innerHTML+='<p>Current TM symbols '+num(collection.requested_symbols,0)+' · In price collection '+num(collection.in_collection,0)+' · Outside collection '+num(collection.not_in_collection,0)+' · All collection slots '+num(collection.total_collection_symbols,0)+' / '+num(collection.ceiling,0)+'</p><p class="fine">'+esc(collection.note)+'</p>';
  renderComparisons();
  if(el('tm-study').classList.contains('active')&&!page&&!loading&&!failed)load('',0,report.at);
  controls();
 };
 el('tm-horizon').onchange=renderComparisons;el('tm-dimension').onchange=renderComparisons;
 el('tm-cohort').onchange=()=>{token++;cohort=el('tm-cohort').value;page=null;loading=false;failed=false;index=0;cursors=[''];el('tm-study-records').innerHTML='';load('',0);};
 el('tm-previous').onclick=()=>{if(!loading&&index>0)load(cursors[index-1],index-1,page.asof);};
 el('tm-next').onclick=()=>{if(!loading&&page?.has_more)load(page.next_cursor,index+1,page.asof);};
 el('tm-newest').onclick=()=>{if(!loading)load('',0);};
 controls();
})();
