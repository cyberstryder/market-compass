/* Terminal home view: top-bar day P&L / clock / connection dot, open-positions
   blotter, today's closed trades, and the watchlist quote rail. Read-only —
   renders from the same /api/state payload the research sections use.
   No backend changes, no new endpoints. */
(()=>{
'use strict';
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,d=2)=>(x===null||x===undefined||x===''||isNaN(Number(x)))?'—':Number(x).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const money=x=>(x===null||x===undefined||isNaN(Number(x)))?'—':(Number(x)<0?'-$':'$')+Math.abs(Number(x)).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const pnlClass=x=>Number(x)<0?'t-down':(Number(x)>0?'t-up':'');
const sideTag=s=>s==='short'?'<span class="t-down">SHORT</span>':s==='long'?'<span class="t-up">LONG</span>':esc(s);
const chicagoDay=ts=>{try{return new Date(ts*1000).toLocaleDateString('en-CA',{timeZone:'America/Chicago'});}catch(e){return '';}};
const chicagoTime=ts=>{try{return new Date(ts*1000).toLocaleTimeString('en-US',{timeZone:'America/Chicago',hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});}catch(e){return '—';}};
let lastOk=0;

function setHTML(sel,html){const el=$(sel);if(el)el.innerHTML=html;}

function renderTopbar(d){
  const trades=Array.isArray(d.trades)?d.trades:[];
  const today=chicagoDay(Date.now()/1000);
  let unreal=0,realized=0,nOpen=0;
  for(const p of trades){
    if(p.status==='closed'){if(chicagoDay(p.entered_at)===today)realized+=Number(p.pnl)||0;}
    else{unreal+=Number(p.unrealized)||0;nOpen++;}
  }
  const day=realized+unreal;
  const pnl=$('#term-pnl');
  if(pnl){
    pnl.textContent=(day<0?'-$':'+$')+Math.abs(day).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    pnl.classList.toggle('up',day>0);
    pnl.classList.toggle('down',day<0);
  }
  const oc=$('#term-open');
  if(oc)oc.textContent=nOpen+(nOpen===1?' open':' open');
  const note=$('#term-open-note');
  if(note)note.textContent='Unrealized '+money(unreal);
  const tn=$('#term-today-note');
  if(tn)tn.textContent='Realized '+money(realized);
}

function renderPositions(d){
  const open=(Array.isArray(d.trades)?d.trades:[]).filter(p=>p.status==='open');
  if(!open.length){setHTML('#term-positions','<div class="empty"><strong>Flat</strong>No open positions.</div>');return;}
  const rows=open.map(p=>{
    const u=Number(p.unrealized)||0;
    return '<tr><td>'+esc(p.symbol)+'</td><td>'+esc(p.strategy||'—')+'</td><td>'+sideTag(p.side)+'</td><td class="num">'+esc(p.qty)+'</td>'+
      '<td class="num">'+num(p.entry)+'</td><td class="num">'+num(p.stop)+'</td><td class="num">'+num(p.target)+'</td>'+
      '<td class="num '+pnlClass(u)+'">'+money(u)+'</td>'+
      '<td class="mono">'+esc(chicagoTime(p.entered_at))+'</td></tr>';
  }).join('');
  setHTML('#term-positions','<table><thead><tr><th>Symbol</th><th>Strategy</th><th>Side</th><th class="num">Qty</th><th class="num">Entry</th><th class="num">Stop</th><th class="num">Target</th><th class="num">Unreal</th><th>Entered</th></tr></thead><tbody>'+rows+'</tbody></table>');
}

function renderToday(d){
  const today=chicagoDay(Date.now()/1000);
  const closed=(Array.isArray(d.trades)?d.trades:[]).filter(p=>p.status==='closed'&&chicagoDay(p.entered_at)===today);
  if(!closed.length){setHTML('#term-today','<div class="empty"><strong>Nothing closed yet</strong>Trades closed today will appear here.</div>');return;}
  const rows=closed.map(p=>{
    const r=Number(p.pnl)||0;
    return '<tr><td>'+esc(p.symbol)+'</td><td>'+esc(p.strategy||'—')+'</td><td>'+sideTag(p.side)+'</td><td class="num">'+esc(p.qty)+'</td>'+
      '<td class="num">'+num(p.entry)+'</td><td class="num">'+num(p.exit)+'</td>'+
      '<td class="num '+pnlClass(r)+'">'+money(r)+'</td>'+
      '<td class="mono">'+esc(chicagoTime(p.exited_at||p.closed_at||p.entered_at))+'</td></tr>';
  }).join('');
  setHTML('#term-today','<table><thead><tr><th>Symbol</th><th>Strategy</th><th>Side</th><th class="num">Qty</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">Realized</th><th>Exited</th></tr></thead><tbody>'+rows+'</tbody></table>');
}

function renderWatchlist(d){
  const q=(d&&d.quotes)||{};
  const syms=Object.keys(q).sort();
  if(!syms.length){setHTML('#term-watchlist','<div class="empty"><strong>No quotes yet</strong>Waiting for the first state poll.</div>');return;}
  const rows=syms.map(s=>{
    const r=q[s]||{};
    const age=(r.age===null||r.age===undefined)?'—':Number(r.age).toFixed(0)+'s';
    return '<tr><td>'+esc(s)+'</td><td class="num">'+num(r.bid)+'</td><td class="num">'+num(r.ask)+'</td><td class="num mono">'+esc(age)+'</td></tr>';
  }).join('');
  setHTML('#term-watchlist','<table><thead><tr><th>Symbol</th><th class="num">Bid</th><th class="num">Ask</th><th class="num">Age</th></tr></thead><tbody>'+rows+'</tbody></table>');
}

function renderTerminal(d){
  try{
    if(!d||typeof d!=='object')return;
    renderTopbar(d);
    renderPositions(d);
    renderToday(d);
    renderWatchlist(d);
    lastOk=Date.now();
  }catch(e){/* never break the shared poll loop */}
}
window.__renderTerminal=renderTerminal;

function tick(){
  try{
    const c=$('#term-clock');
    if(c)c.textContent=new Date().toLocaleTimeString('en-US',{timeZone:'America/Chicago',hour12:false});
    const dot=$('#term-conn');
    if(dot){
      const age=Date.now()-lastOk;
      dot.classList.toggle('ok',lastOk>0&&age<8000);
      dot.classList.toggle('warn',lastOk>0&&age>=8000&&age<30000);
      dot.title=lastOk?('State feed '+(age<8000?'live':age<30000?'stale':'down')+', last update '+Math.round(age/1000)+'s ago'):'State feed: no successful poll yet';
    }
    const tg=$('#research-toggle');
    if(tg)tg.classList.toggle('in',!!document.querySelector('#research-drop .nav.active'));
  }catch(e){}
}

function wireDropdown(){
  const wrap=$('#research-drop-wrap'),tg=$('#research-toggle');
  if(!wrap||!tg)return;
  tg.addEventListener('click',e=>{
    e.stopPropagation();
    const open=wrap.classList.toggle('open');
    tg.setAttribute('aria-expanded',open?'true':'false');
  });
  document.addEventListener('click',e=>{if(!wrap.contains(e.target))wrap.classList.remove('open');});
  document.addEventListener('keydown',e=>{if(e.key==='Escape')wrap.classList.remove('open');});
  const drop=$('#research-drop');
  if(drop)drop.addEventListener('click',e=>{if(e.target.closest('.nav'))wrap.classList.remove('open');});
}

setInterval(tick,1000);
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',()=>{wireDropdown();tick();});
else{wireDropdown();tick();}
})();
