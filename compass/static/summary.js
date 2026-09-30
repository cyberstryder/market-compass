/* Summary tab: what we took, what we skipped, and why. Read-only. */
(()=>{
let busy=false;

async function get(url){const r=await fetch(url);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}

const money=x=>x===null||x===undefined?'—':(x<0?'-$':'$')+num(Math.abs(x));
const resultTag=r=>'<span class="tag '+(r==='win'?'good':r==='loss'?'bad':'')+'">'+esc(r)+'</span>';
const shortStrategy=s=>esc(String(s||'—').replace(/^0dte-/,''));

function statRow(s){
 return '<div class="stats">'+[['Taken',num(s.taken,0)],['Wins',num(s.wins,0)],['Losses',num(s.losses,0)],['Open',num(s.open,0)],['Net P&L',money(s.net_pnl)]].map(x=>'<div class="stat"><div class="label">'+x[0]+'</div><div class="value">'+x[1]+'</div></div>').join('')+'</div>';
}

function takenTable(rows){
 if(!rows.length)return empty('Nothing taken','No paper entries in this bucket today.');
 return table(['Time','Symbol','Side','Strategy','Qty','Entry','Exit','P&L','Result'],rows.map(r=>
  [when(r.entered_at),esc(r.symbol),esc(r.side||'—'),shortStrategy(r.strategy),num(r.qty,0),num(r.entry),r.exit==null?'—':num(r.exit),r.pnl==null?'—':money(r.pnl),resultTag(r.result)]));
}

function skippedTable(rows){
 if(!rows.length)return '<p class="fine">Nothing skipped.</p>';
 return '<h3>Not taken</h3>'+table(['Symbol','Why not','Times'],rows.map(r=>[esc(r.symbol),esc(r.reason),num(r.count,0)]));
}

function bucketBlock(b){
 return statRow(b)+'<h3>Taken</h3>'+takenTable(b.trades)+skippedTable(b.skipped);
}

function futuresBlock(f){
 let h=statRow(f);
 if(f.strategies.length)
  h+='<h3>By strategy</h3>'+table(['Strategy','Taken','Long / Short','W / L','Open','Today P&L','All-time P&L'],f.strategies.map(s=>
   [esc(s.strategy),num(s.taken,0),num(s.long,0)+' / '+num(s.short,0),num(s.wins,0)+' / '+num(s.losses,0),num(s.open,0),money(s.realized),money(s.all_time_realized)]));
 h+='<h3>Trades</h3>'+takenTable(f.trades);
 return h;
}

async function loadSmoothers(){
 try{
  const d=new Date(),dow=(d.getDay()+6)%7;d.setDate(d.getDate()-dow);
  const week=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  const r=await get('/api/native/report?week='+week),s=r.smoothers||{};
  const rows=(s.rows||[]).filter(x=>x.native&&x.native.is_featured);
  $('#summary-smoothers-week').textContent='Week of '+(s.week||week);
  $('#summary-smoothers').innerHTML=(rows.length?table(['Ticker','Dir','Entry','Target','Status'],rows.map(x=>{const n=x.native;
   return [esc(n.ticker),esc(n.direction),num(n.entry_price),num(n.target_price),esc(n.status||'—')];}))
   :'<p class="fine">No featured picks this week yet.</p>')
   +'<p><button type="button" id="summary-smoothers-open">Open the full Smoothers page</button></p>';
  $('#summary-smoothers-open').onclick=()=>selectTab('smoothers');
 }catch(e){$('#summary-smoothers').innerHTML=empty('Smoothers glance unavailable','Could not load the weekly report.');}
}

async function render(){
 if(busy)return;busy=true;
 try{
  const d=await get('/api/summary');
  $('#summary-day').textContent='Trading day '+d.day;
  $('#summary-0dte').innerHTML=bucketBlock(d.zero_dte);
  $('#summary-futures').innerHTML=futuresBlock(d.futures);
 }catch(e){
  const msg=empty('Summary unavailable','Could not load /api/summary.');
  $('#summary-0dte').innerHTML=msg;$('#summary-futures').innerHTML=msg;
 }
 loadSmoothers();
 busy=false;
}
window.__renderSummary=render;
})();
