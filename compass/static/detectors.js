/* Futures section: ICT detectors, YouTube-trader methods, paper attribution,
   plus the intraday magnet-break push status. Read-only views of existing
   JSON endpoints — no scanning, alerting, or order behavior. */
(function(){
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,d=2)=>x===null||x===undefined?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
const when=x=>x?new Date(x*1000).toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'—';
const empty=(t,s)=>'<div class="empty"><strong>'+esc(t)+'</strong>'+esc(s)+'</div>';
function table(head,rows){return '<table><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
async function getJSON(url){try{const r=await fetch(url);if(!r.ok)return null;return await r.json();}catch(e){return null;}}

function renderSignals(d){
  const sigs=(d.signals||[]).slice(0,25);
  if(!sigs.length)return empty('No recent signals','The detector has not fired recently. Evidence only.');
  return table(['Time','Symbol','Detail'],sigs.map(s=>{
    const rest=Object.assign({},s);delete rest.symbol;delete rest.ts;delete rest.signal_ts;
    const det=Object.entries(rest).slice(0,6).map(([k,v])=>esc(k.replaceAll('_',' '))+': '+esc(typeof v==='object'?JSON.stringify(v):String(v))).join(' · ');
    return [when(s.ts||s.signal_ts),esc(s.symbol||'—'),esc(det).slice(0,220)];
  }));
}

function renderRegimes(d){
  const rows=d.regime_rows||[];
  if(!rows.length)return empty('No regime data','The daily scan has not produced regimes yet. Evidence only.');
  return table(['Symbol','Regime','Close','SMA','Distance %'],rows.map(r=>[
    esc(r.symbol||'—'),
    r.excluded?esc('no data ('+r.excluded+')'):'<strong>'+esc(r.regime||'—')+'</strong>',
    r.close==null?'—':num(r.close,2),r.sma==null?'—':num(r.sma,2),
    r.dist_pct==null?'—':num(r.dist_pct,2)+'%'
  ]));
}

async function loadDetector(selId,bodyId,asofId){
  const sel=$(selId),body=$(bodyId);if(!sel||!body)return;
  const d=await getJSON(sel.value);
  if(!d){body.innerHTML=empty('Detector unavailable','Could not load '+esc(sel.value)+'.');return;}
  if(asofId&&$(asofId))$(asofId).textContent=d.asof?('as of '+when(d.asof)):'';
  body.innerHTML='<p class="fine">'+esc(d.note||'Research evidence only.')+'</p>'+
    '<p>'+num((d.signals||[]).length,0)+' recent signals'+(d.params&&Object.keys(d.params).length?' · <span class="fine">'+esc(JSON.stringify(d.params)).slice(0,200)+'</span>':'')+'</p>'+
    (d.regime_rows?renderRegimes(d):renderSignals(d));
}

async function loadAttribution(){
  const body=$('#ict-paper-attribution');if(!body)return;
  const d=await getJSON('/api/ict-paper-attribution');
  if(!d){body.innerHTML=empty('Attribution unavailable','Could not load /api/ict-paper-attribution.');return;}
  $('#ict-paper-asof').textContent=d.asof?('as of '+when(d.asof)):'';
  const rows=Object.entries(d.attribution||{});
  body.innerHTML=rows.length?table(['Method','Trades','Wins','Losses','Realized $'],
    rows.sort((a,b)=>b[1].realized-a[1].realized).map(([k,v])=>[esc(k),num(v.trades,0),num(v.wins,0),num(v.losses,0),num(v.realized)]))
    :empty('No closed paper trades yet','Paper trading is gated behind ICT_FUTURES_PAPER_ENABLED plus each detector flag.');
}

async function loadMagnetPush(){
  const body=$('#magnet-push-status');if(!body)return;
  const d=await getJSON('/api/alerts/routes');
  const routes=d.routes||d||[];
  const m=(Array.isArray(routes)?routes:[]).find(r=>r.route==='magnets');
  $('#magnet-push-asof').textContent='Checked '+when(Date.now()/1000);
  if(!m){body.innerHTML=empty('Magnets route not found','The route manifest did not include a magnets route.');return;}
  const h=m.health||{};
  body.innerHTML=table(['Route','Status','Destination','Pending'],[[
    '#'+esc(m.channel||'intraday'),
    esc(h.status||m.status||'unknown')+(h.message?'<br><span class="fine">'+esc(h.message)+'</span>':''),
    esc(m.destination_mode||'—'),
    num(m.pending,0)
  ]])+'<p class="fine">Full route health and Discord delivery receipts live under Compass → Feed health.</p>';
}

function refresh(section){
  if(section==='futures'){loadDetector('#ict-detector-select','#ict-detector-body','#ict-detector-asof');loadDetector('#yt-method-select','#yt-method-body','#yt-method-asof');loadAttribution();}
  if(section==='intraday'){loadMagnetPush();}
}
window.__detectorsRefresh=refresh;
const ictSel=$('#ict-detector-select');if(ictSel)ictSel.onchange=()=>loadDetector('#ict-detector-select','#ict-detector-body','#ict-detector-asof');
const ytSel=$('#yt-method-select');if(ytSel)ytSel.onchange=()=>loadDetector('#yt-method-select','#yt-method-body','#yt-method-asof');
setInterval(()=>{const open=document.querySelector('.acc-section.open');if(open)refresh(open.id.replace('sec-',''));},60000);
})();
