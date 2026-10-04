/* Command deck: at-a-glance tile grid for the terminal home view.
   One /api/deck poll feeds every tile; day P&L / positions / watchlist
   reuse the shared /api/state payload via window.__renderTerminal data.
   Read-only — tiles link out to the full research sections. */
(()=>{
'use strict';
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=x=>(x===null||x===undefined||isNaN(Number(x)))?'—':(Number(x)<0?'-$':'$')+Math.abs(Number(x)).toLocaleString(undefined,{minimumFractionDigits:0,maximumFractionDigits:0});
const num=(x,d=0)=>(x===null||x===undefined||isNaN(Number(x)))?'—':Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const up=x=>Number(x)>0?'t-up':(Number(x)<0?'t-down':'');
const chicagoTime=ts=>{try{return new Date(ts*1000).toLocaleTimeString('en-US',{timeZone:'America/Chicago',hour12:false,hour:'2-digit',minute:'2-digit'});}catch(e){return '';}};

function tile(id,eyebrow,body,link,linkText){
  return '<article class="deck-tile" id="deck-'+id+'"><p class="eyebrow">'+esc(eyebrow)+'</p>'+
    '<div class="deck-body">'+body+'</div>'+
    (link?'<a class="deck-link" href="'+esc(link)+'">'+esc(linkText||'Details →')+'</a>':'')+'</article>';
}

function renderDeck(d,state){
  const el=$('#deck-grid');
  if(!el)return;
  const tiles=[];
  // --- Row 1: money ---
  // Day P&L from shared state.
  let day=0,nOpen=0,unreal=0;
  try{
    const today=new Date().toLocaleDateString('en-CA',{timeZone:'America/Chicago'});
    for(const p of (state&&state.trades)||[]){
      if(p.status==='closed'){
        try{if(new Date(p.entered_at*1000).toLocaleDateString('en-CA',{timeZone:'America/Chicago'})===today)day+=Number(p.pnl)||0;}catch(e){}
      }else{unreal+=Number(p.unrealized)||0;nOpen++;}
    }
    day+=unreal;
  }catch(e){}
  tiles.push(tile('pnl','Day P&L',
    '<div class="deck-big '+up(day)+'">'+money(day)+'</div>'+
    '<div class="fine">Unrealized '+money(unreal)+'</div>',null));
  tiles.push(tile('positions','Open positions',
    '<div class="deck-big">'+nOpen+'</div>'+
    '<div class="fine">'+(nOpen?nOpen+(nOpen===1?' position':' positions')+' working':'Flat — nothing working')+'</div>',
    '#terminal','Blotter →'));
  // Futures session.
  const fs=(d&&d.futures_session)||{};
  const sessLabel=fs.is_open?'<span class="t-up">OPEN</span>':'<span class="muted">CLOSED</span>';
  // Countdown to 15:45 CT flatten.
  let flatNote='';
  try{
    const now=new Date();
    const ct=new Date(now.toLocaleString('en-US',{timeZone:'America/Chicago'}));
    const flat=new Date(ct);flat.setHours(15,45,0,0);
    if(fs.is_open&&flat>ct){
      const mins=Math.round((flat-ct)/60000);
      flatNote='<div class="fine">Flatten in '+mins+'m (15:45 CT)</div>';
    }
  }catch(e){}
  tiles.push(tile('futures','Futures session',
    '<div class="deck-big">'+sessLabel+'</div>'+flatNote+
    '<div class="fine">'+esc(fs.day||'—')+'</div>',
    '#futures','Detectors →'));
  // --- Row 2: signals ---
  const pulses=(d&&d.flow_pulse)||[];
  const fp=pulses[0];
  tiles.push(tile('pulse','Flow pulse',
    fp?'<div class="deck-big">'+esc(fp.symbol||'?')+' <span class="'+(fp.direction==='bearish'?'t-down':'t-up')+'">'+esc((fp.direction||'').toUpperCase())+'</span></div>'+
        '<div class="fine">'+money(fp.premium)+' · '+esc(chicagoTime(fp.ts))+'</div>'
      :'<div class="deck-big muted">—</div><div class="fine">No pulses today</div>',
    '#compass','Flow →'));
  const dpk=(d&&d.darkpool)||{};
  tiles.push(tile('darkpool','Dark pool',
    '<div class="deck-big">'+num(dpk.print_count)+'</div>'+
    '<div class="fine">'+(dpk.symbols||[]).slice(0,3).map(esc).join(' · ')+'</div>',
    '#compass','Prints →'));
  const apex=(d&&d.apex)||[];
  tiles.push(tile('apex','Apex magnets',
    '<div class="deck-big">'+apex.length+'</div>'+
    '<div class="fine">'+apex.slice(0,2).map(s=>esc(s.symbol)+' '+(s.state||'')).join(' · ')+'</div>',
    '#compass','Magnets →'));
  const aplas=(d&&d.day_board_aplus)||[];
  tiles.push(tile('dayboard','Day board A+',
    '<div class="deck-big">'+aplas.length+'</div>'+
    '<div class="fine">'+aplas.slice(0,3).map(s=>esc(s.symbol)).join(' · ')+'</div>',
    '#morning-brief','Board →'));
  // --- Row 3: context ---
  const iv=(d&&d.iv_rank)||{};
  const ivRows=Object.keys(iv).map(s=>{
    const r=iv[s]||{};
    return '<div class="deck-row"><span>'+esc(s)+'</span><strong class="'+(r.iv_rank>=70?'t-down':r.iv_rank<=30?'t-up':'')+'">'+num(r.iv_rank)+'</strong><span class="fine">'+esc(r.bias||'')+'</span></div>';
  }).join('');
  tiles.push(tile('iv','IV rank',
    ivRows||'<div class="fine">No ranks yet</div>',
    '#compass','Exposure →'));
  const gm=(d&&d.gamma)||{};
  const gRows=Object.keys(gm).map(s=>{
    const l=gm[s]||{};
    return '<div class="deck-row"><span>'+esc(s)+'</span><span class="fine">C '+num(l.call_wall)+' / P '+num(l.put_wall)+'</span></div>';
  }).join('');
  tiles.push(tile('gamma','Gamma walls',
    gRows||'<div class="fine">No levels yet</div>',
    '#compass','Levels →'));
  const sq=(d&&d.squeeze)||{};
  const sqSyms=Object.keys(sq);
  tiles.push(tile('squeeze','Squeeze',
    sqSyms.length?'<div class="deck-big">'+sqSyms.length+'</div><div class="fine">'+sqSyms.slice(0,4).map(esc).join(' · ')+'</div>'
      :'<div class="deck-big muted">—</div><div class="fine">None squeezed</div>',
    '#compass','States →'));
  el.innerHTML=tiles.join('');
}

let lastDeck=null,lastState=null;
async function poll(){
  try{
    const r=await fetch('/api/deck',{cache:'no-store'});
    if(r.ok){lastDeck=await r.json();renderDeck(lastDeck,lastState);}
  }catch(e){/* tile grid degrades silently; state poll still runs */}
}
// Hook into the shared state render so P&L tiles stay in sync.
const origRender=window.__renderTerminal;
window.__renderTerminal=function(d){
  try{lastState=d;if(lastDeck)renderDeck(lastDeck,lastState);}catch(e){}
  if(origRender)origRender(d);
};
setInterval(poll,30000);
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',poll);
else poll();
})();
