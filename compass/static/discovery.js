/* Bounded research view. All provider and symbol text is escaped. */
(()=>{
 let report=null,busy=false;
 const el=id=>document.getElementById('discovery-'+id);
 function draw(){
  if(!report)return;
  const u=report.universe||{},w=report.worker||{};
  el('coverage').innerHTML='<p>'+esc(w.enabled?'Collecting':'New discovery paused')+' · checked '+when(w.at)+
   ' · '+num((u.symbols||[]).length,0)+' research symbols · '+num(Object.keys(u.dynamic||{}).length,0)+' discovered from flow</p>'+
   '<p>'+esc(report.note)+'</p><p>Capacity exclusions: '+esc((u.excluded_capacity||[]).join(', ')||'None')+
   '. Flow query: '+esc(u.flow_query_truncated?'truncated; incomplete evidence':'bounded')+'</p>'+
   '<details><summary>Covered symbols and source</summary><p>'+esc((u.symbols||[]).join(', '))+'</p><p>'+esc(u.source)+'</p></details>';
  const query=el('filter').value.trim().toUpperCase();
  const rows=(report.coverage||[]).filter(r=>!query||r.symbol===query);
  el('reasons').innerHTML=table(['Symbol','Checked','Reasons','Rule checks'],rows.map(r=>[
   esc(r.symbol),when(r.at),esc((r.reasons||[]).join(', ')||'Candidate'),
   esc((r.tests||[]).map(t=>t.side+' '+t.rule+': '+(t.failures.join(', ')||'passed')).join('; '))]));
  el('records').innerHTML='<p>'+num(report.total,0)+' retained candidates; showing latest '+num(report.records.length,0)+
   (report.records_truncated?' (older records remain stored).':'.')+'</p>'+report.records.map(r=>
   '<details><summary>'+esc(r.symbol)+' · '+esc(r.side)+' · '+esc(r.rule)+' · '+when(r.assessed_at)+'</summary>'+
   '<p>'+esc(r.flow.group)+' · entry midpoint '+num(r.entry_mid,4)+' · '+esc(r.status)+'</p>'+
   table(['Checkpoint','Due','Status','Directional stock move'],r.outcomes.map(o=>[
    esc(o.horizon),when(o.target_at),esc(o.status),o.directional_pct==null?esc(o.reason||'Pending'):num(o.directional_pct,3)+'%']))+
   '<p>Stock midpoint changes before costs; not option profits or verified stop/target paths.</p></details>').join('');
 }
 async function load(){
  if(location.hash!=='#discovery'||busy)return;
  busy=true;
  try{const response=await fetch('/api/discovery');if(!response.ok)throw new Error('Discovery report unavailable');report=await response.json();draw();}
  catch(error){el('coverage').textContent='Unable to refresh discovery research. Last displayed results may be stale.';}
  finally{busy=false;}
 }
 el('filter').addEventListener('input',draw);
 document.querySelector('[data-tab="discovery"]').addEventListener('click',load);
 window.addEventListener('hashchange',load);setInterval(load,15000);load();
})();
