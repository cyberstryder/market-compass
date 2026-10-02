/* P/L calendar: realized paper P&L per day per strategy bucket. Read-only. */
(()=>{
let busy=false, curMonth=null;

async function get(url){const r=await fetch(url);if(!r.ok)throw new Error('HTTP '+r.status);return r.json();}

const money=x=>x===null||x===undefined?'—':(x<0?'-$':'$')+num(Math.abs(x),0);
const pnlClass=x=>x>0?'good':x<0?'bad':'';

function shiftMonth(m,delta){
 const[y,mo]=m.split('-').map(Number);
 const d=new Date(y,mo-1+delta,1);
 return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');
}

function monthLabel(m){
 const[y,mo]=m.split('-').map(Number);
 return new Date(y,mo-1,1).toLocaleString('en-US',{month:'long',year:'numeric'});
}

function render(cal){
 curMonth=cal.month;
 const buckets=cal.buckets||[];
 const days=cal.days||{};
 const mt=cal.month_total||{},{by_bucket={},total={pnl:0,trades:0,wins:0}}=mt;

 let h='<div class="research-controls"><button type="button" id="cal-prev">‹ Prev</button>'
  +'<strong>'+esc(monthLabel(cal.month))+'</strong>'
  +'<button type="button" id="cal-next">Next ›</button>'
  +'<span class="fine">Realized P&L only · since '+esc(cal.cutoff_day||'—')+'</span></div>';

 const t=total;
 h+='<div class="stats">'
  +'<div class="stat"><div class="label">Month P&L</div><div class="value '+pnlClass(t.pnl)+'">'+money(t.pnl)+'</div></div>'
  +'<div class="stat"><div class="label">Trades</div><div class="value">'+num(t.trades,0)+'</div></div>'
  +'<div class="stat"><div class="label">Wins</div><div class="value">'+num(t.wins,0)+'</div></div>'
  +buckets.map(b=>{const s=(by_bucket[b.id]||{pnl:0,trades:0});
    return '<div class="stat"><div class="label">'+esc(b.label)+'</div><div class="value '+pnlClass(s.pnl)+'">'+money(s.pnl)+'</div></div>';}).join('')
  +'</div>';

 const[y,mo]=cal.month.split('-').map(Number);
 const first=new Date(y,mo-1,1), startDow=first.getDay(), dim=new Date(y,mo,0).getDate();
 h+='<div class="cal-grid">'
  +['Sun','Mon','Tue','Wed','Thu','Fri','Sat'].map(d=>'<div class="cal-dow">'+d+'</div>').join('');
 for(let i=0;i<startDow;i++)h+='<div class="cal-day cal-pad"></div>';
 const today=new Date().toLocaleDateString('en-CA',{timeZone:'America/Chicago'});
 for(let dnum=1;dnum<=dim;dnum++){
  const day=cal.month+'-'+String(dnum).padStart(2,'0');
  const info=days[day], tot=info&&info.total;
  const cls='cal-day'+(day===today?' cal-today':'')+(tot?'':' cal-empty');
  h+='<div class="'+cls+'"><div class="cal-num">'+dnum+'</div>';
  if(tot){
   h+='<div class="cal-pnl '+pnlClass(tot.pnl)+'">'+money(tot.pnl)+'</div>';
   h+='<div class="cal-break">'+buckets.map(b=>{
     const s=(info.buckets||{})[b.id];
     return s&&s.trades?'<div><span>'+esc(b.label)+'</span> <span class="'+pnlClass(s.pnl)+'">'+money(s.pnl)+'</span></div>':'';
    }).join('')+'</div>';
  }
  h+='</div>';
 }
 h+='</div>';
 $('#calendar-body').innerHTML=h;
 $('#cal-prev').onclick=()=>load(shiftMonth(curMonth,-1));
 $('#cal-next').onclick=()=>load(shiftMonth(curMonth,1));
}

async function load(month){
 if(busy)return;busy=true;
 try{
  render(await get('/api/calendar'+(month?'?month='+encodeURIComponent(month):'')));
 }catch(e){
  $('#calendar-body').innerHTML=empty('Calendar unavailable','Could not load /api/calendar.');
 }finally{busy=false;}
}
window.__renderCalendar=()=>load(curMonth);
})();
