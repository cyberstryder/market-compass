/* Evidence scanner panels. Endpoints are research evidence only: no alerts, no trades. */
(()=>{
 let busy=false,boardBusy=false;
 const detail=(o,skip)=>Object.entries(o||{}).filter(([k])=>!(skip||[]).includes(k)).map(([k,v])=>esc(k)+': '+esc(typeof v==='object'?JSON.stringify(v):String(v??''))).join(' · ');
 const pct1=x=>x===null||x===undefined?'—':num(x*100,1)+'%';

 function renderApex(r){
  $('#apex-asof').textContent='Snapshot '+when(r.asof)+' · radius '+esc(r.radius??'—')+' · tolerance '+esc(r.tolerance??'—');
  const rows=(r.rows||[]).slice(0,50);
  $('#apex-rows').innerHTML=rows.length?table(['Symbol','Magnet','Spot','Distance','Role','Status'],rows.map(x=>[esc(x.symbol),num(x.magnet),num(x.spot),num((x.distance_pct||0)*100,2)+'%',esc(x.role),tag(x.status)]))+(r.rows.length>50?'<p class="fine">Nearest 50 of '+num(r.rows.length,0)+' shown.</p>':''):empty('No magnets in range','The latest vendor snapshot has no magnets within radius.');
  const sig=(r.signals||[]).slice(0,20);
  $('#apex-signals').innerHTML=sig.length?table(['Time','Symbol','Detail'],sig.map(s=>[when(s.ts),esc(s.symbol),detail(s,['symbol','ts'])])):empty('No signal transitions','No magnet touches or breaks recorded recently.');
  const out=(r.outcomes||[]).slice(0,20);
  $('#apex-outcomes').innerHTML=out.length?table(['Time','Symbol','Detail'],out.map(s=>[when(s.ts),esc(s.symbol),detail(s,['symbol','ts'])])):empty('No session outcomes yet','Outcomes are recorded as sessions close.');
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

 async function get(url){const resp=await fetch(url);if(!resp.ok)throw new Error('HTTP '+resp.status);return resp.json();}

 async function loadScanners(){
  if(busy)return;busy=true;
  try{
   const [apex,tape,gap,brk]=await Promise.allSettled([get('/api/apex-magnets'),get('/api/tape-confirmed'),get('/api/gap-continuation'),get('/api/breakouts')]);
   if(apex.status==='fulfilled')renderApex(apex.value);else $('#apex-rows').innerHTML=empty('Apex magnets unavailable','Could not load /api/apex-magnets.');
   if(tape.status==='fulfilled')renderTape(tape.value);else $('#tape-scorecard').innerHTML=empty('Tape confirmed unavailable','Could not load /api/tape-confirmed.');
   if(gap.status==='fulfilled')renderGap(gap.value);else $('#gap-board').innerHTML=empty('Gap continuation unavailable','Could not load /api/gap-continuation.');
   if(brk.status==='fulfilled')renderBreak(brk.value);else $('#break-forming').innerHTML=empty('Breakouts unavailable','Could not load /api/breakouts.');
  }finally{busy=false;}
 }

 async function loadBoard(){
  if(boardBusy||!$('#day-board'))return;boardBusy=true;
  try{renderBoard(await get('/api/day-trading-board'));}
  catch(e){$('#day-board').innerHTML=empty('Board unavailable','Could not load /api/day-trading-board.');}
  finally{boardBusy=false;}
 }

 document.querySelector('[data-tab="scanners"]').addEventListener('click',loadScanners);
 document.querySelector('[data-tab="overview"]').addEventListener('click',loadBoard);
 window.addEventListener('hashchange',()=>{if(location.hash==='#scanners')loadScanners();if(location.hash==='#overview'||location.hash===''||location.hash==='#')loadBoard();});
 if(location.hash==='#scanners')loadScanners();
 loadBoard();
 setInterval(()=>{if($('#scanners').classList.contains('active'))loadScanners();if($('#overview').classList.contains('active'))loadBoard();},60000);
})();
