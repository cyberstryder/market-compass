/* Simple view: the plain-English version of what each tracker does. Read-only. */
(()=>{
let busy=false, data=null, cal=null, calMonth=null, drillKey=null, daySel=null;

async function get(url){const r=await fetch(url);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}

const money=x=>x===null||x===undefined?'—':(x<0?'-$':'$')+num(Math.abs(x),0);
const pnlClass=x=>x===null||x===undefined?'':x>0?'good':x<0?'bad':'';
const pct=x=>x===null||x===undefined?'—':num(x,1)+'%';
const DOLLAR=['smoothers','futures','flow_pulse'];

function dayTotal(d){
  let pnl=0,count=0;
  for(const k of DOLLAR){pnl+=(d[k]?d[k].pnl:0);count+=(d[k]?d[k].count:0);}
  let setups=0;
  for(const k of ['flash','0dte']){setups+=(d[k]?d[k].count:0);}
  return {pnl,count,setups};
}

function statsHtml(st,dollar){
  const wr=pct(st.win_rate);
  const pnl=dollar?'<div><span class="fine">Paper P&amp;L</span><strong class="'+pnlClass(st.pnl)+'">'+money(st.pnl)+'</strong></div>'
    :'<div><span class="fine">Scoring</span><strong>not in dollars</strong></div>';
  return '<div class="research-card-metrics">'
    +'<div><span class="fine">Tracked</span><strong>'+num(st.tracked,0)+'</strong></div>'
    +'<div><span class="fine">Win rate</span><strong>'+wr+'</strong></div>'
    +pnl+'</div>';
}

function cardHtml(key,t){
  const st=t.stats,dollar=DOLLAR.includes(key);
  return '<article class="panel research-card" data-simple-card="'+esc(key)+'" style="cursor:pointer;margin-bottom:0">'
    +'<div class="panel-head"><div><div class="eyebrow fine">TRACKER</div><h2>'+esc(t.title)+'</h2></div>'
    +(drillKey===key?tag('open'):tag('details'))+'</div>'
    +'<p class="fine" style="font-size:13px;color:#c7d6d0"><strong>Watches:</strong> '+esc(t.watches)+'</p>'
    +'<p class="fine"><strong>Fires:</strong> '+esc(t.fires)+'</p>'
    +'<p class="fine"><strong>Scored:</strong> '+esc(t.scored)+'</p>'
    +statsHtml(st,dollar)+'</article>';
}

function drillHtml(t){
  const dollar=DOLLAR.includes(t.key);
  let h='<article class="panel"><div class="panel-head"><h2>'+esc(t.title)+' — breakdown</h2><button type="button" class="quiet" id="simple-drill-close">Close</button></div>';
  if(!t.drill.length)h+=empty('Nothing tracked yet','No completed items for this tracker.');
  h+='<div class="research-cards">'+t.drill.map(d=>{
    const st=d.stats;
    return '<article class="panel research-card" style="margin-bottom:0"><div class="panel-head"><h2>'+esc(d.name)+'</h2></div>'
      +'<p class="fine">'+esc(d.watches)+'</p>'+statsHtml(st,dollar)+'</article>';
  }).join('')+'</div></article>';
  return h;
}

function shiftMonth(m,delta){
  const[y,mo]=m.split('-').map(Number);
  const d=new Date(y,mo-1+delta,1);
  return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');
}
function monthLabel(m){
  const[y,mo]=m.split('-').map(Number);
  return new Date(y,mo-1,1).toLocaleString('en-US',{month:'long',year:'numeric'});
}

function calendarHtml(){
  const days=cal.days||{};
  const[y,mo]=calMonth.split('-').map(Number);
  const first=new Date(y,mo-1,1), startPad=(first.getDay()+6)%7;
  const ndays=new Date(y,mo,0).getDate();
  const today=new Date();
  const todayStr=today.getFullYear()+'-'+String(today.getMonth()+1).padStart(2,'0')+'-'+String(today.getDate()).padStart(2,'0');
  let h='<div class="research-controls"><button type="button" id="simple-cal-prev">‹ Prev</button>'
    +'<strong>'+esc(monthLabel(calMonth))+'</strong>'
    +'<button type="button" id="simple-cal-next">Next ›</button>'
    +'<p>Paper P&amp;L by day · click a day for the split</p></div>';
  h+='<div class="cal-grid">'+['M','T','W','T','F','S','S'].map(d=>'<div class="fine" style="text-align:center">'+d+'</div>').join('');
  for(let i=0;i<startPad;i++)h+='<div class="cal-day cal-pad"></div>';
  for(let d=1;d<=ndays;d++){
    const ds=calMonth+'-'+String(d).padStart(2,'0');
    const tot=dayTotal(days[ds]||{});
    const has=days[ds]&&(tot.pnl!==0||tot.count>0||tot.setups>0);
    h+='<div class="cal-day'+(ds===todayStr?' cal-today':'')+(has?'':' cal-empty')+(daySel===ds?' cal-sel':'')+'" data-simple-day="'+ds+'">'
      +'<div class="fine">'+d+'</div>'
      +(has?'<div class="cal-pnl '+pnlClass(tot.pnl)+'">'+money(tot.pnl)+'</div>'
        +(tot.setups?'<div class="fine">'+tot.setups+' setups</div>':''):'')
      +'</div>';
  }
  h+='</div>';
  if(daySel&&days[daySel]){
    const d=days[daySel],tot=dayTotal(d);
    h+='<article class="panel" style="margin-top:16px"><div class="panel-head"><h2>'+esc(daySel)+'</h2><span class="fine">'+money(tot.pnl)+'</span></div><div class="fine">'
      +Object.entries(data.types).map(([k,t])=>{
        const v=d[k];if(!v||(v.pnl===0&&v.count===0))return '';
        return '<div>'+esc(t.title)+': '+(DOLLAR.includes(k)?'<strong class="'+pnlClass(v.pnl)+'">'+money(v.pnl)+'</strong> · '+v.count+' closed':v.count+' logged')+'</div>';
      }).join('')+'</div></article>';
  }
  return h;
}

function snapshotHtml(){
  if(!cal)return '';
  const days=cal.days||{};
  const now=new Date();
  const iso=d=>d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
  const ranges={
    'Today':[iso(now),iso(now)],
    'Last 7 days':[iso(new Date(now-6*864e5)),iso(now)],
    'This month':[calMonth+'-01',calMonth+'-31'],
  };
  let h='<div class="stats">';
  for(const[label,[a,b]]of Object.entries(ranges)){
    let pnl=0,setups=0;
    for(const[ds,d]of Object.entries(days)){
      if(ds<a||ds>b)continue;
      const t=dayTotal(d);pnl+=t.pnl;setups+=t.setups;
    }
    h+='<div class="stat"><div class="label">'+esc(label)+'</div>'
      +'<div class="value '+pnlClass(pnl)+'">'+money(pnl)+'</div>'
      +'<div class="fine">'+setups+' setups logged</div></div>';
  }
  h+='</div>';
  return h;
}

function render(){
  const el=$('#simple-body');
  const order=['smoothers','futures','flow_pulse','flash','0dte'];
  const types=data.types||{};
  let h='<div class="banner">The plain-English version. Paper numbers, not broker fills. Flash Agentic and 0DTE are scored by hit rate and counts — no dollars.</div>';
  h+=snapshotHtml();
  h+='<div class="research-cards" style="margin-bottom:22px">'+order.filter(k=>types[k]).map(k=>cardHtml(k,types[k])).join('')+'</div>';
  if(drillKey&&types[drillKey])h+=drillHtml(types[drillKey]);
  h+='<article class="panel"><div class="panel-head"><h2>P&amp;L calendar</h2></div><div id="simple-cal">'+calendarHtml()+'</div></article>';
  el.innerHTML=h;
  el.querySelectorAll('[data-simple-card]').forEach(c=>c.onclick=()=>{
    const k=c.getAttribute('data-simple-card');
    drillKey=drillKey===k?null:k;render();
  });
  const dc=$('#simple-drill-close');if(dc)dc.onclick=()=>{drillKey=null;render();};
  el.querySelectorAll('[data-simple-day]').forEach(c=>c.onclick=()=>{
    const ds=c.getAttribute('data-simple-day');
    daySel=daySel===ds?null:ds;render();
  });
  const pv=$('#simple-cal-prev'),nx=$('#simple-cal-next');
  if(pv)pv.onclick=async()=>{calMonth=shiftMonth(calMonth,-1);await loadCal();};
  if(nx)nx.onclick=async()=>{calMonth=shiftMonth(calMonth,1);await loadCal();};
}

async function loadCal(){
  cal=await get('/api/simple/calendar?month='+encodeURIComponent(calMonth));
  render();
}

window.__renderSimple=async()=>{
  if(busy)return;busy=true;
  try{
    const now=new Date();
    if(!calMonth)calMonth=now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0');
    const[s,c]=await Promise.all([get('/api/simple'),get('/api/simple/calendar?month='+encodeURIComponent(calMonth))]);
    data=s;cal=c;render();
  }catch(e){
    $('#simple-body').innerHTML=empty('Could not load the simple view','The dashboard API did not respond ('+esc(e.message)+').');
  }finally{busy=false;}
};
})();
