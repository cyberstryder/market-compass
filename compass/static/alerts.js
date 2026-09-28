/* Alerts feed: unified chronological stream of everything firing.
   Sources: 0DTE option setups, Flash Agentic (auto pick-checked),
   scanner signals (apex/tape/gaps), Smoothers featured picks.
   Read-only, evidence-first. */
(function(){
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtT=ts=>{try{return new Date(ts*1000).toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'});}catch(e){return '';}};
const fmtD=ts=>{try{return new Date(ts*1000).toLocaleDateString('en-US',{month:'short',day:'numeric'});}catch(e){return '';}};

async function getJSON(url){
  try{const r=await fetch(url);if(!r.ok)return null;return await r.json();}
  catch(e){return null;}
}

function badge(src){
  const colors={'0DTE':'b-0dte','FLASH':'b-flash','APEX':'b-apex','TAPE':'b-tape','GAP':'b-gap','SMOOTHERS':'b-smooth'};
  return '<span class="src '+(colors[src]||'')+'">'+esc(src)+'</span>';
}

function card(time,src,headline,detail,status){
  return '<article class="alert-card"><div class="ac-head">'+badge(src)+
    '<span class="ac-time">'+esc(time)+'</span>'+
    (status?'<span class="ac-status">'+esc(status)+'</span>':'')+'</div>'+
    '<div class="ac-title">'+headline+'</div>'+
    (detail?'<div class="ac-detail">'+detail+'</div>':'')+'</article>';
}

/* --- 0DTE option setups --- */
function render0DTE(alerts,deliveries){
  const items=[];
  const delivBy={};
  (deliveries||[]).forEach(d=>{
    const p=d.payload||{};
    const key=p.event_id||'';
    if(key)delivBy[key]={at:p.at||d.ts,route:p.route||''};
  });
  (alerts||[]).forEach(a=>{
    const p=a.payload||{};
    if(p.status!=='option_setup_new')return;
    const sym=p.symbol||a.symbol||'';
    const m=sym.match(/^O:([A-Z]+)(\d{6})([CP])(\d{8})/);
    const ticker=m?m[1]:(a.symbol||sym), dir=m?(m[3]==='C'?'CALL':'PUT'):'';
    const entry=p.entry, stop=p.stop, target=p.target;
    const dv=delivBy[a.id];
    const discord=dv?('Discord ✓ '+(dv.route||'')):'Discord: pending';
    const head='<strong>'+esc(ticker)+'</strong> '+esc(dir)+
      ' <span class="mono">'+esc(sym)+'</span>';
    const det=(entry!=null?('Entry '+entry+' · Stop '+stop+' · Target '+target):'')+
      (p.underlying_invalidation?(' · Invalidation '+p.underlying_invalidation):'');
    items.push({t:a.ts||0,html:card(fmtD(a.ts)+' '+fmtT(a.ts),'0DTE',head,det,discord)});
  });
  return items;
}

/* --- Flash Agentic (source=flash_agentic pick checks) --- */
function renderFlash(checks){
  const items=[];
  (checks||[]).forEach(c=>{
    if((c.source||'')!=='flash_agentic')return;
    const dir=(c.direction||'').toUpperCase();
    const pillars=c.pillars||{};
    const order=['apex','tape','gap','breakout','exposure'];
    const pips=order.map(k=>{
      const pl=pillars[k];if(!pl)return '';
      const al=pl.alignment||'';
      const cls=al==='supports'?'p-sup':(al==='contradicts'?'p-con':(al==='neutral'?'p-neu':'p-na'));
      return '<span class="pip '+cls+'" title="'+esc(pl.note||'')+'">'+k+': '+esc(al)+'</span>';
    }).join('');
    const head='<strong>'+esc(c.ticker)+'</strong> '+esc(dir)+
      ' <span class="muted">score '+(c.evidence_score??0)+'</span>';
    const det='Entry '+c.entry+' · Target '+c.target+'<div class="pips">'+pips+'</div>';
    items.push({t:c.at||0,html:card(fmtD(c.at)+' '+fmtT(c.at),'FLASH',head,det,null)});
  });
  return items;
}

/* --- Scanner signals: latest apex transitions + tape confirmed --- */
function renderScanners(apex,tape,gaps){
  const items=[];
  const sigs=(apex&&apex.signals)||[];
  sigs.slice(0,8).forEach(s=>{
    const st=s.signal||s.state||'';
    const head='<strong>'+esc(s.symbol||'')+'</strong> magnet '+esc(st)+
      (s.magnet!=null?(' @ '+s.magnet):'');
    items.push({t:s.ts||s.at||0,html:card(fmtD(s.ts)+' '+fmtT(s.ts),'APEX',head,s.note||'',null)});
  });
  const conf=(tape&&tape.recent_confirmed)||[];
  conf.slice(0,5).forEach(t=>{
    const head='<strong>'+esc(t.symbol||'')+'</strong> '+esc(t.side||'')+
      ' tape-confirmed <span class="muted">score '+(t.score||t.flow_score||'')+'</span>';
    const det=t.premium?('$'+Number(t.premium).toLocaleString()+' premium'):''; 
    items.push({t:t.ts||t.evaluated_at||0,html:card(fmtD(t.ts)+' '+fmtT(t.ts),'TAPE',head,det,null)});
  });
  const board=(gaps&&gaps.board)||[];
  board.slice(0,5).forEach(g=>{
    const head='<strong>'+esc(g.symbol||'')+'</strong> gap '+esc(g.gap_direction||g.direction||'')+
      (g.gap_pct!=null?(' '+g.gap_pct+'%'):'');
    items.push({t:g.ts||0,html:card(fmtD(g.ts)+' '+fmtT(g.ts),'GAP',head,g.stage||'',null)});
  });
  return items;
}

async function load(){
  const el=$('#alerts-feed');
  if(!el)return;
  el.innerHTML='<p class="muted">Loading alerts…</p>';
  const [alerts,deliveries,picks,apex,tape,gaps]=await Promise.all([
    getJSON('/api/events?kind=alert&limit=100'),
    getJSON('/api/events?kind=alert_delivery&limit=100'),
    getJSON('/api/pick-check/recent?limit=30'),
    getJSON('/api/apex-magnets'),
    getJSON('/api/tape-confirmed'),
    getJSON('/api/gap-continuation'),
  ]);
  let items=[];
  items=items.concat(render0DTE(alerts,deliveries));
  items=items.concat(renderFlash((picks&&picks.checks)||[]));
  items=items.concat(renderScanners(apex,tape,gaps));
  items.sort((a,b)=>b.t-a.t);
  if(!items.length){el.innerHTML='<p class="muted">No alerts yet today. 0DTE setups, Flash Agentic triggers, and scanner signals will appear here as they fire.</p>';return;}
  el.innerHTML='<div class="alert-stream">'+items.slice(0,60).map(i=>i.html).join('')+'</div>'+
    '<p class="fine">Showing '+Math.min(items.length,60)+' of '+items.length+'. 0DTE Discord status reflects the last known delivery record.</p>';
  const ts=$('#alerts-updated');
  if(ts)ts.textContent='Updated '+new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',second:'2-digit'});
}

let timer=null;
function start(){
  load();
  if(timer)clearInterval(timer);
  timer=setInterval(()=>{const s=$('#alerts');if(s&&s.classList.contains('active'))load();},60000);
}
window.AlertsFeed={start,reload:load};
document.addEventListener('DOMContentLoaded',start);
})();
