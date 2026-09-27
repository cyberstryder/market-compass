/* Smoothers home tab: weekly roster, featured A+ picks, sender status. Read-only views; the handoff controls are wired by native.js. */
(()=>{
 let busy=false,histBusy=false;

 function mondayOf(d){
  const x=new Date(d.getFullYear(),d.getMonth(),d.getDate());
  const dow=(x.getDay()+6)%7;x.setDate(x.getDate()-dow);return x;
 }
 const iso=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');

 async function get(url){const r=await fetch(url);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}

 function statusCell(s){return tag(s||'—');}

 function rosterTable(rows){
  if(!rows.length)return empty('No signals this week','The weekly selection has not run yet or produced no signals.');
  return table(['Ticker','Dir','Entry','Target','ATR×','Premium','Est ret','Status'],rows.map(x=>{const n=x.native||{};
   return [esc(n.ticker),esc(n.direction),num(n.entry_price),num(n.target_price),
    n.target_atr_mult==null?'—':num(n.target_atr_mult,2)+'×',
    n.entry_premium==null?'—':'$'+num(n.entry_premium),
    n.est_return_pct==null?'—':num(n.est_return_pct,1)+'%',
    statusCell(n.status)+(n.is_featured?' ★':'')];}));
 }

 function renderOwnership(h){
  const o=h.ownership||{},owner=o.owner||'original';
  const plan=h.plan||{};
  $('#smoothers-handoff-asof').textContent='Checked '+when(Date.now()/1000);
  $('#smoothers-ownership').innerHTML=table(['Sender','State'],[
   ['Official Smoothers sender',owner==='compass'?'<strong>Compass (native)</strong>':'Original app'],
   ['Sender environment',h.sender_environment_enabled?'Enabled':'Disabled — set NATIVE_PROGRAM_SEND_ENABLED=true in Railway'],
   ['Discord webhook',h.webhook_configured?'Configured':'Missing — set NATIVE_SMOOTHERS_DISCORD_WEBHOOK in Railway'],
   ['Handoff plan',plan.id?esc(plan.state)+' · review '+esc(plan.review_week||'')+' → effective '+esc(plan.effective_week||''):'None prepared'],
   ['In-flight / uncertain deliveries',num(h.in_flight,0)+' / '+num(h.unknown_delivery,0)]])
   +'<p class="fine">Until a handoff plan is prepared, reviewed and armed below, Monday selections stay in shadow mode and the original app remains the official sender. Arming takes effect at the next weekly cohort; it never pauses the original app for you.</p>';
 }

 async function loadHandoff(){
  try{renderOwnership(await get('/api/native/smoothers/handoff'));}
  catch(e){$('#smoothers-ownership').innerHTML=empty('Sender status unavailable','Could not load /api/native/smoothers/handoff.');}
 }

 async function loadWeek(week){
  const r=await get('/api/native/report?week='+week),s=r.smoothers||{};
  const rows=s.rows||[],feat=rows.filter(x=>x.native&&x.native.is_featured);
  $('#smoothers-featured-asof').textContent='Week of '+esc(s.week||week)+' · selection '+(s.job&&s.job.state?esc(s.job.state):'not started');
  $('#smoothers-featured').innerHTML=feat.length?rosterTable(feat):empty('No featured picks','No signals met the featured bar this week, or selection has not run yet.');
  $('#smoothers-roster').innerHTML='<p class="fine">Week of '+esc(s.week||week)+' · '+(s.job&&s.job.state?esc(s.job.state):'not started')+' · '+num(rows.length,0)+' signals.</p>'+rosterTable(rows);
 }

 async function loadHistory(){
  if(histBusy)return;histBusy=true;
  try{
   const mondays=[];let d=mondayOf(new Date());
   for(let i=0;i<5;i++){const m=new Date(d);m.setDate(m.getDate()-7*i);mondays.push(iso(m));}
   const settled=await Promise.allSettled(mondays.map(w=>get('/api/native/report?week='+w).then(r=>({week:w,s:r.smoothers||{}}))));
   const rows=settled.filter(x=>x.status==='fulfilled').map(x=>x.value).filter(x=>(x.s.rows||[]).length);
   $('#smoothers-history').innerHTML=rows.length?table(['Week','Signals','WIN','LOSS','Unresolved','Featured hit'],
    rows.map(({week,s})=>{const ns=(s.rows||[]).map(x=>x.native||{});
     const c=k=>ns.filter(n=>n.status===k).length;
     const f=ns.filter(n=>n.is_featured),fh=f.filter(n=>n.status==='WIN').length;
     return [esc(week),num(ns.length,0),num(c('WIN'),0),num(c('LOSS'),0),num(c('UNRESOLVED'),0),f.length?fh+'/'+f.length:'—'];}))
    +'<p class="fine">Unresolved weeks are excluded from hit-rate math; the roster above shows why.</p>'
    :empty('No weekly history','No completed native weeks found in the last five.');
  }catch(e){$('#smoothers-history').innerHTML=empty('History unavailable','Could not load past weeks.');}
  finally{histBusy=false;}
 }

 async function loadSmoothers(){
  if(busy)return;busy=true;
  try{
   const input=$('#smoothers-week');
   if(!input.value)input.value=iso(mondayOf(new Date()));
   const week=iso(mondayOf(new Date(input.value+'T12:00:00')));
   input.value=week;
   await Promise.all([loadWeek(week).catch(()=>{$('#smoothers-roster').innerHTML=empty('Roster unavailable','Could not load /api/native/report.');}),loadHandoff(),loadHistory()]);
  }finally{busy=false;}
 }

 document.querySelector('[data-tab="smoothers"]').addEventListener('click',loadSmoothers);
 $('#smoothers-week').addEventListener('change',()=>{busy=false;loadSmoothers();});
 window.addEventListener('hashchange',()=>{if(location.hash==='#smoothers')loadSmoothers();});
 if(location.hash==='#smoothers')loadSmoothers();
 setInterval(()=>{if($('#smoothers').classList.contains('active'))loadSmoothers();},60000);
})();
