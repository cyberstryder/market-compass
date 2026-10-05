const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,d=2)=>x===null||x===undefined?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
const money=x=>x===null||x===undefined?'—':(x<0?'-$':'$')+num(Math.abs(x),0);
const cls=x=>x===null||x===undefined?'':x>0?'good':x<0?'bad':'';
const pct=x=>x===null||x===undefined?'—':num(x,1)+'%';
const DOLLAR=['smoothers','futures','flow_pulse'];
let data=null,cal=null,calMonth=null,drillKey=null,daySel=null;

async function get(u){const r=await fetch(u);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}

function dayTotal(d){let pnl=0,setups=0;for(const k of DOLLAR){pnl+=(d[k]?d[k].pnl:0);}for(const k of['flash','0dte']){setups+=(d[k]?d[k].count:0);}return{pnl,setups};}

function metrics(st,dollar){
  return '<div class="metrics">'
  +'<div><strong>'+num(st.tracked,0)+'</strong><span>Tracked</span></div>'
  +'<div><strong>'+pct(st.win_rate)+'</strong><span>Win rate</span></div>'
  +(dollar?'<div><strong class="'+cls(st.pnl)+'">'+money(st.pnl)+'</strong><span>Paper P&L</span></div>'
          :'<div><strong>—</strong><span>Not scored in dollars</span></div>')+'</div>';
}

function card(k,t){
  const dollar=DOLLAR.includes(k);
  return '<div class="card" data-k="'+esc(k)+'"><h2>'+esc(t.title)+'</h2>'
  +'<p><strong>Watches:</strong> '+esc(t.watches)+'</p>'
  +'<p><strong>Fires:</strong> '+esc(t.fires)+'</p>'
  +'<p><strong>Scored:</strong> '+esc(t.scored)+'</p>'+metrics(t.stats,dollar)+'</div>';
}

function shiftMonth(m,d){const[y,mo]=m.split('-').map(Number);const dt=new Date(y,mo-1+d,1);return dt.getFullYear()+'-'+String(dt.getMonth()+1).padStart(2,'0');}
function monthLabel(m){const[y,mo]=m.split('-').map(Number);return new Date(y,mo-1,1).toLocaleString('en-US',{month:'long',year:'numeric'});}

function calendarHtml(){
  const days=cal.days||{};const[y,mo]=calMonth.split('-').map(Number);
  const pad=(new Date(y,mo-1,1).getDay()+6)%7, ndays=new Date(y,mo,0).getDate();
  const n=new Date(), today=n.getFullYear()+'-'+String(n.getMonth()+1).padStart(2,'0')+'-'+String(n.getDate()).padStart(2,'0');
  let h='<div class="controls"><button id="pv">‹ Prev</button><strong>'+esc(monthLabel(calMonth))+'</strong><button id="nx">Next ›</button><span class="fine">click a day for the split</span></div><div class="cal-grid">';
  ['M','T','W','T','F','S','S'].forEach(d=>h+='<div class="fine" style="text-align:center">'+d+'</div>');
  for(let i=0;i<pad;i++)h+='<div class="cal-day pad"></div>';
  for(let d=1;d<=ndays;d++){
    const ds=calMonth+'-'+String(d).padStart(2,'0'), t=dayTotal(days[ds]||{}), has=days[ds]&&(t.pnl!==0||t.setups>0);
    h+='<div class="cal-day'+(ds===today?' today':'')+(has?'':' dim')+(daySel===ds?' sel':'')+'" data-day="'+ds+'"><div class="fine">'+d+'</div>'
      +(has?'<div class="pnl '+cls(t.pnl)+'">'+money(t.pnl)+'</div>'+(t.setups?'<div class="fine">'+t.setups+' setups</div>':''):'')+'</div>';
  }
  h+='</div>';
  if(daySel&&days[daySel]){
    const d=days[daySel],t=dayTotal(d);
    h+='<div class="daydetail"><strong style="color:var(--text)">'+esc(daySel)+' — <span class="'+cls(t.pnl)+'">'+money(t.pnl)+'</span></strong>'
      +Object.entries(data.types).map(([k,ty])=>{const v=d[k];if(!v||(v.pnl===0&&v.count===0))return '';
        return '<div>'+esc(ty.title)+': '+(DOLLAR.includes(k)?'<span class="'+cls(v.pnl)+'">'+money(v.pnl)+'</span> · '+v.count+' closed':v.count+' logged')+'</div>';}).join('')+'</div>';
  }
  return h;
}

function snapshot(){
  const days=cal.days||{}, n=new Date();
  const iso=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  const R={'Today':[iso(n),iso(n)],'Last 7 days':[iso(new Date(n-6*864e5)),iso(n)],'This month':[calMonth+'-01',calMonth+'-31']};
  let h='<div class="stats">';
  for(const[l,[a,b]]of Object.entries(R)){
    let pnl=0,setups=0;
    for(const[ds,d]of Object.entries(days)){if(ds<a||ds>b)continue;const t=dayTotal(d);pnl+=t.pnl;setups+=t.setups;}
    h+='<div class="stat"><div class="label">'+esc(l)+'</div><div class="value '+cls(pnl)+'">'+money(pnl)+'</div><div class="fine">'+setups+' setups logged</div></div>';
  }
  return h+'</div>';
}

function render(){
  const order=['smoothers','futures','flow_pulse','flash','0dte'], types=data.types||{};
  let h='<div class="banner">Paper results only — no real money, no broker fills. Flash Agentic and 0DTE are scored by hit rate and counts, not dollars.</div>';
  h+=snapshot();
  h+='<div class="cards">'+order.filter(k=>types[k]).map(k=>card(k,types[k])).join('')+'</div>';
  if(drillKey&&types[drillKey]){
    const t=types[drillKey], dollar=DOLLAR.includes(drillKey);
    h+='<div class="panel"><div class="drill-head"><h2>'+esc(t.title)+' — breakdown</h2><button id="dclose">Close</button></div><div class="cards">'
      +(t.drill.length?t.drill.map(d=>'<div class="card" style="cursor:default"><h2>'+esc(d.name)+'</h2><p>'+esc(d.watches)+'</p>'+metrics(d.stats,dollar)+'</div>').join(''):'<div class="empty">Nothing tracked yet.</div>')+'</div></div>';
  }
  h+='<div class="panel"><h2>P&amp;L calendar</h2><div id="cal">'+calendarHtml()+'</div></div>';
  $('#body').innerHTML=h;
  document.querySelectorAll('[data-k]').forEach(c=>c.onclick=()=>{const k=c.getAttribute('data-k');drillKey=drillKey===k?null:k;render();});
  const dc=$('#dclose');if(dc)dc.onclick=()=>{drillKey=null;render();};
  document.querySelectorAll('[data-day]').forEach(c=>c.onclick=()=>{const d=c.getAttribute('data-day');daySel=daySel===d?null:d;render();});
  $('#pv').onclick=async()=>{calMonth=shiftMonth(calMonth,-1);cal=await get('/api/simple/calendar?month='+calMonth);render();};
  $('#nx').onclick=async()=>{calMonth=shiftMonth(calMonth,1);cal=await get('/api/simple/calendar?month='+calMonth);render();};
}

(async()=>{
  try{
    const n=new Date();calMonth=n.getFullYear()+'-'+String(n.getMonth()+1).padStart(2,'0');
    const[s,c]=await Promise.all([get('/api/simple'),get('/api/simple/calendar?month='+calMonth)]);
    data=s;cal=c;render();
  }catch(e){$('#body').innerHTML='<div class="empty">Could not load results ('+esc(e.message)+').</div>';}
})();
