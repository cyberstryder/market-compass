/* Read-only candidate browsing with pinned pages and complete-window summaries. */
(()=>{
 const el=id=>document.getElementById('swing-study-'+id);
 const labels={technical_only:'Technical-only baseline',fresh_flow:'Fresh flow',delayed_flow:'Delayed flow',
  fresh_vendor_blocked:'Fresh prints / vendor gate blocked',fresh_or_delayed:'Fresh or delayed flow',
  any_support:'Any supporting flow, ignoring freshness gates',no_confirming_flow:'No confirming observed flow',
  rejected_by_fresh_flow:'Not selected by fresh-flow rules',unavailable:'Flow coverage unavailable',historical_inventory:'Historical inventory'};
 let report=null,page=null,loading=false,failed=false,token=0,index=0,cursors=[''],cohort=el('cohort').value;
 function controls(){el('prev').disabled=loading||index===0;el('next').disabled=loading||!page?.has_more;el('newest').disabled=loading;}
 function results(rows,label){return table([label,'Candidates','Measured','Pending','Unavailable','Mean directional move','Positive endpoints','≥ +0.5%','≤ −0.5%'],rows.map(r=>[
  esc(labels[r.arm||r.bucket]||r.bucket||'Unknown'),num(r.records,0),num(r.measured,0),num(r.pending,0),num(r.unavailable,0),
  r.mean_directional_pct==null?'Not measured':num(r.mean_directional_pct,3)+'%',
  r.positive_fraction==null?'Not measured':num(r.positive_fraction*100,1)+'%',num(r.up_half,0),num(r.down_half,0)]));}
 function comparisons(){
  const h=el('horizon').value,d=el('dimension').value;
  const rows=(report?.comparisons||[]).filter(r=>r.horizon===h),groups=(report?.buckets||[]).filter(r=>r.horizon===h&&r.dimension===d);
  el('comparisons').innerHTML=rows.length?results(rows,'Selection'):empty('Awaiting prospective evidence','Known flow coverage and completed checkpoint windows are needed for comparison.');
  el('groups').innerHTML=groups.length?results(groups,'Group'):empty('No prospective group observations yet','Historical inventory does not receive invented original prices or flow classifications.');
 }
 function renderPage(){
  el('page-status').textContent=(page.records.length?'Candidates '+num(index*100+1,0)+'–'+num(index*100+page.records.length,0):'0 candidates')+' of '+num(page.total,0)+'. Full 30-day receipt window ending '+when(page.asof)+'. Pages show up to 100; outcomes and option status may advance.';
  el('records').innerHTML=page.records.map(r=>{
   const s=r.signal||{},f=r.flow||{},summary=f.summary||{},rows=f.source_rows||[],p=r.price_context||{};
   return '<details><summary>'+esc(r.symbol)+' · '+esc(r.side)+' · '+esc(r.rule)+' · '+esc(labels[r.flow_group]||r.flow_group)+' · '+when(r.first_seen)+'</summary>'+
    '<p>'+tag(r.origin)+' · '+tag(r.status)+' · Separate option pipeline: '+esc(r.option_status||'Not queued')+'</p>'+
    '<p>Signal '+when(s.signal_time)+' · First retained '+when(r.first_seen)+' · Assessment '+when(r.assessed_at)+' · Reference midpoint '+num(r.entry_mid,4)+'</p>'+
    '<p>Signal price '+num(s.signal_price)+' · Invalidation '+num(s.stop)+' · Target '+num(s.target)+' · Daily ATR '+num(s.daily?.atr14)+' · Reference within 0.25 ATR: '+(p.within_entry_tolerance==null?'Not measured':p.within_entry_tolerance?'Yes':'No')+'</p>'+
    '<p>Flow snapshot '+when(f.snapshot_at)+' · Aligned source age '+age(f.aligned_source_age)+' · Expiry policy '+esc(f.dte_policy||'Not captured')+' DTE · Vendor check '+esc(f.vendor_check?.status||'Not captured')+'</p>'+
    '<p>Bullish premium $'+num(summary.bullish_premium,0)+' · Bearish premium $'+num(summary.bearish_premium,0)+' · Original flow-confirmed flag '+esc(r.original_flow_confirmed==null?'Unknown':String(r.original_flow_confirmed))+'</p>'+
    '<p>'+esc([r.entry_reason,...(f.reasons||[])].filter(Boolean).join(' · ')||'Frozen observations available')+'</p>'+
    table(['Checkpoint','Target','State','Directional move','Missing-data reason'],r.checkpoints.map(x=>[esc(x.horizon),when(x.target_at),tag(x.status),x.directional_return_pct==null?'Not measured':num(x.directional_return_pct,3)+'%',esc(x.reason||'—')]))+
    '<details><summary>Frozen source evidence ('+num(rows.length,0)+' received records)</summary>'+table(['Vendor ID','Direction','Expiry','TM score','Premium','Source time','First received','Snapshot seen'],rows.map(x=>{const v=x.payload||{};return [esc(v.vendor_id),esc(v.sentiment),esc(v.expiry),num(v.score,0),'$'+num(v.premium,0),when(v.source_ts),when(x.first_seen),when(x.last_seen)];}))+'</details>'+
    '<p class="fine">Underlying midpoint endpoints exclude fees, slippage and option pricing. Stops, targets and tolerance are frozen context; this study does not simulate their path. '+esc(r.version)+' · '+esc(r.id.slice(0,16))+'</p></details>';
  }).join('')||empty('No candidates in this cohort','Retained candidates will appear as the Swing scanner and comparison worker run.');
 }
 async function load(cursor,pageIndex,asof){
  const current=++token;loading=true;failed=false;controls();el('page-status').textContent='Loading candidates…';
  const params=new URLSearchParams({cohort,limit:'100'});if(cursor)params.set('cursor',cursor);else if(asof)params.set('asof',String(asof));
  try{
   const response=await fetch('/api/swing-study/records?'+params,{cache:'no-store'});
   if(!response.ok)throw new Error('Request failed ('+response.status+')');
   const data=await response.json();if(current!==token)return;
   page=data;index=pageIndex;if(!index)cursors=[''];cursors[index]=cursor;renderPage();
  }catch(error){if(current!==token)return;failed=true;el('page-status').textContent=error.message+'. Use Newest candidates to retry.';}
  finally{if(current===token){loading=false;controls();}}
 }
 window.renderSwingStudy=data=>{
  report=data||{};const n=report.counts||{},i=report.inventory||{},c=report.collection||{};
  el('summary').innerHTML=[['Prospective candidates',n.prospective],['Known flow coverage',n.classified],['Flow coverage unavailable',n.flow_unavailable],['Historical inventory',n.historical_inventory]].map(([label,value])=>'<div class="stat"><div class="label">'+esc(label)+'</div><div class="value">'+num(value,0)+'</div><div class="fine">Full 30-day receipt window</div></div>').join('');
  el('coverage').innerHTML='<p>Report '+when(report.at)+' · Worker '+when(report.worker?.at)+' · '+esc(report.version||'Awaiting first report')+'</p><p>Lifetime retained '+num(i.retained_candidates,0)+' · Registered '+num(i.registered,0)+' · Awaiting registration '+num(i.unregistered,0)+' · Due checkpoints waiting '+num(report.overdue_checkpoints,0)+'</p><p>Prospective sessions '+num(n.prospective_sessions,0)+' · Symbols '+num(n.prospective_symbols,0)+' · Pending symbols in price collection '+num(c.in_collection,0)+' / '+num(c.pending_symbols,0)+'</p><p class="fine">'+esc(report.note||'The worker will publish coverage after its first cycle.')+'</p>'+table(['Origin','Checkpoint','State','Candidates','Missing-data reason'],(report.outcome_counts||[]).map(r=>[esc(r.origin),esc(r.horizon),tag(r.status),num(r.count,0),esc(r.reason||'—')]));
  el('options').innerHTML=(report.option_pipeline||[]).length?table(['Study origin','Option pipeline state','Candidates'],report.option_pipeline.map(r=>[esc(r.origin),tag(r.status),num(r.count,0)])):empty('No linked option admissions yet','The candidate study still measures rejected technical setups when reference prices are available.');
  comparisons();if(document.getElementById('swing-study').classList.contains('active')&&!page&&!loading&&!failed)load('',0,report.at);controls();
 };
 el('horizon').onchange=comparisons;el('dimension').onchange=comparisons;
 el('cohort').onchange=()=>{token++;cohort=el('cohort').value;page=null;loading=false;failed=false;index=0;cursors=[''];el('records').innerHTML='';load('',0);};
 el('prev').onclick=()=>{if(!loading&&index>0)load(cursors[index-1],index-1,page.asof);};
 el('next').onclick=()=>{if(!loading&&page?.has_more)load(page.next_cursor,index+1,page.asof);};
 el('newest').onclick=()=>{if(!loading)load('',0);};controls();
})();
