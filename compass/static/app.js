const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,d=2)=>x===null||x===undefined?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
const when=x=>x?new Date(x*1000).toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})+' CT':'No observation';
const age=x=>x===null||x===undefined?'—':x<120?num(x,1)+'s':x<7200?num(x/60,1)+'m':num(x/3600,1)+'h';
const compact=x=>x===null||x===undefined?'—':Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:2}).format(x);
const empty=(title,sub)=>'<div class="empty"><strong>'+esc(title)+'</strong>'+esc(sub)+'</div>';
const tag=(s)=>'<span class="tag '+(['ready','current','receiving','available','running','connected','delivered','entered','triggered','setup_triggered'].includes(s)?'good':['stale','error','clock_error','not_configured','blocked','missing','partial','source_time_unknown','invalidated'].includes(s)?'bad':'')+'">'+esc(String(s||'pending').replaceAll('_',' '))+'</span>';
function table(head,rows){return '<table><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
const titles={secondary:'Secondary review',projects:'Connected projects',scanner:'Live scanner',research:'Research desk',overview:'Session overview',exposure:'Exposure context',flow:'Options flow',trades:'Simulated trades',assistant:'Ask Compass',health:'Feed health'};
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
 $('#journal').innerHTML=d.alerts.length?d.alerts.slice(0,7).map(a=>{const p=a.presentation||{};return '<div class="journal-item"><strong>'+esc(p.title||a.symbol)+'</strong><p>'+esc(p.mode||'SIMULATED')+' · '+esc(p.horizon||'')+'</p><p>'+esc(a.payload.exit_reason||a.payload.reason||p.setup||a.payload.strategy)+'</p><span class="time">'+esc(p.origin||'Compass')+' · #'+a.id+' · '+when(a.ts)+'</span></div>';}).join(''):empty('No decisions recorded yet','Signals, entries, exits and data-related skips will appear here.');
 const ex=Object.entries(d.exposure).slice(0,6);
 $('#exposures').innerHTML=ex.length?'<div class="exposure-grid">'+ex.map(([s,e])=>'<article class="panel"><div class="panel-head"><h2>'+esc(s)+'</h2>'+tag(e.status)+'</div><p>GEX '+compact(e.gex)+' · local vanna proxy '+compact(e.vex)+'</p><p class="fine">'+esc(e.source)+' · '+num((e.coverage??0)*100,1)+'% GEX input coverage · '+num(e.usable_gex,0)+' / '+num(e.contracts,0)+' contracts</p><p class="fine">Missing/invalid OI '+num(e.missing_oi,0)+' · gamma '+num(e.missing_gamma,0)+' · contract metadata '+num(e.invalid_contract,0)+' (counts may overlap)</p><p class="fine">Chain checked '+when(e.asof)+' · underlying source '+when(e.spot_asof)+'</p><p class="fine">'+esc(e.reason)+'</p><details data-key="local-'+esc(s)+'"><summary>Strike exposure & methodology</summary><p class="fine">'+esc(e.sign_model)+'. '+esc(e.gex_units)+'; '+esc(e.vex_units)+'. Vanna input coverage '+num((e.vex_coverage??0)*100,1)+'%. OI dates: '+esc((e.oi_dates||[]).join(', ')||'provider date unavailable')+'</p>'+table(['Strike','GEX','Vanna proxy'],(e.strikes||[]).slice(0,200).map(r=>[num(r.strike),compact(r.gex),compact(r.vex)]))+'</details></article>').join('')+'</div>':empty('Exposure is waiting for input','A fresh underlying price, chain, Greeks and open interest are required.');
 $('#matrix').innerHTML=vendorMatrix(Object.fromEntries(Object.entries(d.matrix).slice(0,6)));
 renderScanner(d.scanner);
 renderProjects(d.projects);
 renderSecondary(d.secondary);
 renderStrikeMap(d.matrix);
 updateResearchChoices(d.scanner.feeds);
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
function renderScanner(scanner){
 if(!scanner)return;
 const status=scanner.status;
 const rows=scanner.opportunities.filter(o=>$('#scanner-filter').value==='all'||['triggered','watch','blocked'].includes(o.status));
 const active=scanner.opportunities.filter(o=>o.status==='triggered').length;
 const current=scanner.feeds.filter(f=>f.status==='current').length;
 const metrics=[['Watchlist',status.watch_symbols,'Continuous price universe'],['Ready price contexts',status.features_ready??0,'Completed bars and history'],['Triggered setups',active,'Simulated entry conditions'],['Dated research sources',current+' / '+scanner.feeds.length,'Open details for actual source age']];
 $('#scanner-stats').innerHTML=metrics.map(([label,value,note])=>'<div class="stat"><div class="label">'+esc(label)+'</div><div class="value">'+esc(value)+'</div><div class="fine">'+esc(note)+'</div></div>').join('');
 $('#opportunities').innerHTML=rows.length?rows.map(o=>'<article class="setup-card"><div class="panel-head"><h2>'+esc(o.symbol)+' · '+esc(o.side.toUpperCase())+'</h2>'+tag(o.status)+'</div><p>'+esc(o.reason)+'</p><p class="fine">'+esc((o.matched_rules||[o.rule]).join(' · ').replaceAll('_',' '))+' · '+when(o.decided_at)+'</p><div class="setup-prices"><div><span>Modeled entry</span><strong>'+num(o.entry??o.signal_price)+'</strong></div><div><span>Invalidation</span><strong>'+num(o.invalidation)+'</strong></div><div><span>Target</span><strong>'+num(o.target)+'</strong></div></div>'+(o.blocked_reason?'<p class="fine">'+esc(o.blocked_reason)+'</p>':'')+'<p class="fine">Paper position: '+esc(o.paper_status||'No position confirmed')+' · Entry window ends '+when(o.expires_at)+'</p><details data-key="setup-'+esc(o.id)+'"><summary>Supporting observations</summary>'+table(['Source','Observation','As of'],(o.evidence||[]).map(e=>[esc(e.source),esc(e.kind.replaceAll('_',' '))+(e.symbol?' '+esc(e.symbol):'')+(e.agreement?' · '+esc(e.agreement):'')+(e.level?' · '+num(e.level):''),when(e.source_ts)]))+'</details></article>').join(''):empty('No active setup has qualified','The scanner waits for a fresh trigger, defined invalidation, and usable price data. Use All recent decisions to review expired or blocked setups.');
 $('#scanner-focus').innerHTML=scanner.focus.length?table(['Symbol','Reason','Requested'],scanner.focus.map(f=>[esc(f.symbol),esc(f.reason),when(f.at)])):empty('No additional symbols prioritized','Core index coverage continues while the full watchlist is scanned.');
 $('#scanner-coverage').innerHTML=table(['Research feed','State','Source age','Retrieved','Target cadence'],scanner.feeds.map(f=>[esc(f.label),tag(f.status),age(f.source_age),when(f.received),age(f.target_interval)]))+'<p class="fine">Target cadence is a scheduling goal. Provider caching, quotas, and failures can delay a refresh. Unknown source times are not treated as current.</p>';
}
let researchKey='',researchLoaded=0;
function updateResearchChoices(feeds){
 const select=$('#research-source');
 if(!select.options.length){select.innerHTML=feeds.map(f=>'<option value="'+esc(f.key)+'">'+esc(f.label)+'</option>').join('');}
 if($('#research').classList.contains('active')&&Date.now()-researchLoaded>15000)loadResearch();
}
function readable(value){
 if(value===null||value===undefined)return '—';
 if(typeof value==='object')return JSON.stringify(value).slice(0,1600);
 return String(value);
}
async function loadResearch(){
 const key=$('#research-source').value;
 if(!key)return;
 researchLoaded=Date.now();researchKey=key;
 try{
  const response=await fetch('/api/research?key='+encodeURIComponent(key));if(!response.ok)throw new Error('Unable to read research source');
  const d=await response.json();if(researchKey!==key)return;
  const meta='<h2>'+esc(d.label||key)+'</h2><p>'+tag(d.status)+' <span class="fine">Source '+when(d.source_ts)+' · Retrieved '+when(d.received)+'</span></p>';
  const rows=(d.items||[]).filter(r=>!r.symbol||lastState.scanner.watchlist.includes(r.symbol));
  let content=rows.length?rows.slice(0,100).map(r=>'<details data-key="research-'+esc(key+'-'+(r.symbol||''))+'"><summary>'+esc(r.symbol||'Market observation')+' · '+when(r.source_ts)+'</summary>'+table(['Field','Reported value'],Object.entries(r.data).map(([k,v])=>[esc(k.replaceAll('_',' ')),'<span class="research-value">'+esc(readable(v))+'</span>']))+'</details>').join(''):d.data?'<pre>'+esc(JSON.stringify(d.data,null,2))+'</pre>':empty('Waiting for this source','The coverage table shows its collector status.');
  $('#research-detail').innerHTML=meta+content+'<p class="fine">Vendor-reported information. A missing source timestamp cannot establish a current trading condition. Up to 100 matching rows shown.</p>';
 }catch(error){$('#research-detail').textContent=error.message;}
}
let mapData=null,mapRequested='',mapLoaded=0,mapPending=false;
async function loadStrikeMap(symbol){
 if(!symbol||mapPending)return;
 mapPending=true;mapRequested=symbol;mapLoaded=Date.now();
 try{const response=await fetch('/api/matrix?symbol='+encodeURIComponent(symbol));if(!response.ok)throw new Error('Unable to load strike matrix');const data=await response.json();if(mapRequested===symbol){mapData=data;mapLoaded=Date.now();}}
 catch(error){$('#strike-map').textContent=error.message;}
 finally{mapPending=false;if(lastState)renderStrikeMap(lastState.matrix);}
}
function renderStrikeMap(matrices){
 const choices=Object.values(matrices).filter(m=>m.symbol&&m.strikes?.length);
 const select=$('#map-symbol'),chosen=select.value;
 select.innerHTML=choices.map(m=>'<option value="'+esc(m.symbol)+'">'+esc(m.symbol)+'</option>').join('');
 if(choices.some(m=>m.symbol===chosen))select.value=chosen;
 const summary=choices.find(m=>m.symbol===select.value);
 if(summary&&((mapData?.symbol!==summary.symbol&&mapRequested!==summary.symbol)||Date.now()-mapLoaded>15000)&&!mapPending){loadStrikeMap(summary.symbol);}
 const m=mapData?.symbol===select.value?mapData:null;
 if(!m){$('#strike-map').innerHTML=empty('Waiting for a matrix','The strike map uses vendor-reported GEX and VEX cells.');return;}
 const metric=$('#map-metric').value;
 const near=[...m.strikes].sort((a,b)=>Math.abs(a.strike-m.spot)-Math.abs(b.strike-m.spot)).slice(0,25).sort((a,b)=>b.strike-a.strike);
 const values=near.flatMap(r=>(r[metric+'_cells']||[]).slice(0,8)).filter(v=>v!==null);
 const maximum=Math.max(1,...values.map(Math.abs));
 const rows=near.map(r=>[num(r.strike),...(r[metric+'_cells']||[]).slice(0,8).map(v=>v===null?'—':'<span class="exposure-cell '+(v>=0?'positive':'negative')+' strength-'+Math.min(4,Math.ceil(Math.abs(v)/maximum*4))+'" title="'+esc(num(v))+'">'+compact(v)+'</span>')]);
 $('#strike-map').innerHTML='<p class="fine">'+esc(m.symbol)+' · '+metric.toUpperCase()+' · Source '+when(m.source_ts)+' · nearest 25 strikes / first 8 expirations · vendor units</p>'+table(['Strike',...m.expirations.slice(0,8).map(e=>e.expiry)],rows);
}
$('#scanner-filter').onchange=()=>{if(lastState)renderScanner(lastState.scanner);};
$('#research-source').onchange=loadResearch;
$('#map-symbol').onchange=()=>{if(lastState)renderStrikeMap(lastState.matrix);};
$('#map-metric').onchange=()=>{if(lastState)renderStrikeMap(lastState.matrix);};
function renderProjects(data){
 if(!data)return;
 const names=Object.fromEntries(data.projects.map(p=>[p.project,p.name]));
 $('#project-status').innerHTML=data.projects.map(p=>'<div class="stat"><div class="label">'+esc(p.name)+'</div><p>'+tag(p.status)+'</p><div class="value">'+num(p.record_count,0)+'</div><div class="fine">Saved source records</div><p class="fine">'+(p.project==='futures'?(p.streams||[]).map(s=>esc(s.stream.toUpperCase())+' · '+tag(s.status)+'<br>'+when(s.last_received)).join('<br>'):('Source checked '+when(p.checked_at)+'<br>Last scan '+num(p.cycle_seconds,2)+'s · target 5s'))+'</p></div>').join('');
 const filter=$('#project-filter').value;
 const rows=data.records.filter(r=>filter==='all'||r.project===filter);
 $('#project-records').innerHTML=rows.length?rows.map(r=>{
  const ctx=r.context||{},o=r.outcome||{};
  const result=r.project==='morning'?(o.return_pct==null?'Awaiting checkpoints':num(o.return_pct)+'% underlying'):
    r.project==='smoothers'?String(o.status||r.status)+' · underlying target test':r.source_format==='pine_alert_v1'?String(o.reason||r.status)+' · broker outcome unverified':String(o.prev_position||'')+' → '+String(o.position||'')+' · emulator';
  return '<details data-key="project-'+esc(r.project+'-'+r.id)+'"><summary><strong>'+esc(r.symbol)+'</strong> · '+esc(names[r.project])+' · '+tag(r.status)+' · '+when(r.source_ts)+'</summary><p>'+esc(r.strategy)+' v'+esc(r.version)+' · '+esc(r.stream)+' · '+esc(r.side)+'</p>'+table([r.project==='futures'?'Source price':'Entry / target','Latest outcome','Context captured'],[[r.project==='futures'?num(r.source_price??r.fill_price):num(r.entry)+' / '+num(r.target),esc(result),when(ctx.captured_at)]])+'<p class="fine">'+esc(r.source_price_basis||'')+'</p><p class="fine">'+esc(ctx.timing_note)+'</p><h3>Recorded context</h3><pre>'+esc(JSON.stringify(ctx,null,2))+'</pre><h3>Source measurements</h3><pre>'+esc(JSON.stringify({outcome:o,stop:r.stop,target:r.target,option:r.option,checkpoints:r.checkpoints,option_sample_count:r.option_sample_count,original:r.original},null,2))+'</pre><p><a href="/api/projects/record?project='+encodeURIComponent(r.project)+'&amp;id='+encodeURIComponent(r.id)+'" target="_blank" rel="noreferrer">Full source record</a></p></details>';
 }).join(''):empty('No matching source observations','Connected sources can be healthy while no strategy signal has fired.');
}
$('#project-filter').onchange=()=>{if(lastState)renderProjects(lastState.projects);};
if(location.hash==='#projects')document.querySelector('[data-tab="projects"]').click();

function renderSecondary(data){
 if(!data)return;
 const names={morning:'Morning Algo',smoothers:'Smoothers',futures:'TradingView futures',compass_futures:'Compass futures'};
 const labels={supported:'Supported',watch:'Watch',rejected:'Rejected',insufficient_data:'Insufficient data'};
 const status=data.status||{}, count=data.counts||{};
 const stale=!status.at||Date.now()/1000-status.at>25;
 $('#secondary-status').textContent=(status.enabled?(stale?'Review worker update is stale. ':'Review worker active. '):'Awaiting review worker. ')+
  'Originals run independently. Started '+when(data.activation?.at)+'. Last check '+when(status.at)+'. Comparison refreshed '+when(data.report_at)+'. '+data.version+
  (data.window?.truncated?' · Comparison limited to the latest 5,000 reviews.':' · Rolling 30-day comparison.');
 $('#secondary-stats').innerHTML=Object.entries(labels).map(([key,label])=>'<div class="stat"><div class="label">'+label+'</div><div class="value">'+num(count[key]||0,0)+'</div><div class="fine">'+(key==='supported'?'Selected by the secondary rules':key==='insufficient_data'?'Excluded from directional comparison':'Retained for comparison')+'</div></div>').join('');
 const horizon=Number($('#secondary-horizon').value);
 const groups=(data.comparisons||[]).filter(g=>$('#secondary-filter').value==='all'||g.project===$('#secondary-filter').value);
 const pct=v=>v===null||v===undefined?'—':num(v,3)+'%';
 $('#secondary-comparison').innerHTML=groups.length?table(['Source / symbol / strategy','Measured / eligible','Selected','All candidates: mean','Selected: mean','Filter per candidate','Missed positive / avoided negative','Pending / missing / session end'],groups.map(g=>{const h=g.horizons.find(h=>h.minutes===horizon);return [esc(g.label)+' · '+esc(g.symbol)+' · '+esc(g.side)+'<br><span class="fine">'+esc(g.strategy)+' · '+esc(g.source_version)+'</span>',num(h.measured,0)+' / '+num(h.eligible,0),num(h.selected,0),pct(h.original_mean_pct),pct(h.selected_mean_pct),pct(h.filter_per_candidate_pct),num(h.missed_positive,0)+' / '+num(h.avoided_negative,0),num(h.pending,0)+' / '+num(h.missing,0)+' / '+num(h.session_boundary,0)];})):empty('Waiting for forward candidates','Every supported, watch and rejected candidate is measured from the same review-time reference. The first comparison needs a completed checkpoint.');
 const weekly=groups.filter(g=>g.project==='smoothers');
 $('#secondary-weekly').innerHTML=weekly.length?'<h3>Smoothers weekly target outcomes</h3>'+table(['Symbol / strategy','All resolved: target hits','Selected resolved: target hits'],weekly.map(g=>[esc(g.symbol)+' · '+esc(g.side)+' · '+esc(g.strategy),num(g.weekly.target_hits,0)+' / '+num(g.weekly.resolved,0),num(g.weekly.selected_target_hits,0)+' / '+num(g.weekly.selected_resolved,0)]))+'<p class="fine">These are the original underlying target tests. Option observations remain available in each full record; a target hit is not an option profit.</p>':'';
 const rows=data.reviews.filter(r=>$('#secondary-filter').value==='all'||r.project===$('#secondary-filter').value);
 $('#secondary-reviews').innerHTML=rows.length?rows.map(r=>{
  const d=r.decision,m=r.measurements;
  return '<details data-key="secondary-'+esc(r.id)+'"><summary><strong>'+esc(names[r.project])+' | SECONDARY '+esc(labels[r.verdict].toUpperCase())+' | '+esc(r.symbol)+' · '+esc(r.side.toUpperCase())+'</strong> · '+when(r.decided_at)+'</summary>'+
   '<p>'+esc(r.candidate.strategy)+' · '+esc(d.reasons.join('; '))+'</p><p class="fine">Original reference '+when(r.source_ts)+' · candidate available '+when(r.candidate.available_at??r.source_ts)+' · review delay '+num(d.latency_seconds,1)+'s · '+esc(r.version)+'</p>'+
   '<p>Review reference '+num(m.anchor)+' · '+esc(m.market_symbol||'Independent quote unavailable')+'</p>'+
   (d.support.length?'<p>Support: '+esc(d.support.join('; '))+'</p>':'')+(d.cautions.length?'<p>Watch points: '+esc(d.cautions.join('; '))+'</p>':'')+
   '<p class="fine">'+esc(d.optional_missing.join('; '))+'</p>'+
   table(['Checkpoint','Observation','Direction-adjusted move'],Object.entries(m.horizons||{}).map(([h,p])=>[h+'m',esc(p.status)+(p.source_ts?' · '+when(p.source_ts):''),pct(p.return_pct)]))+
   '<p class="fine">'+esc(m.basis)+' · samples '+num(m.samples,0)+' · largest sampling gap '+num(m.max_gap_seconds,1)+'s. Sampled excursions are not tick-complete.</p>'+
   '<p class="fine">Original ref: '+esc(r.source_id)+'</p><p><a href="/api/secondary/record?id='+encodeURIComponent(r.id)+'" target="_blank" rel="noreferrer">Frozen inputs and original outcome record</a></p></details>';
 }).join(''):empty('No secondary assessments yet','New source candidates will receive a separately labeled review. Historical originals will not be turned into fresh trade alerts.');
}
$('#secondary-filter').onchange=()=>{if(lastState)renderSecondary(lastState.secondary);};
$('#secondary-horizon').onchange=()=>{if(lastState)renderSecondary(lastState.secondary);};
if(location.hash==='#secondary')document.querySelector('[data-tab="secondary"]').click();
poll();
