/* Pick checker: stack one external pick against Compass evidence. Read-only views plus a POST to /api/pick-check. */
(()=>{
 const PILLARS=[['apex','Apex magnets'],['tape','Institutional tape'],['gap','Gap continuation'],['breakout','Breakouts'],['exposure','Exposure / GEX']];
 const ALIGN_CLS={supports:'good',contradicts:'bad',neutral:'',info:'',no_data:''};
 const alignTag=a=>'<span class="tag '+(ALIGN_CLS[a]||'')+'">'+esc((a||'no_data').replaceAll('_',' '))+'</span>';

 async function j(url,opts){const r=await fetch(url,opts);const b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(b.detail||('HTTP '+r.status));return b;}

 function money(x){return x==null?'—':'$'+num(x);}

 function pillarCard(key,p){
  const detail=p.detail||{};
  let extra='';
  if(key==='apex'){
   const a=detail.nearest_above,b=detail.nearest_below;
   const role=detail.role?'<div>Magnet role: <strong>'+esc(detail.role)+'</strong>'+(detail.vs_flip?' · spot <strong>'+esc(detail.vs_flip)+'</strong> the gamma flip'+(detail.gamma_flip?' ('+money(detail.gamma_flip)+')':''):'')+'</div>':'';
   extra=role+(a?'<div>Nearest magnet above: <strong>'+money(a.magnet)+'</strong> ('+num(a.distance_pct*100,2)+'% away)'+(a.signal?' · '+esc(a.signal):'')+'</div>':'<div>No magnet above in radius.</div>')
        +(b?'<div>Nearest magnet below: <strong>'+money(b.magnet)+'</strong> ('+num(b.distance_pct*100,2)+'% away)'+(b.signal?' · '+esc(b.signal):'')+'</div>':'<div>No magnet below in radius.</div>');
  }else if(key==='tape'){
   extra='<div>Confirmed prints: '+esc(detail.confirmed??0)+' · with pick: '+esc(detail.with_pick??0)+' · against: '+esc(detail.against_pick??0)+'</div>';
  }else if(key==='gap'){
   extra='<div>Gap: '+(detail.gap_pct==null?'—':num(detail.gap_pct*100,2)+'%')+' · stage: '+esc(detail.stage||'—')+(detail.qualified?' · <strong>qualified</strong>':'')+'</div>';
  }else if(key==='breakout'){
   extra=(detail.events||[]).map(e=>'<div>'+esc(e.status||'')+' '+esc(e.direction||'')+' break at '+money(e.level)+'</div>').join('')||'<div>No active coil or fresh break.</div>';
  }else if(key==='exposure'){
   const keys=Object.keys(detail).slice(0,6);
   extra=keys.length?'<div class="fine">'+esc(keys.map(k=>k+'='+String(detail[k]).slice(0,40)).join(' · '))+'</div>':'<div>No exposure snapshot for this symbol.</div>';
  }
  return '<article class="panel research-card"><div class="panel-head"><h2>'+esc(PILLARS.find(x=>x[0]===key)[1])+'</h2>'+alignTag(p.alignment)+'</div>'
   +(p.note?'<p>'+esc(p.note)+'</p>':'')
   +extra
   +'<p class="fine">'+esc(p.basis||'')+'</p></article>';
 }

 function renderResult(r){
  const pills=r.pillars||{};
  const names={'apex':'apex','tape':'tape','gap':'gap','breakout':'breakout','exposure':'exposure'};
  const counted=r.pillars_counted??0,score=r.evidence_score??0;
  $('#pickcheck-result').innerHTML=
   '<article class="panel"><div class="panel-head"><h2>'+esc(r.ticker)+' · '+esc(r.direction)+'</h2><span class="pill">evidence score '+(score>=0?'+':'')+score+' / '+counted+' pillars</span></div>'
   +'<div class="stats">'
   +'<div class="stat"><div class="label">Entry</div><div class="value">'+money(r.entry)+'</div></div>'
   +'<div class="stat"><div class="label">Target</div><div class="value">'+money(r.target)+'</div></div>'
   +'<div class="stat"><div class="label">Compass spot</div><div class="value">'+money(r.spot)+'</div></div>'
   +'<div class="stat"><div class="label">Source</div><div class="value" style="font-size:18px">'+esc(r.source||'—')+'</div></div>'
   +'</div>'
   +'<p class="fine">Checked '+when(r.at)+'. Pillars with no data do not move the score. This is research evidence, not a trade signal.</p></article>'
   +'<div class="research-cards">'+PILLARS.map(([k])=>pillarCard(names[k],pills[k]||{alignment:'no_data',detail:{}})).join('')+'</div>';
 }

 function renderRecent(rows){
  if(!rows.length){$('#pickcheck-recent').innerHTML=empty('No picks checked yet','Checked picks are logged here so analyst hit rates can be measured later.');return;}
  $('#pickcheck-recent').innerHTML=table(['Time','Ticker','Dir','Entry','Target','Spot','Source','Score'],
   rows.map(x=>{const p=x.payload||{};const s=p.evidence_score??0;
    return [when(x.ts||p.at),'<strong>'+esc(p.ticker||'')+'</strong>',esc(p.direction||''),money(p.entry),money(p.target),money(p.spot),esc(p.source||'—'),(s>=0?'+':'')+s];}));
 }

 async function loadRecent(){
  try{const d=await j('/api/pick-check/recent?limit=50');renderRecent(d.checks||[]);}
  catch(e){$('#pickcheck-recent').innerHTML=empty('Could not load history',e.message);}
 }

 async function runCheck(){
  const btn=$('#pickcheck-run');btn.disabled=true;
  $('#pickcheck-result').innerHTML='<p class="muted">Checking against Compass evidence…</p>';
  try{
   const body={ticker:$('#pickcheck-ticker').value.trim(),
    direction:$('#pickcheck-direction').value,
    entry:$('#pickcheck-entry').value==='' ? null : Number($('#pickcheck-entry').value),
    target:$('#pickcheck-target').value==='' ? null : Number($('#pickcheck-target').value),
    source:$('#pickcheck-source').value.trim()};
   const r=await j('/api/pick-check',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
   renderResult(r);loadRecent();
  }catch(e){$('#pickcheck-result').innerHTML=empty('Check failed',e.message);}
  btn.disabled=false;
 }

 document.addEventListener('DOMContentLoaded',()=>{
  if(!$('#pick-check'))return;
  $('#pickcheck-run').onclick=runCheck;
  loadRecent();
 });
})();
