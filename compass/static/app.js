const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,d=2)=>x===null||x===undefined?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
const when=x=>x?new Date(x*1000).toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})+' CT':'No observation';
const age=x=>x===null||x===undefined?'—':x<120?num(x,1)+'s':x<7200?num(x/60,1)+'m':num(x/3600,1)+'h';
const compact=x=>x===null||x===undefined?'—':Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:2}).format(x);
const empty=(title,sub)=>'<div class="empty"><strong>'+esc(title)+'</strong>'+esc(sub)+'</div>';
const tag=(s)=>'<span class="tag '+(['ready','current','receiving','available','running','connected','delivered','entered'].includes(s)?'good':['stale','error','clock_error','not_configured','blocked','missing','partial'].includes(s)?'bad':'')+'">'+esc(String(s||'pending').replaceAll('_',' '))+'</span>';
function table(head,rows){return '<table><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
const titles={overview:'Session overview',exposure:'Exposure context',flow:'Options flow',trades:'Simulated trades',assistant:'Ask Compass',health:'Feed health'};
document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav,.tab').forEach(n=>n.classList.remove('active'));b.classList.add('active');$('#'+b.dataset.tab).classList.add('active');$('#title').textContent=titles[b.dataset.tab];});
$('#logout').onclick=async()=>{await fetch('/logout',{method:'POST'});location.href='/login';};
let lastState=null,first=true,testEvent=null;
function vendorMatrix(items){
 const matrices=Object.entries(items).filter(([key,item])=>key!=='matrix:unusual_activity'&&Array.isArray(item.strikes));
 if(!matrices.length)return empty('Waiting for vendor matrices','The source snapshot time and returned strike concentrations will appear here.');
 return '<div class="exposure-grid">'+matrices.map(([key,e])=>{
  const ranked=[...e.strikes].filter(r=>r.gex!==null).sort((a,b)=>Math.abs(b.gex)-Math.abs(a.gex)).slice(0,12);
  return '<article class="panel"><div class="panel-head"><h2>'+esc(e.symbol)+'</h2>'+tag(e.freshness)+'</div>'+
   '<div class="metric-pair"><div><span class="fine">GEX · vendor units</span><strong>'+compact(e.gex)+'</strong></div><div><span class="fine">VEX · vendor units</span><strong>'+compact(e.vex)+'</strong></div></div>'+
   '<p class="fine">Source '+when(e.source_ts)+'<br>Fetched '+when(e.received)+(e.cached?' · vendor cache':'')+' · snapshot spot '+num(e.spot)+'</p>'+
   '<p class="fine">'+num(e.strikes.length,0)+' strikes · '+num(e.expirations.length,0)+' expirations · '+num(e.populated_cells.gex,0)+' / '+num(e.cells,0)+' GEX cells populated. Empty cells are not zero.</p>'+
   '<h3>Largest absolute GEX concentrations</h3>'+table(['Strike','GEX','VEX'],ranked.map(r=>[num(r.strike),compact(r.gex),compact(r.vex)]))+
   '<details data-key="'+esc(key)+'"><summary>Expiration totals & method</summary>'+table(['Expiration','GEX','VEX'],e.expirations.map(r=>[esc(r.expiry),compact(r.gex),compact(r.vex)]))+'<p class="fine">'+esc(e.methodology)+'</p></details></article>';
 }).join('')+'</div>';
}
function vendorFlow(e){
 if(!e||!Array.isArray(e.rows))return empty('Waiting for the vendor feed','TraderMatrix unusual activity will appear after a successful poll.');
 const metrics=[['Vendor filtered rows',num(e.total,0)],['Daily unique rows stored',num(e.unique_rows,0)],['Vendor total premium','$'+compact(e.aggregates.totalPremium)],['Bullish / bearish premium','$'+compact(e.aggregates.bullishPremium)+' / $'+compact(e.aggregates.bearishPremium)]];
 const headline='<div class="stats flow-stats">'+metrics.map(([label,value])=>'<div class="stat"><div class="label">'+esc(label)+'</div><div class="value">'+esc(value)+'</div></div>').join('')+'</div>';
 const context='<p>'+tag(e.status)+' '+(e.limited?'Recovery is continuing. ':'')+esc(e.rejected?e.rejected+' unparsed or redacted rows. ':'')+'</p><p class="fine">Fetched '+when(e.received)+' · latest returned trade '+when(e.source_ts)+' · '+num(e.pages,0)+' / '+num(e.page_cap,0)+' allowed pages</p><p class="fine">'+esc(e.coverage_note)+'</p>';
 const recovery=e.recovery||{};
 const progress='<p class="fine">Estimated missing IDs '+num(recovery.estimated_gap,0)+' · resume page '+num(recovery.next_page,0)+' / '+num(recovery.page_ceiling,0)+' · last end-of-feed pass '+when(recovery.last_full_pass_at)+'</p>'+((e.prior_gaps||[]).length?'<p class="fine">Prior-day gaps: '+esc(e.prior_gaps.map(g=>g.day+': '+g.estimated_gap+' estimated missing').join(' · '))+'</p>':'');
 const rows=e.rows.slice(0,100);
 return headline+context+progress+(rows.length?table(['Trade time','Ticker / contract','Premium','Vendor type','Sentiment','Score'],rows.map(r=>[
  when(r.source_ts),esc(r.symbol)+' '+esc(r.option_type)+' '+num(r.strike)+'<br><span class="fine">'+esc(r.expiry)+'</span>','$'+num(r.premium),esc(r.classification||'Unclassified'),esc(r.sentiment||'Unknown'),num(r.score,0)])):
  empty('No vendor activity returned for today','This is an empty filtered response; it is not proof of a failed connection.'))+'<p class="fine">Latest '+rows.length+' rows shown. '+esc(e.schema_validation)+'</p>';
}
function render(d){
 const openDetails=new Set([...document.querySelectorAll('details[open][data-key]')].map(e=>e.dataset.key));
 lastState=d;
 $('#market').textContent=d.markets.equities?'EQUITY SESSION OPEN':d.markets.futures?'FUTURES SESSION OPEN':'MARKETS CLOSED';
 $('#clock').textContent=when(d.asof);
 $('#updated').textContent='Refreshed '+when(d.asof);
 const missing=d.health.filter(h=>['not_configured','error','stale','waiting','partial','blocked'].includes(h.status));
 const populated=Object.keys(d.quotes).length;
 $('#connection').textContent=missing.length?missing.length+' components need attention. Open Feed health for configuration and freshness details.':!d.markets.equities&&!d.markets.futures?'Markets are closed. Feed health distinguishes worker activity from the age of the last market observation.':populated?'Source timestamps and quote checks determine which observations can be used.':'Infrastructure is running. Waiting for market observations.';
 const open=d.trades.filter(p=>p.status==='open').length;
 const closed=d.trades.filter(p=>p.status==='closed');
 const stats=[['Observed symbols',populated,'Configured watchlist + resolved futures'],['Open simulations',open,'Portfolio limit: '+d.limits.max_positions],['Closed simulations',closed.length,'Latest 100 records'],['Realized simulation P&L',num(closed.reduce((a,p)=>a+(p.pnl||0),0)),'USD · includes configured costs']];
 $('#stats').innerHTML=stats.map(x=>'<div class="stat"><div class="label">'+esc(x[0])+'</div><div class="value">'+esc(x[1])+'</div><div class="fine">'+esc(x[2])+'</div></div>').join('');
 $('#watch').innerHTML=populated?table(['Symbol','Bid / Ask','Age','OR high / low','Prior high / low'],Object.entries(d.quotes).map(([s,q])=>{const l=d.levels[s]||{};return [esc(s),num(q.bid)+' / '+num(q.ask),esc(num(q.age,1)+'s'),l.or_complete?num(l.or_high)+' / '+num(l.or_low):'Warming up',l.prior_complete?num(l.prior_high)+' / '+num(l.prior_low):'Incomplete history'];})):empty('Waiting for first observations',d.markets.futures||d.markets.equities?'Check Feed health for active contracts and source readiness.':'The next open session will provide live quotes. Feed health shows connection and history checks.');
 $('#journal').innerHTML=d.alerts.length?d.alerts.slice(0,7).map(a=>'<div class="journal-item"><strong>'+esc(a.symbol)+'</strong> '+tag(a.payload.status)+'<p>'+esc(a.payload.reason||a.payload.exit_reason||a.payload.strategy)+'</p><span class="time">#'+a.id+' · '+when(a.ts)+'</span></div>').join(''):empty('No decisions recorded yet','Signals, entries, exits and data-related skips will appear here.');
 const ex=Object.entries(d.exposure);
 $('#exposures').innerHTML=ex.length?'<div class="exposure-grid">'+ex.map(([s,e])=>'<article class="panel"><div class="panel-head"><h2>'+esc(s)+'</h2>'+tag(e.status)+'</div><p>GEX '+compact(e.gex)+' · local vanna proxy '+compact(e.vex)+'</p><p class="fine">'+esc(e.source)+' · '+num((e.coverage??0)*100,1)+'% GEX input coverage · '+num(e.usable_gex,0)+' / '+num(e.contracts,0)+' contracts</p><p class="fine">Missing/invalid OI '+num(e.missing_oi,0)+' · gamma '+num(e.missing_gamma,0)+' · contract metadata '+num(e.invalid_contract,0)+' (counts may overlap)</p><p class="fine">Chain checked '+when(e.asof)+' · underlying source '+when(e.spot_asof)+'</p><p class="fine">'+esc(e.reason)+'</p><details data-key="local-'+esc(s)+'"><summary>Strike exposure & methodology</summary><p class="fine">'+esc(e.sign_model)+'. '+esc(e.gex_units)+'; '+esc(e.vex_units)+'. Vanna input coverage '+num((e.vex_coverage??0)*100,1)+'%. OI dates: '+esc((e.oi_dates||[]).join(', ')||'provider date unavailable')+'</p>'+table(['Strike','GEX','Vanna proxy'],(e.strikes||[]).slice(0,200).map(r=>[num(r.strike),compact(r.gex),compact(r.vex)]))+'</details></article>').join('')+'</div>':empty('Exposure is waiting for input','A fresh underlying price, chain, Greeks and open interest are required.');
 $('#matrix').innerHTML=vendorMatrix(d.matrix);
 $('#vendor-flows').innerHTML=vendorFlow(d.matrix['matrix:unusual_activity']);
 $('#flows').innerHTML=d.flow.length?table(['Time','Contract','Premium','Size','Classification'],d.flow.map(r=>[when(r.ts),esc(r.symbol),'$'+num(r.payload.premium),num(r.payload.size),esc(r.payload.classification)])):empty('No qualifying prints recorded','Continuous selected-contract trades are recorded when Massive is connected.');
 $('#ledger').innerHTML=d.trades.length?table(['Entry time','Symbol','Strategy','State','Qty','Entry','Stop','Target','Exit','P&L'],d.trades.map(p=>[when(p.entered_at),esc(p.symbol),esc(p.strategy),tag(p.status),num(p.qty,0),num(p.entry),num(p.stop),num(p.target),num(p.exit),num(p.pnl??p.unrealized)])):empty('No simulated trades yet','The engine requires a complete setup, a fresh executable quote, and available risk capacity.');
 $('#quote-checks').innerHTML=table(['Configured symbol','Resolved symbol','Status','Quote age','Source time'],d.quote_checks.map(q=>[esc(q.configured_symbol),esc(q.symbol),tag(q.status),age(q.age),when(q.source_ts)]));
 $('#health-table').innerHTML=d.health.length?table(['Component','Status','Source age','Last task update','Worker heartbeat','Detail'],d.health.map(h=>[esc(h.name),tag(h.status),age(h.age),age(h.check_age),age(h.heartbeat_age),'<span class="health-detail">'+esc(h.detail)+(h.stream_contracts!==undefined?' · '+num(h.stream_contracts,0)+' streamed / '+num(h.contracts,0)+' chain contracts':'')+'</span>'])):empty('Waiting for workers','Collector and engine heartbeats will appear here.');
 const delivery=d.delivery,confirmation=delivery.last_confirmation;
 $('#delivery').innerHTML='<p><strong>'+num(delivery.pending,0)+'</strong> alerts queued'+(delivery.pending?' · oldest '+age(delivery.oldest_age):'')+'</p>'+(confirmation?'<p class="fine">Last confirmed event #'+num(confirmation.event_id,0)+' · saved '+when(confirmation.at)+' · Discord message '+esc(confirmation.message_id)+'</p>':'<p class="muted">No saved-message confirmation yet. Send a clearly labeled test to verify delivery.</p>')+'<p class="fine">An alert stays queued until Discord confirms a saved message. Retries can duplicate an event; its event ID stays the same.</p>';
 const f=d.futures,fh=f.session;
 $('#future-readiness').innerHTML='<p>Trading day '+esc(fh.day)+' · '+tag(fh.is_open?'open':'closed')+' · risk resets at 17:00 CT</p><p class="fine">Session '+when(fh.open)+' → '+when(fh.close)+'<br>Last new entry '+when(fh.entry_end)+' · flatten '+when(fh.flatten_at)+'</p>'+table(['Root','Data contract','TradingView contract','Resolved ID'],f.selected.map(t=>{const r=f.contracts.find(r=>r.raw_symbol===t.raw_symbol);return [esc(t.root),esc(t.raw_symbol),esc(t.chart_symbol),r?esc(r.instrument_id):'Awaiting mapping'];}))+'<p class="fine">CME customary quarterly roll. Use the explicit chart contract above to compare unadjusted levels; continuous chart settings may differ.</p>'+f.history.map(h=>'<p>'+tag(h.status)+' '+esc(h.detail)+(h.estimated_usd!==undefined?' · reserved download cost $'+num(h.estimated_usd,6):'')+'</p>').join('');
 $('#greek-diagnostics').innerHTML=Object.entries(d.greek_diagnostics||{}).map(([symbol,g])=>'<article class="panel"><div class="panel-head"><h3>'+esc(symbol)+'</h3><span>'+num(g.target_near_atm.gamma_usable,0)+' / '+num(g.target_near_atm.contracts,0)+' near-money '+esc(g.target_expiry)+' gamma inputs</span></div><p>'+esc(g.finding)+'</p><p class="fine">Classification reference '+num(g.reference)+' from '+when(g.reference_ts)+' · pagination '+(g.complete_pagination?'complete':'incomplete')+'</p><details data-key="greeks-'+esc(symbol)+'"><summary>By expiry, side and moneyness · missing-field samples</summary>'+table(['Expiry','Side','Moneyness','Usable gamma / contracts','Absent / null / invalid'],g.groups.map(r=>[esc(r.expiry),esc(r.type),esc(r.moneyness),num(r.gamma_usable,0)+' / '+num(r.contracts,0),num(r.gamma_absent,0)+' / '+num(r.gamma_null,0)+' / '+num(r.gamma_invalid,0)]))+table(['Missing contract','Gamma field','Returned Greek fields','OI','IV','Quote source'],g.missing_samples.map(r=>[esc(r.symbol),esc(r.gamma_field),(r.greek_fields||[]).map(esc).join(', ')||'None',num(r.oi,0),num(r.iv),when(r.quote_ts)]))+'<p class="fine">'+esc(g.note)+'</p></details></article>').join('')||empty('Waiting for a chain refresh','Diagnostics retain the provider’s raw Greek-field presence.');
 if(testEvent&&delivery.last_test_confirmation?.event_id===testEvent){$('#test-result').textContent='Test event #'+testEvent+' confirmed. Discord message '+delivery.last_test_confirmation.message_id;testEvent=null;}
 $('#workers').textContent=Object.entries(d.workers).map(([k,v])=>k.replace('worker:','')+': '+when(v.at)).join(' · ');
 if(first&&!d.ai_configured){$('#answer').textContent='Add OPENAI_API_KEY in Railway to enable grounded answers. The live dashboard and scanners work independently.';}
 first=false;
 document.querySelectorAll('details[data-key]').forEach(e=>{e.open=openDetails.has(e.dataset.key);});
}
async function poll(){
 try{const r=await fetch('/api/state');if(r.status===401){location.href='/login';return;}if(!r.ok)throw new Error('Unable to read shared state');render(await r.json());}
 catch(e){$('#connection').textContent='Connection interrupted. Displayed observations are not being refreshed. '+e.message;}
 finally{setTimeout(poll,3000);}
}
$('#ask').addEventListener('submit',async e=>{e.preventDefault();const button=$('#ask button');button.disabled=true;$('#answer').textContent='Reading the recorded market context…';
 try{const r=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:$('#question').value})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Unable to answer');$('#answer').textContent=d.answer;$('#answer-time').textContent='Context captured '+when(d.asof);}
 catch(e){$('#answer').textContent=e.message;}finally{button.disabled=false;}});
$('#test-alert').onclick=async()=>{const button=$('#test-alert');button.disabled=true;$('#test-result').textContent='Queuing a clearly labeled test…';
 try{const r=await fetch('/api/alerts/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:crypto.randomUUID()})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Test request failed');testEvent=d.event_id;$('#test-result').textContent='Test event #'+testEvent+' queued. Waiting for Discord’s saved-message confirmation.';}
 catch(e){$('#test-result').textContent=e.message;}finally{button.disabled=false;}};
poll();
