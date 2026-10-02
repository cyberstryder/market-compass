/* Evidence scanner panels. Endpoints are research evidence only: no alerts, no trades. */
(()=>{
 let busy=false,boardBusy=false,apexCache=null;
 const detail=(o,skip)=>Object.entries(o||{}).filter(([k])=>!(skip||[]).includes(k)).map(([k,v])=>esc(k)+': '+esc(typeof v==='object'?JSON.stringify(v):String(v??''))).join(' · ');
 const pct1=x=>x===null||x===undefined?'—':num(x*100,1)+'%';

 function renderApex(r){
  $('#apex-asof').textContent='Snapshot '+when(r.asof)+' · radius '+esc(r.radius??'—')+' · tolerance '+esc(r.tolerance??'—');
  apexCache=r;
  const within=parseFloat(($('#apex-within')||{}).value||'0.02');
  const filt=(r.rows||[]).filter(x=>(x.distance_pct??1)<=within);
  const rows=filt.slice(0,50);
  const role=x=>x.role==='support'?'<span class="tag good">Support</span>':x.role==='resistance'?'<span class="tag bad">Resistance</span>':esc(x.role||'—');
  const flip=x=>x.vs_flip==='above'?'<span class="tag good">Above '+num(x.gamma_flip)+'</span>':x.vs_flip==='below'?'<span class="tag bad">Below '+num(x.gamma_flip)+'</span>':'<span class="tag">—</span>';
  $('#apex-rows').innerHTML=rows.length?table(['Symbol','Magnet','Spot','Distance','Role','VS flip','Status'],rows.map(x=>[esc(x.symbol),num(x.magnet),num(x.spot),num((x.distance_pct||0)*100,2)+'%',role(x),flip(x),tag(x.status)]))+(filt.length>50?'<p class="fine">Nearest 50 of '+num(filt.length,0)+' within '+num(within*100,1)+'% shown.</p>':'<p class="fine">'+num(filt.length,0)+' within '+num(within*100,1)+'% of magnet.</p>'):empty('No magnets in range','No magnets within '+num(within*100,1)+'% — widen the Within filter.');
  const sig=(r.signals||[]).slice(0,20);
  $('#apex-signals').innerHTML=sig.length?table(['Time','Symbol','Detail'],sig.map(s=>[when(s.ts),esc(s.symbol),detail(s,['symbol','ts'])])):empty('No signal transitions','No magnet touches or breaks recorded recently.');
  const out=(r.outcomes||[]).slice(0,20);
  $('#apex-outcomes').innerHTML=out.length?table(['Time','Symbol','Detail'],out.map(s=>[when(s.ts),esc(s.symbol),detail(s,['symbol','ts'])])):empty('No session outcomes yet','Outcomes are recorded as sessions close.');
  // Hit rates: separate fetch, evidence-only.
  get('/api/apex-magnets/hit-rates').then(hr=>{
    const states=hr.states||{};
    const order=['broke_through','tested_holding','approaching'];
    const rows=order.filter(k=>states[k]).map(k=>{
      const s=states[k];
      const bd=s.breakdown||{};
      const bdStr=Object.entries(bd).map(([oc,n])=>oc+': '+n).join(', ')||'—';
      return [esc(k),num(s.signals,0),num(s.outcomes_recorded,0),num(s.tested,0),pct1(s.hit_rate),esc(bdStr)];
    });
    $('#apex-hitrats').innerHTML=rows.length?table(['Signal','Signals','Outcomes','Tested','Hit rate','Breakdown'],rows)+'<p class="fine">Lookback '+num(hr.lookback_days,0)+' days · as of '+when(hr.asof)+'.</p>':empty('No hit-rate data','Not enough signal history yet.');
  }).catch(e=>{$('#apex-hitrats').innerHTML=empty('Hit rates unavailable','Could not load /api/apex-magnets/hit-rates.');});
 }

 function renderTape(r){
  $('#tape-asof').textContent='Trailing '+num(r.window_days,0)+' days · evaluated '+when(r.asof);
  const sc=r.scorecard||{},c=sc.confirmed||{},u=sc.unconfirmed||{};
  const row=(label,b)=>[esc(label),num(b.n,0),num(b.wins,0),pct1(b.win_rate),b.mean_r==null?'—':num(b.mean_r,2)+'R'];
  $('#tape-scorecard').innerHTML=table(['Cohort','Trials','Wins','Win rate','Mean R'],[row('Confirmed',c),row('Unconfirmed',u)])+'<p class="fine">Confirmed = setup trial with ≥$250k same-direction flow inside ±15 min, before invalidation. R multiples use stop-distance accounting.</p>';
  const rec=(r.recent_confirmed||[]).slice(0,20);
  $('#tape-recent').innerHTML=rec.length?table(['Evaluated','Trial','Side','Detail'],rec.map(x=>[when(x.evaluated_at),esc(String(x.trial_id||'').slice(0,12)),esc(x.side),detail(x,['trial_id','evaluated_at','side','confirmed'])])):empty('No confirmed trials','No setup trial has been tape-confirmed in the window.');
 }

 function pillars(p){return Object.entries(p||{}).map(([k,v])=>esc(k)+': '+esc(v&&typeof v==='object'?(v.status||'') : v)).join(' · ');}

 function renderGap(r){
  $('#gap-asof').textContent='Evaluated '+when(r.asof);
  const b=(r.board||[]);
  $('#gap-board').innerHTML=b.length?table(['Symbol','Gap','Dir','Stage','Break','Pillars','Qualified'],b.map(s=>[esc(s.symbol),num((s.gap_pct||0)*100,2)+'%',esc(s.direction),esc(s.stage),esc(s.break_direction),pillars(s.pillars),s.veto?tag('vetoed'):tag(s.qualified?'qualified':'watch')])):empty('No gaps today','No ≥1.5% gaps in the curated large-cap list.');
  const w=(r.weekly_outcomes||[]);
  $('#gap-weekly').innerHTML=w.length?table(['Direction','Gap','Confirmations','Dealer','n','Win rate','Mean return'],w.map(x=>[esc(x.direction),esc(x.gap_bucket),num(x.confirmations,0),esc(x.dealer),num(x.n,0),pct1(x.win_rate),num((x.mean_return||0)*100,2)+'%'])):empty('No weekly outcomes yet','Cells accumulate as qualified gaps close out.');
 }

 function renderBreak(r){
  $('#break-asof').textContent='Evaluated '+when(r.asof);
  const f=(r.forming||[]);
  $('#break-forming').innerHTML=f.length?table(['Symbol','Day','Pattern','State','Evaluated'],f.map(s=>[esc(s.symbol),esc(s.day),esc(s.flavor),tag(s.state),when(s.evaluated_at)])):empty('No coils forming','No 15-session triangle coils detected.');
  const fr=(r.fresh||[]).slice(0,25);
  $('#break-fresh').innerHTML=fr.length?table(['Symbol','Day','Pattern','Direction','Level','Vol ×','Confirmation','Status'],fr.map(e=>[esc(e.symbol),esc(e.day),esc(e.pattern),esc(e.direction),num(e.level),e.volume_ratio==null?'—':num(e.volume_ratio,2)+'×',tag(e.confirmation),tag(e.status)])):empty('No fresh breaks','No volume-confirmed range breaks in the last sessions.');
  const tr=(r.traps||[]).slice(0,25);
  $('#break-traps').innerHTML=tr.length?table(['Symbol','Trap day','Type','Fade','Target','Structure','Break day'],tr.map(e=>[esc(e.symbol),esc(e.day),esc(e.pattern),esc(e.direction),num(e.target),esc(e.structure||'credit spread'),esc(e.break_day||'—')])):empty('No traps','No bull/bear traps detected in the last sessions.');
  const w=(r.weekly_outcomes||[]);
  $('#break-weekly').innerHTML=w.length?table(['Pattern','Direction','Confirmation','n','Win rate','Mean 20d'],w.map(x=>[esc(x.pattern),esc(x.direction),esc(x.confirmation),num(x.n,0),pct1(x.win_rate),num((x.mean_d20||0)*100,2)+'%'])):empty('No weekly outcomes yet','Cells accumulate as breaks resolve.');
 }

 function comps(c){return Object.entries(c||{}).map(([k,v])=>esc(k.slice(0,4))+':'+num(v&&v.score,0)).join(' ');}

 function renderBoard(r){
  const rows=r.board||[];
  $('#board-state').innerHTML=(r.frozen?tag('frozen')+' frozen '+when(r.frozen_at):tag('live')+' live until the open')+' · built '+when(r.built_at);
  $('#day-board').innerHTML=rows.length?table(['Symbol','Score','Day %','Components'],rows.map(x=>[esc(x.symbol),num(x.total,0),x.day_pct==null?'—':num(x.day_pct*100,2)+'%',comps(x.components)])):empty('No board yet','The board builds at 08:30 CT on trading days.');
  const rev=(r.weekly_reviews||[]).slice(0,7);
  if(rev.length)$('#day-board').innerHTML+='<h3>Weekly reviews</h3>'+table(['Day','Names','Board hit rate','Off-board alert fraction'],rev.map(v=>[esc(v.day),num(v.names,0),pct1(v.board_hit_rate),pct1(v.off_board_alert_fraction)]));
 }

 function renderPulse(r){
  $('#pulse-asof').textContent='Evaluated '+when(r.asof)+(r.last_scan?' · scan '+when(r.last_scan):'');
  const rows=(r.pulses||[]).slice(0,25);
  const tops=p=>(p.top_prints||[]).slice(0,3).map(t=>esc(t.option_type||'?')+' '+esc(t.strike)+' '+esc(t.expiry)+' $'+num((t.premium||0)/1000,0)+'k').join(' · ');
  $('#pulse-rows').innerHTML=rows.length?table(['Time','Symbol','Dir','Premium $M','Prints','Max score','Top prints'],rows.map(p=>[when(p.ts),esc(p.symbol),p.direction==='bullish'?'<span class="tag good">bullish</span>':'<span class="tag bad">bearish</span>',num((p.directional_premium||0)/1e6,2),num(p.print_count,0),num(p.max_score,0),tops(p)])):empty('No pulses','No concentrated directional flow in the window.');
 }

 async function get(url){const resp=await fetch(url);if(!resp.ok)throw new Error('HTTP '+resp.status);return resp.json();}

 async function loadScanners(){
  if(busy)return;busy=true;
  try{
   const [apex,tape,gap,brk,pulse]=await Promise.allSettled([get('/api/apex-magnets'),get('/api/tape-confirmed'),get('/api/gap-continuation'),get('/api/breakouts'),get('/api/flow-pulse')]);
   if(apex.status==='fulfilled')renderApex(apex.value);else $('#apex-rows').innerHTML=empty('Apex magnets unavailable','Could not load /api/apex-magnets.');
   if(tape.status==='fulfilled')renderTape(tape.value);else $('#tape-scorecard').innerHTML=empty('Tape confirmed unavailable','Could not load /api/tape-confirmed.');
   if(gap.status==='fulfilled')renderGap(gap.value);else $('#gap-board').innerHTML=empty('Gap continuation unavailable','Could not load /api/gap-continuation.');
   if(brk.status==='fulfilled')renderBreak(brk.value);else $('#break-forming').innerHTML=empty('Breakouts unavailable','Could not load /api/breakouts.');
   if(pulse.status==='fulfilled')renderPulse(pulse.value);else $('#pulse-rows').innerHTML=empty('Flow pulse unavailable','Could not load /api/flow-pulse.');
  }finally{busy=false;}
 }

 async function loadBoard(){
  if(boardBusy||!$('#day-board'))return;boardBusy=true;
  try{renderBoard(await get('/api/day-trading-board'));}
  catch(e){$('#day-board').innerHTML=empty('Board unavailable','Could not load /api/day-trading-board.');}
  finally{boardBusy=false;}
 }

 document.querySelector('[data-section="intraday"]')?.addEventListener('click',loadScanners);
 document.querySelector('[data-section="morning-brief"]').addEventListener('click',loadBoard);
 document.querySelector('#apex-within').addEventListener('change',()=>{if(apexCache)renderApex(apexCache);});
 window.addEventListener('hashchange',()=>{if(location.hash==='#intraday')loadScanners();if(location.hash==='#morning-brief')loadBoard();});
 if(location.hash==='#intraday')loadScanners();
 loadBoard();
 setInterval(()=>{if($('#scanners').classList.contains('active'))loadScanners();if($('#overview').classList.contains('active'))loadBoard();},60000);
})();
