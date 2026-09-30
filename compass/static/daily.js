/* Daily status page: one trading day across every section. Read-only. */
(()=>{
let busy=false;
async function get(url){const r=await fetch(url);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,d=2)=>x===null||x===undefined?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
const money=x=>x===null||x===undefined?'—':(x<0?'-$':'$')+num(Math.abs(x));
const when=x=>x?new Date(x*1000).toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})+' CT':'—';
const resultTag=r=>'<span class="tag '+(r==='win'?'good':r==='loss'?'bad':'')+'">'+esc(r)+'</span>';
const shortStrategy=s=>esc(String(s||'—').replace(/^0dte-/,''));
function table(head,rows){return '<table><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
function statRow(items){return '<div class="stats">'+items.map(x=>'<div class="stat"><div class="label">'+esc(x[0])+'</div><div class="value">'+x[1]+'</div>'+(x[2]?'<div class="fine">'+esc(x[2])+'</div>':'')+'</div>').join('')+'</div>';}
function go(tab,label){return '<p><button type="button" data-goto="'+tab+'">Open '+esc(label)+' →</button></p>';}
function takenRows(rows){
 if(!rows.length)return '<p class="fine">Nothing taken.</p>';
 return table(['Time','Symbol','Side','Strategy','Qty','Entry','Exit','P&L','Result'],rows.map(r=>
  [when(r.entered_at),esc(r.symbol),esc(r.side||'—'),shortStrategy(r.strategy),num(r.qty,0),num(r.entry),r.exit==null?'—':num(r.exit),r.pnl==null?'—':money(r.pnl),resultTag(r.result)]));
}
function paperStats(s){
 return statRow([['Taken',num(s.taken,0)],['Wins',num(s.wins,0)],['Losses',num(s.losses,0)],['Open',num(s.open,0)],['Net P&L',money(s.net_pnl)]]);
}
function renderBody(d){
 const p=d.paper,z=p.zero_dte,f=p.futures;
 let h='<div class="daily-sec">';
 h+='<h3>At a glance</h3>'+statRow([
  ['0DTE net P&L',money(z.net_pnl),'Taken '+z.taken+' · open '+z.open],
  ['Futures net P&L',money(f.net_pnl),'Taken '+f.taken+' · open '+f.open],
  ['Alerts sent',num(d.alerts.sent,0),'Across all channels'],
  ['Apex signals',num(d.scanners.apex_signals,0),'Tape confirmations '+d.scanners.tape_confirmations]]);
 h+='<h3>0DTE options</h3>'+paperStats(z);
 if(z.skipped.length)h+='<h3>Top skip reasons</h3>'+table(['Symbol','Why not','Times'],z.skipped.slice(0,5).map(r=>[esc(r.symbol),esc(r.reason),num(r.count,0)]));
 h+='<h3>Recent 0DTE trades</h3>'+takenRows(z.trades.slice(0,5))+go('0dte','0DTE');
 h+='<h3>Futures</h3>'+paperStats(f);
 if(f.strategies.length)h+='<h3>By strategy</h3>'+table(['Strategy','Taken','W / L','Open','Today P&L'],f.strategies.slice(0,8).map(s=>[esc(s.strategy),num(s.taken,0),num(s.wins,0)+' / '+num(s.losses,0),num(s.open,0),money(s.realized)]));
 h+='<h3>Recent futures trades</h3>'+takenRows(f.trades.slice(0,5))+go('futures','Futures');
 const b=d.board;
 h+='<h3>Morning brief</h3>'+statRow([
  ['Board',b.built?'Built':'Not built',b.frozen?'Frozen at open':''],
  ['Board names',num(b.rows,0),'Universe '+(b.universe??'—')],
  ['Pick checks',num(d.pick_checks.count,0),(d.pick_checks.tickers.join(', ')||'None today')],
  ['Smoothers featured',num(d.smoothers.featured.length,0),'Week of '+d.smoothers.week]]);
 if(b.top.length)h+=table(['Top board names','Score','Day %'],b.top.slice(0,5).map(r=>[esc(r.symbol),num(r.score,1),r.day_pct==null?'—':num(r.day_pct)+'%']));
 h+=go('morning-brief','Morning Brief');
 const sm=d.smoothers;
 h+='<h3>Smoothers · week of '+esc(sm.week)+'</h3>';
 h+=sm.available?(sm.featured.length?table(['Ticker','Dir','Status'],sm.featured.map(x=>[esc(x.ticker),esc(x.direction),esc(x.status||'—')])):'<p class="fine">No featured picks this week yet.</p>'):'<p class="fine">Weekly report unavailable.</p>';
 h+=go('smoothers','Smoothers')+go('ideas','Ideas');
 const sc=d.scanners;
 h+='<h3>Compass scanners</h3>'+statRow([
  ['Apex signals',num(sc.apex_signals,0),Object.entries(sc.apex_by_state).map(([k,v])=>k.replaceAll('_',' ')+' '+v).join(' · ')||'None'],
  ['Tape confirmations',num(sc.tape_confirmations,0),(sc.tape_symbols.join(', ')||'None')],
  ['Gaps qualified',num(sc.gaps_qualified,0),'≥1.5% large-cap gaps'],
  ['Fresh breakouts',num(sc.breakouts_fresh,0),'Range breaks + triangle fires']]);
 if(sc.apex_broke_through.length)h+='<h3>Magnet breaks</h3>'+table(['Time','Symbol','Spot','Magnet'],sc.apex_broke_through.slice(0,5).map(r=>[when(r.ts),esc(r.symbol),num(r.spot),num(r.level)]));
 if(sc.gaps.length)h+='<h3>Top gaps</h3>'+table(['Symbol','Gap','Direction'],sc.gaps.slice(0,5).map(g=>[esc(g.symbol),num(g.gap_pct)+ '%',esc(g.direction||'—')]));
 if(sc.breakouts.length)h+='<h3>Fresh breakouts</h3>'+table(['Symbol','Pattern','Direction'],sc.breakouts.slice(0,5).map(b2=>[esc(b2.symbol),esc(b2.pattern||'—'),esc(b2.direction||'—')]));
 if(d.alerts.sent&&Object.keys(d.alerts.by_strategy).length)h+='<h3>Alerts by strategy</h3>'+table(['Strategy','Sent'],Object.entries(d.alerts.by_strategy).sort((a,b)=>b[1]-a[1]).slice(0,8).map(([k,v])=>[esc(k),num(v,0)]));
 h+=go('compass','Compass');
 return h+'</div>';
}
async function render(){
 if(busy)return;busy=true;
 const day=$('#daily-day').value;
 $('#daily-body').innerHTML='<p class="muted">Loading…</p>';
 try{
  const d=await get('/api/daily-status?day='+encodeURIComponent(day));
  $('#daily-asof').textContent='Data as of '+when(d.asof);
  $('#daily-note').textContent=d.day===todayCT()?'Today — numbers update as the session runs.':'Historical day — paper P&L uses the 17:00 CT risk-day rollover.';
  $('#daily-body').innerHTML=renderBody(d);
 }catch(e){
  $('#daily-body').innerHTML='<div class="empty"><strong>Daily status unavailable</strong>'+esc(e.message)+'</div>';
 }
 if(window.__applyPanels)window.__applyPanels();
 busy=false;
}
function todayCT(){
 return new Intl.DateTimeFormat('en-CA',{timeZone:'America/Chicago',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
}
document.addEventListener('click',e=>{
 const b=e.target.closest('#daily-body [data-goto]');
 if(b&&window.selectTab)selectTab(b.dataset.goto);
});
$('#daily-load').onclick=render;
$('#daily-day').value=todayCT();
window.__renderDaily=render;
const _h=(location.hash||'').slice(1);
if(!_h||_h==='daily')render();
})();
