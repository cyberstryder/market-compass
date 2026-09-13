const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,d=2)=>x===null||x===undefined?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
const when=x=>x?new Date(x*1000).toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})+' CT':'No observation';
const empty=(title,sub)=>'<div class="empty"><strong>'+esc(title)+'</strong>'+esc(sub)+'</div>';
const tag=(s)=>'<span class="tag '+(['receiving','available','running','connected','delivered','entered'].includes(s)?'good':['stale','error','not_configured','blocked'].includes(s)?'bad':'')+'">'+esc(s)+'</span>';
function table(head,rows){return '<table><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
const titles={overview:'Session overview',exposure:'Exposure context',flow:'Options flow',trades:'Simulated trades',assistant:'Ask Compass',health:'Feed health'};
document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav,.tab').forEach(n=>n.classList.remove('active'));b.classList.add('active');$('#'+b.dataset.tab).classList.add('active');$('#title').textContent=titles[b.dataset.tab];});
$('#logout').onclick=async()=>{await fetch('/logout',{method:'POST'});location.href='/login';};
let lastState=null,first=true;
function render(d){
 lastState=d;
 $('#market').textContent=d.markets.equities?'EQUITY SESSION OPEN':d.markets.futures?'FUTURES SESSION OPEN':'MARKETS CLOSED';
 $('#clock').textContent=when(d.asof);
 $('#updated').textContent='Refreshed '+when(d.asof);
 const missing=d.health.filter(h=>['not_configured','error','stale','waiting'].includes(h.status));
 const populated=Object.keys(d.quotes).length;
 $('#connection').textContent=missing.length?missing.length+' components need attention. Open Feed health for configuration and freshness details.':populated?'Observations are arriving. Source timestamps below determine whether each observation is current.':'Infrastructure is running. Waiting for configured feeds and market observations.';
 const open=d.trades.filter(p=>p.status==='open').length;
 const closed=d.trades.filter(p=>p.status==='closed');
 const stats=[['Observed symbols',populated,'Configured watchlist + resolved futures'],['Open simulations',open,'Portfolio limit: '+d.limits.max_positions],['Closed simulations',closed.length,'Latest 100 records'],['Realized simulation P&L',num(closed.reduce((a,p)=>a+(p.pnl||0),0)),'USD · includes configured costs']];
 $('#stats').innerHTML=stats.map(x=>'<div class="stat"><div class="label">'+esc(x[0])+'</div><div class="value">'+esc(x[1])+'</div><div class="fine">'+esc(x[2])+'</div></div>').join('');
 $('#watch').innerHTML=populated?table(['Symbol','Bid / Ask','Age','OR high / low','Prior high / low'],Object.entries(d.quotes).map(([s,q])=>{const l=d.levels[s]||{};return [esc(s),num(q.bid)+' / '+num(q.ask),esc(num(q.age,1)+'s'),l.or_complete?num(l.or_high)+' / '+num(l.or_low):'Warming up',l.prior_complete?num(l.prior_high)+' / '+num(l.prior_low):'Incomplete history'];})):empty('Waiting for first observations','Add data-provider keys in Railway. This view contains no sample prices.');
 $('#journal').innerHTML=d.alerts.length?d.alerts.slice(0,7).map(a=>'<div class="journal-item"><strong>'+esc(a.symbol)+'</strong> '+tag(a.payload.status)+'<p>'+esc(a.payload.reason||a.payload.exit_reason||a.payload.strategy)+'</p><span class="time">#'+a.id+' · '+when(a.ts)+'</span></div>').join(''):empty('No decisions recorded yet','Signals, entries, exits and data-related skips will appear here.');
 const ex=Object.entries(d.exposure);
 $('#exposures').innerHTML=ex.length?'<div class="exposure-grid">'+ex.map(([s,e])=>'<article class="panel"><div class="panel-head"><h2>'+esc(s)+'</h2>'+tag(e.status)+'</div><p>GEX '+num(e.gex)+' · VEX '+num(e.vex)+'</p><p class="fine">'+esc(e.source)+' · '+num((e.coverage||0)*100,1)+'% usable · '+e.usable_gex+' / '+e.contracts+' contracts · '+when(e.asof)+'</p><p class="fine">'+esc(e.sign_model)+'</p><details><summary>Strike exposure & methodology</summary><p class="fine">'+esc(e.gex_units)+'; '+esc(e.vex_units)+'. OI dates: '+esc((e.oi_dates||[]).join(', ')||'provider date unavailable')+'</p>'+table(['Strike','GEX','VEX'],(e.strikes||[]).slice(0,200).map(r=>[num(r.strike),num(r.gex),num(r.vex)]))+'</details></article>').join('')+'</div>':empty('Exposure is waiting for input','A fresh underlying price, chain, Greeks and open interest are required.');
 $('#matrix').innerHTML=Object.keys(d.matrix).length?Object.entries(d.matrix).map(([k,v])=>'<details><summary>'+esc(k.replace('matrix:',''))+' · '+when(v.received)+'</summary><p>'+esc(v.normalization)+'</p><pre>'+esc(JSON.stringify(v.data,null,2))+'</pre></details>').join(''):empty('TraderMatrix is not connected','Set TRADERMATRIX_API_KEY to capture its analytics and unusual-activity responses.');
 $('#flows').innerHTML=d.flow.length?table(['Time','Contract','Premium','Size','Classification'],d.flow.map(r=>[when(r.ts),esc(r.symbol),'$'+num(r.payload.premium),num(r.payload.size),esc(r.payload.classification)])):empty('No qualifying prints recorded','Continuous selected-contract trades are recorded when Massive is connected.');
 $('#ledger').innerHTML=d.trades.length?table(['Entry time','Symbol','Strategy','State','Qty','Entry','Stop','Target','Exit','P&L'],d.trades.map(p=>[when(p.entered_at),esc(p.symbol),esc(p.strategy),tag(p.status),num(p.qty,0),num(p.entry),num(p.stop),num(p.target),num(p.exit),num(p.pnl??p.unrealized)])):empty('No simulated trades yet','The engine requires a complete setup, a fresh executable quote, and available risk capacity.');
 $('#health-table').innerHTML=d.health.length?table(['Component','Status','Source age','Heartbeat age','Detail'],d.health.map(h=>[esc(h.name),tag(h.status),h.age===null?'No event':esc(num(h.age,1)+'s'),esc(num(h.heartbeat_age,1)+'s'),esc(h.detail)])):empty('Waiting for workers','Collector and engine heartbeats will appear here.');
 $('#workers').textContent=Object.entries(d.workers).map(([k,v])=>k.replace('worker:','')+': '+when(v.at)).join(' · ');
 if(first&&!d.ai_configured){$('#answer').textContent='Add OPENAI_API_KEY in Railway to enable grounded answers. The live dashboard and scanners work independently.';}
 first=false;
}
async function poll(){
 try{const r=await fetch('/api/state');if(r.status===401){location.href='/login';return;}if(!r.ok)throw new Error('Unable to read shared state');render(await r.json());}
 catch(e){$('#connection').textContent='Connection interrupted. Displayed observations are not being refreshed. '+e.message;}
 finally{setTimeout(poll,3000);}
}
$('#ask').addEventListener('submit',async e=>{e.preventDefault();const button=$('#ask button');button.disabled=true;$('#answer').textContent='Reading the recorded market context…';
 try{const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:$('#question').value})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Unable to answer');$('#answer').textContent=d.answer;$('#answer-time').textContent='Context captured '+when(d.asof);}
 catch(e){$('#answer').textContent=e.message;}finally{button.disabled=false;}});
poll();
