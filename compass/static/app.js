const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num=(x,d=2)=>x===null||x===undefined?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:d});
const when=x=>x?new Date(x*1000).toLocaleString('en-US',{timeZone:'America/Chicago',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})+' CT':'No observation';
const age=x=>x===null||x===undefined?'—':x<120?num(x,1)+'s':x<7200?num(x/60,1)+'m':num(x/3600,1)+'h';
const compact=x=>x===null||x===undefined?'—':Intl.NumberFormat('en-US',{notation:'compact',maximumFractionDigits:2}).format(x);
const empty=(title,sub)=>'<div class="empty"><strong>'+esc(title)+'</strong>'+esc(sub)+'</div>';
const tag=(s)=>'<span class="tag '+(['ready','current','receiving','available','running','connected','delivered','entered','triggered','setup_triggered'].includes(s)?'good':['stale','error','clock_error','not_configured','blocked','missing','partial','source_time_unknown','invalidated','event_stale','poll_stale','vendor_stale','mixed'].includes(s)?'bad':'')+'">'+esc(String(s||'pending').replaceAll('_',' '))+'</span>';
function table(head,rows){return '<table><thead><tr>'+head.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>';}
const titles={obsidian:'Obsidian Watchlist','swing-ideas':'Swing ideas','option-ideas':'Options ideas','setup-study':'Setup results',secondary:'Secondary review',projects:'Connected projects',scanner:'Live scanner',research:'Research desk',overview:'Session overview',exposure:'Exposure context',flow:'Options flow',trades:'Simulated trades',assistant:'Ask Compass',health:'Feed health'};
document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>{document.querySelectorAll('.nav,.tab').forEach(n=>n.classList.remove('active'));b.classList.add('active');$('#'+b.dataset.tab).classList.add('active');$('#title').textContent=titles[b.dataset.tab];});
$('#logout').onclick=async()=>{await fetch('/logout',{method:'POST'});location.href='/login';};
let lastState=null,first=true,testEvent=null;
let secondaryReport=null,secondaryReportRequest=0;
function vendorMatrix(items){
 const matrices=Object.entries(items).filter(([key,item])=>key!=='matrix:unusual_activity'&&Array.isArray(item.strikes));
 if(!matrices.length)return empty('Waiting for vendor matrices','The source snapshot time and returned strike concentrations will appear here.');
 return '<div class="exposure-grid">'+matrices.map(([key,e])=>{
  const ranked=[...e.strikes].filter(r=>r.gex!==null).sort((a,b)=>Math.abs(b.gex)-Math.abs(a.gex)).slice(0,12);
  return '<article class="panel"><div class="panel-head"><h2>'+esc(e.symbol)+'</h2>'+tag(e.freshness)+'</div>'+
   '<p class="fine">Cache '+tag(e.context?.cache_status)+' · age '+age(e.context?.source_age)+' / '+age(e.context?.expected_cache_seconds)+' · context '+(e.context?.eligible_for_context?'available':'unavailable')+' · live confirmation '+tag(e.confirmation?.status)+'</p>'+
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
 const f=e.flow_freshness||(typeof e.freshness==='object'?e.freshness:{});
 const context='<p>Coverage '+tag(e.status)+' · Live confirmation '+tag(f.status||'unknown')+' '+(e.limited?'Recovery is continuing. ':'')+esc(e.rejected?e.rejected+' unparsed or redacted rows. ':'')+'</p><p class="fine">Fetched '+when(e.received)+' · latest returned trade '+when(e.source_ts)+' · '+num(e.pages,0)+' / '+num(e.page_cap,0)+' allowed pages</p><p class="fine">'+esc(e.coverage_note)+'</p><p class="fine">'+esc(f.note||'')+'</p>';
 const recovery=e.recovery||{};
 const progress='<p class="fine">Estimated missing IDs '+num(recovery.estimated_gap,0)+' · resume page '+num(recovery.next_page,0)+' / '+num(recovery.page_ceiling,0)+' · last end-of-feed pass '+when(recovery.last_full_pass_at)+'</p>'+((e.prior_gaps||[]).length?'<p class="fine">Prior-day gaps: '+esc(e.prior_gaps.map(g=>g.day+': '+g.estimated_gap+' estimated missing').join(' · '))+'</p>':'');
 const rows=e.rows.slice(0,100);
 const discovery='<p class="fine">Last newly discovered ID '+when(recovery.last_new_id_at)+' · last changed existing ID '+when(recovery.last_correction_at)+' · recorded corrections '+num(recovery.corrections_observed||0,0)+'. First seen is discovery time, including recovery of older pages.</p>';
 return headline+context+progress+discovery+(rows.length?table(['Trade time / first seen','Ticker / contract','Premium','Vendor type','Sentiment','Score'],rows.map(r=>[
  when(r.source_ts)+'<br><span class="fine">First seen '+when(r.first_seen)+'</span>',esc(r.symbol)+' '+esc(r.option_type)+' '+num(r.strike)+'<br><span class="fine">'+esc(r.expiry)+'</span>','$'+num(r.premium),esc(r.classification||'Unclassified'),esc(r.sentiment||'Unknown'),num(r.score,0)])):
  empty('No vendor activity returned for today','This is an empty filtered response; it is not proof of a failed connection.'))+'<p class="fine">Latest '+rows.length+' rows shown. '+esc(e.schema_validation)+'</p>';
}
function renderObsidian(d){
 if(!d)return;
 const h=d.history||{},hr=h.results||[];
 $('#obsidian-history').innerHTML='<p>'+hr.length+' / '+(h.total||0)+' ideas checked · '+(h.complete?'Audit finished':'Pending / running')+'</p><p class="fine">Retrospective trade bars through September 14, 2026. Baseline is the first full minute open after the idea; delays and missing bars can materially affect results. These are not executable fills, provider exits, or live Compass ratings. The ±25% column is a hypothetical sensitivity check; same-bar order is unknown.</p>'+table(['Contract / update','History','Baseline / delay','High / low / last close','15 / 30 / 60 minutes','±25% first touch'],hr.map(r=>[esc(r.contract)+'<br>'+(r.has_update?'Has matched update':'No matched update'),tag(r.status)+'<br>'+num(r.bars,0)+' bars',num(r.anchor)+'<br>'+age(r.anchor_delay_seconds),[r.highest_pct,r.lowest_pct,r.last_close_pct].map(v=>v==null?'—':num(v)+'%').join(' / '),[15,30,60].map(k=>r.checkpoints?.[k]==null?'—':num(r.checkpoints[k])+'%').join(' / '),esc(r.scenario_25||'Unavailable')]));
 $('#obsidian-status').innerHTML='<p>'+tag(d.status.status||'not_configured')+' · '+num(d.total_events,0)+' retained events · '+num(d.total_ideas,0)+' ideas · last poll '+when(d.status.at)+'</p><p class="fine">New ideas receive an independent underlying-direction review in Secondary review. Option measurement starts at the first fresh quote after live receipt and may wait for market open. Historical imports have no reconstructed entry. Latest 100 ideas and events shown.</p>';
 $('#obsidian-ideas').innerHTML=d.ideas.length?table(['Contract','Added / first received','Baseline observation','Latest bid / modeled change','Coverage'],d.ideas.map(r=>{const m=r.measurements;return [esc(r.contract),when(r.source_ts)+'<br>'+when(r.received),tag(m.anchor_status)+'<br>'+num(m.anchor_ask)+'<br>Delay '+age(m.anchor_delay_seconds),num(m.latest_bid)+' / '+(m.liquidation_pct==null?'—':num(m.liquidation_pct)+'%')+'<br>'+when(m.last_observed_at),num(m.samples,0)+' samples · '+(m.anchor_status!=='observed'?'Entry unavailable':m.path_complete?'Sampled path continuous':'Path has gaps')];})):empty('Waiting for watchlist ideas','The feed must be connected before events can appear.');
 $('#obsidian-events').innerHTML=table(['Event / contract','Source time','Provider-reported change','Association'],d.events.map(r=>[esc(r.payload.type)+' #'+num(r.vendor_id,0)+'<br>'+esc(r.payload.contract),when(r.payload.source_ts),r.payload.vendor_percentage==null?'—':num(r.payload.vendor_percentage)+'%',esc(r.association)]))+'<p class="fine">Repeated additions for the same contract can make update ownership ambiguous. Reported returns are never used as measured fills or profits.</p>';
}
function render(d){
 renderObsidian(d.obsidian);
 const openDetails=new Set([...document.querySelectorAll('details[open][data-key]')].map(e=>e.dataset.key));
 lastState=d;
 $('#market').textContent=d.markets.equities?'EQUITY SESSION OPEN':d.markets.futures?'FUTURES SESSION OPEN':'MARKETS CLOSED';
 $('#clock').textContent=when(d.asof);
 $('#updated').textContent='Refreshed '+when(d.asof);
 const missing=d.health.filter(h=>['not_configured','error','stale','waiting','partial','blocked','poll_stale','event_stale','clock_error','no_events','vendor_stale'].includes(h.status));
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
 const coreMatrices=['matrix:SPY','matrix:QQQ','matrix:IWM'];
 $('#matrix').innerHTML=vendorMatrix(Object.fromEntries(Object.entries(d.matrix).sort(([a],[b])=>Number(coreMatrices.includes(b))-Number(coreMatrices.includes(a))).slice(0,6)));
 renderScanner(d.scanner);
 renderProjects(d.projects);
 renderSecondary(d.secondary);
 renderSetupStudy(d.setup_study);
 renderOptionIdeas(d.option_ideas);
 renderSwingIdeas(d.swing_ideas,d.asof);
 renderStrikeMap(d.matrix);
 updateResearchChoices(d.scanner.feeds);
 $('#vendor-flows').innerHTML=vendorFlow(d.matrix['matrix:unusual_activity']);
 $('#flows').innerHTML=d.flow.length?table(['Time','Contract','Premium','Size','Classification'],d.flow.map(r=>[when(r.ts),esc(r.symbol),'$'+num(r.payload.premium),num(r.payload.size),esc(r.payload.classification)])):empty('No qualifying prints recorded','Continuous selected-contract trades are recorded when Massive is connected.');
 $('#ledger').innerHTML=d.trades.length?table(['Entry time','Symbol','Strategy','State','Qty','Entry','Stop','Target','Exit','P&L'],d.trades.map(p=>[when(p.entered_at),esc(p.symbol),esc(p.strategy),tag(p.status),num(p.qty,0),num(p.entry),num(p.stop),num(p.target),num(p.exit),num(p.pnl??p.unrealized)])):empty('No simulated trades yet','The engine requires a complete setup, a fresh executable quote, and available risk capacity.');
 $('#quote-checks').innerHTML=table(['Configured symbol','Resolved symbol','Status','Quote age','Source time'],d.quote_checks.map(q=>[esc(q.configured_symbol),esc(q.symbol),tag(q.status),age(q.age),when(q.source_ts)]));
 const fa=d.forward_acceptance||{};
 $('#forward-acceptance').innerHTML='<p>Correction period begins '+when(fa.since)+'</p>'+table(['Option cohort','Total','States','Reasons'],[['Before correction',num(fa.options_before?.total,0),esc(JSON.stringify(fa.options_before?.states||{})),esc(JSON.stringify(fa.options_before?.reasons||{}))],['After correction',num(fa.options_after?.total,0),esc(JSON.stringify(fa.options_after?.states||{})),esc(JSON.stringify(fa.options_after?.reasons||{}))]])+'<p>Swing daily history ready: '+num(fa.swing_daily_ready,0)+' / '+num(fa.swing_symbols,0)+' · Technical candidates '+num(fa.swing_technical_candidates,0)+' · Without flow '+num(fa.swing_without_flow,0)+'</p><p>Current swing scan: '+esc(JSON.stringify(fa.swing_scan||{}))+'</p><p class="fine">'+esc(fa.note||'Waiting for audit')+'</p>';
 const storage=d.storage||{};
 $('#storage-health').innerHTML='<p>'+tag(storage.status||'awaiting_measurement')+' · Checked '+when(storage.at)+'</p>'+table(['Accounted GB','Configured budget GB','Growth GB/hour','Hours to budget'],[[num(storage.accounted_bytes/1e9,2),num(storage.budget_gb,0),num(storage.growth_bytes_per_hour==null?null:storage.growth_bytes_per_hour/1e9,3),num(storage.hours_to_budget_at_observed_rate,1)]])+'<p class="fine">'+esc(storage.basis||'Awaiting PostgreSQL accounting')+'</p><p class="fine">Automatic deletion disabled. Archive destination and durable copy verification pending.</p>'+table(['Largest tables','Total MB','Indexes MB','Estimated rows'],(storage.tables||[]).map(t=>[esc(t.name),num(t.total_bytes/1e6,1),num(t.index_bytes/1e6,1),num(t.estimated_rows,0)]));
 $('#health-table').innerHTML=d.health.length?table(['Component','Status','Source age','Last task update','Worker heartbeat','Detail'],d.health.map(h=>[esc(h.name),tag(h.status),age(h.age),age(h.check_age),age(h.heartbeat_age),'<span class="health-detail">'+esc(h.detail)+(h.stream_contracts!==undefined?' · '+num(h.stream_contracts,0)+' streamed / '+num(h.contracts,0)+' chain contracts':'')+'</span>'])):empty('Waiting for workers','Collector and engine heartbeats will appear here.');
 const delivery=d.delivery,confirmation=delivery.last_confirmation;
 $('#delivery').innerHTML='<p><strong>'+num(delivery.pending,0)+'</strong> alerts queued'+(delivery.pending?' · oldest '+age(delivery.oldest_age):'')+'</p>'+(confirmation?'<p class="fine">Last confirmed event #'+num(confirmation.event_id,0)+' · saved '+when(confirmation.at)+' · Discord message '+esc(confirmation.message_id)+'</p>':'<p class="muted">No saved-message confirmation yet. Send a clearly labeled test to verify delivery.</p>')+'<p class="fine">An alert stays queued until Discord confirms a saved message. Retries can duplicate an event; its event ID stays the same.</p>';
 const f=d.futures,fh=f.session;
 $('#future-readiness').innerHTML='<p>Trading day '+esc(fh.day)+' · '+tag(fh.is_open?'open':'closed')+' · risk resets at 17:00 CT</p><p class="fine">Session '+when(fh.open)+' → '+when(fh.close)+'<br>Last new entry '+when(fh.entry_end)+' · flatten '+when(fh.flatten_at)+'</p>'+table(['Root','Data contract','TradingView contract','Resolved ID'],f.selected.map(t=>{const r=f.contracts.find(r=>r.raw_symbol===t.raw_symbol);return [esc(t.root),esc(t.raw_symbol),esc(t.chart_symbol||t.raw_symbol),r?esc(r.instrument_id):'Awaiting mapping'];}))+'<p class="fine">ES/NQ families use the CME customary quarterly roll. Dow, metals and energy use the provider’s prior-day volume leader. Compare the explicit dated contract; continuous chart settings may differ.</p>'+f.history.map(h=>'<p>'+tag(h.status)+' '+esc(h.detail)+(h.estimated_usd!==undefined?' · reserved download cost $'+num(h.estimated_usd,6):'')+'</p>').join('');
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
 $('#scanner-coverage').innerHTML=table(['Research feed','State','Source age','Retrieved','Target cadence','Cache / live eligibility'],scanner.feeds.map(f=>[esc(f.label),tag(f.status),f.source_age!=null?age(f.source_age):(f.item_clocks||[]).some(r=>r.source_ts!=null)?'<details data-key="clocks-'+esc(f.key)+'"><summary>Per-item clocks</summary>'+table(['Symbol','State','Source time','Age'],f.item_clocks.map(r=>[esc(r.symbol||'Unspecified'),tag(r.status),when(r.source_ts),age(r.source_age)]))+'</details>':age(null),when(f.received),age(f.target_interval),esc((f.cached===true?'Cached':f.cached===false?'Not cached':'Cache unknown')+' · '+(f.usage==='context_only'?'Context only':f.eligible_for_live_confirmation?'Clocks current; price confirmation required':'Not current confirmation'))]))+'<p class="fine">Target cadence is a scheduling goal. Provider caching, quotas, and failures can delay a refresh. Unknown source times are not treated as current. Mixed feeds retain each item’s clock.</p>';
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
 $('#strike-map').innerHTML='<p class="fine">'+esc(m.symbol)+' · '+metric.toUpperCase()+' · Source '+when(m.source_ts)+' · Fetched '+when(m.received)+(m.cached?' · vendor cache':'')+' · nearest 25 strikes / first 8 expirations · vendor units</p>'+table(['Strike',...m.expirations.slice(0,8).map(e=>e.expiry)],rows);
}
$('#scanner-filter').onchange=()=>{if(lastState)renderScanner(lastState.scanner);};
$('#research-source').onchange=loadResearch;
$('#map-symbol').onchange=()=>{if(lastState)renderStrikeMap(lastState.matrix);};
$('#map-metric').onchange=()=>{if(lastState)renderStrikeMap(lastState.matrix);};
function renderProjects(data){
 if(!data)return;
 const migration=data.migration||{};
 const history=data.history_import||{};
 const historyHtml='<h3>Morning research import</h3><p>'+esc(history.basis||'Awaiting import')+'</p>'+table(['Stream','Status','Last complete scan'],Object.entries(history.streams||{}).map(([k,v])=>[esc(k),tag(v.status),when(v.last_complete_at)]))+'<p>'+Object.entries(history.counts||{}).map(([k,v])=>esc(k)+': '+num(v,0)).join(' · ')+'</p>';
 $('#strategy-migration').innerHTML=historyHtml+'<p>'+esc(migration.scope||'Awaiting migration worker')+'</p><p>Mode: '+tag((migration.status||{}).mode)+' · Cutover: not ready · Checked '+when((migration.status||{}).at)+'</p>'+table(['Morning signal','Version / stream','5m','15m','30m','60m'],(migration.rows||[]).slice(0,30).map(r=>[esc(r.symbol)+' · '+when(r.source_ts),esc(r.version)+' / '+esc(r.stream),...r.checkpoints.map(p=>tag(p.status)+(p.return_difference_pp==null?'':' · '+num(p.return_difference_pp,3)+' pp'))]))+'<p class="fine">Paired results compare source candle closes with retained quote midpoints; differences are not strategy failures. Missing observations stay explicit. Originals still own alerts and orders.</p>';
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
 if(secondaryReport)data={...data,comparisons:secondaryReport.comparisons,window:secondaryReport.window,report_at:secondaryReport.at};
 const names={obsidian:'Obsidian Watchlist',morning:'Morning Algo',smoothers:'Smoothers',futures:'TradingView futures',compass_futures:'Compass futures'};
 const labels={supported:'Supported',watch:'Watch',rejected:'Rejected',insufficient_data:'Insufficient data'};
 const status=data.status||{}, filter=$('#secondary-filter').value;
 const groups=(data.comparisons||[]).filter(g=>filter==='all'||g.project===filter);
 const count=groups.reduce((tot,g)=>{for(const key of Object.keys(labels))tot[key]=(tot[key]||0)+(g.counts?.[key]||0);return tot;},{});
 const stale=!status.at||Date.now()/1000-status.at>25;
 $('#secondary-status').textContent=(status.enabled?(stale?'Review worker update is stale. ':'Review worker active. '):'Awaiting review worker. ')+
  'Originals run independently. Started '+when(data.activation?.at)+'. Last check '+when(status.at)+'. Comparison refreshed '+when(data.report_at)+'. '+data.version+
  (secondaryReport?' · Selected review period '+when(data.window.since)+' through '+when(data.window.through)+'.':' · Rolling 30-day comparison.')+
  (data.window?.truncated?' · Comparison limited to the latest 5,000 reviews.':'');
 $('#secondary-stats').innerHTML=Object.entries(labels).map(([key,label])=>'<div class="stat"><div class="label">'+label+'</div><div class="value">'+num(count[key]||0,0)+'</div><div class="fine">'+(key==='supported'?'Selected by the secondary rules':key==='insufficient_data'?'Excluded from directional comparison':'Retained for comparison')+'</div></div>').join('');
 const opening=data.morning_opening;
 if(opening){
  const tested=opening.rows||[];
  let box=$('#morning-opening-comparison');
  if(!box){box=document.createElement('div');box.id='morning-opening-comparison';$('#secondary-stats').after(box);}
  box.innerHTML='<h3>Morning opening review · forward test</h3><p class="fine">Separate opening-range review after at least five complete minutes. Existing alerts remain authoritative. '+tested.length+' recorded reviews'+(opening.truncated?' (latest 500)':'')+'. Checked '+esc(when(opening.at))+'.</p>'+
   (tested.length?table(['Symbol','Opening review','Baseline at same time','Baseline final','15m / 30m / 60m'],tested.slice(0,50).map(r=>[esc(r.symbol),esc(r.decision.verdict),esc(r.baseline_at_capture.verdict),esc(r.baseline_final||'pending'),['15','30','60'].map(h=>esc((r.measurements.horizons[h]||{}).status||'unmeasurable')).join(' / ')])):'<p>Awaiting new opening-session alerts. Earlier decisions are not rescored.</p>');
 }
 const horizon=Number($('#secondary-horizon').value);
 const readiness=data.data_readiness||{}, readinessStale=!readiness.at||Date.now()/1000-readiness.at>45;
 const coverage=(readiness.rows||[]).filter(r=>filter==='all'||r.projects.includes(filter));
 const ready=coverage.filter(r=>r.ready&&!readinessStale).length;
 $('#secondary-readiness-note').textContent=(readinessStale?'Input coverage check is stale. ':'')+ready+' / '+coverage.length+' connected equity symbols have usable review inputs. Checked '+when(readiness.at)+'. Current readiness does not rescore earlier alerts. Quote age must be at most 5 seconds; completed minute context at most 90 seconds. Smoothers also requires completed daily history.';
 $('#secondary-readiness').innerHTML=coverage.length?'<details data-key="secondary-data"><summary>Input status by symbol</summary>'+table(['Symbol','Collection','Quote','Minute context','Daily history','Daily bias'],coverage.map(r=>[esc(r.symbol),r.collection_enabled?'Included':'Missing',r.quote_ready?'Ready · '+num(r.quote_age,1)+'s':'Missing / stale · '+num(r.quote_age,1)+'s',r.minute_ready?'Ready':esc(r.minute_reason||'Missing / stale'),r.projects.includes('smoothers')?(r.daily_ready?'Through '+esc(r.daily_through):'Missing'):'Not required',r.daily_ready?(r.daily_bias===1?'Bullish':r.daily_bias===-1?'Bearish':'Neutral'):'Unavailable']))+'</details>':empty('No connected equity coverage','Equity review inputs appear after source records are imported.');
 const waiting=(data.pending||[]).filter(r=>filter==='all'||r.project===filter);
 $('#secondary-pending').innerHTML=waiting.length?'<h3>Waiting for current data</h3>'+table(['Source / symbol','Deadline','Attempts','Missing inputs'],waiting.map(r=>[esc(names[r.project])+' · '+esc(r.symbol),when(r.deadline),num(r.attempts,0),esc(r.missing.join('; '))])):'<p class="fine">No reviews waiting for data. A pending review can retry only within 60 seconds of original candidate availability.</p>';
 const pct=v=>v===null||v===undefined?'—':num(v,3)+'%';
 $('#secondary-comparison').innerHTML=groups.length?table(['Source / symbol / strategy','Measured / eligible','Selected','Supported positive / negative','All candidates: mean','Selected: mean','Filter per candidate','Missed positive / avoided negative','Pending / missing / session end'],groups.map(g=>{const h=g.horizons.find(h=>h.minutes===horizon);return [esc(g.label)+' · '+esc(g.symbol)+' · '+esc(g.side)+'<br><span class="fine">'+esc(g.strategy)+' · '+esc(g.source_version)+'</span>',num(h.measured,0)+' / '+num(h.eligible,0),num(h.selected,0),num(h.supported_positive,0)+' / '+num(h.supported_negative,0),pct(h.original_mean_pct),pct(h.selected_mean_pct),pct(h.filter_per_candidate_pct),num(h.missed_positive,0)+' / '+num(h.avoided_negative,0),num(h.pending,0)+' / '+num(h.missing,0)+' / '+num(h.session_boundary,0)];})):empty('Waiting for forward candidates','Every supported, watch and rejected candidate is measured from the same review-time reference. The first comparison needs a completed checkpoint.');
 const paired=groups.filter(g=>g.tradermatrix?.paired);
 $('#secondary-comparison').innerHTML+='<h3>TraderMatrix contribution</h3>'+(paired.length?table(['Source / symbol','Paired / changed / evidence','Measured / pending / missing','Added winners / losers','Avoided losers / missed winners','Selection difference per candidate'],paired.map(g=>{const t=g.tradermatrix,h=t.horizons.find(x=>x.minutes===horizon);return [esc(g.label)+' · '+esc(g.symbol)+' · '+esc(g.side),t.paired+' / '+t.changed+' / '+t.with_evidence,h.measured+' / '+h.pending+' / '+h.missing,h.added_winners+' / '+h.added_losers,h.avoided_losers+' / '+h.missed_winners,pct(h.selection_delta_per_candidate_pct)];})):'<p class="fine">Waiting for new paired reviews. Earlier decisions are excluded.</p>')+'<p class="fine">Same candidate and price observations, with and without TraderMatrix flow and levels. Underlying midpoint results, not option profit or proof of causation.</p>';
 const weekly=groups.filter(g=>g.project==='smoothers');
 $('#secondary-weekly').innerHTML=weekly.length?'<h3>Smoothers weekly target outcomes</h3>'+table(['Symbol / strategy','All resolved: target hits','Selected resolved: target hits'],weekly.map(g=>[esc(g.symbol)+' · '+esc(g.side)+' · '+esc(g.strategy),num(g.weekly.target_hits,0)+' / '+num(g.weekly.resolved,0),num(g.weekly.selected_target_hits,0)+' / '+num(g.weekly.selected_resolved,0)]))+'<p class="fine">These are the original underlying target tests. Option observations remain available in each full record; a target hit is not an option profit.</p>':'';
 const pairedWeekly=weekly.filter(g=>g.tradermatrix?.paired);
 if(pairedWeekly.length)$('#secondary-weekly').innerHTML+='<h3>TraderMatrix weekly comparison</h3>'+table(['Symbol / side','Resolved paired','With vendor: targets / selected','Without vendor: targets / selected'],pairedWeekly.map(g=>{const w=g.tradermatrix.weekly;return [esc(g.symbol)+' · '+esc(g.side),w.resolved,w.vendor_target_hits+' / '+w.vendor_selected,w.baseline_target_hits+' / '+w.baseline_selected];}));
 const rows=data.reviews.filter(r=>filter==='all'||r.project===filter).slice(0,100);
 $('#secondary-reviews').innerHTML=rows.length?rows.map(r=>{
  const d=r.decision,m=r.measurements;
  return '<details data-key="secondary-'+esc(r.id)+'"><summary><strong>'+esc(names[r.project])+' | SECONDARY '+esc(labels[r.verdict].toUpperCase())+' | '+esc(r.symbol)+' · '+esc(r.side.toUpperCase())+'</strong> · '+when(r.decided_at)+'</summary>'+
   '<p>'+esc(r.candidate.strategy)+' · '+esc(d.reasons.join('; '))+'</p><p class="fine">Original reference '+when(r.source_ts)+' · candidate available '+when(r.candidate.available_at??r.source_ts)+' · review delay '+num(d.latency_seconds,1)+'s · '+esc(r.version)+'</p>'+
   '<p>Review reference '+num(m.anchor)+' · '+esc(m.market_symbol||'Independent quote unavailable')+'</p>'+
   (d.readiness_attempts?'<p class="fine">Data checks: '+num(d.readiness_attempts,0)+' · waited '+num(d.readiness_wait_seconds,1)+'s within the original review window.</p>':'')+
   (d.data_checks?table(['Frozen input','Status','Source time / detail'],[
    ['Independent quote',d.data_checks.quote.ready?'Ready':'Missing / stale',when(d.data_checks.quote.source_ts)],
    ['Completed minute context',d.data_checks.minute.ready?'Ready':'Missing / stale',when(d.data_checks.minute.source_ts)+' · '+esc(d.data_checks.minute.reason||'')],
    ['Completed daily history',d.data_checks.daily.status==='ready'?'Ready':r.project==='smoothers'?'Missing':'Not required',d.data_checks.daily.status==='ready'?esc(d.data_checks.daily.through||'')+' · EMA21 '+num(d.data_checks.daily.ema21)+' / EMA50 '+num(d.data_checks.daily.ema50):esc(d.data_checks.daily.note||'')]]):'<p class="fine">Legacy review: its daily-trend caution may also mean missing history. Original decision preserved.</p>')+
   (d.support.length?'<p>Support: '+esc(d.support.join('; '))+'</p>':'')+(d.cautions.length?'<p>Watch points: '+esc(d.cautions.join('; '))+'</p>':'')+
   '<p class="fine">'+esc(d.optional_missing.join('; '))+'</p>'+
   table(['Checkpoint','Observation','Evidence','Direction-adjusted move'],Object.entries(m.horizons||{}).map(([h,p])=>[h+'m',esc(p.status)+(p.source_ts?' · '+when(p.source_ts):''),esc(p.observation_source==='retained_quote'?'Retained quote':p.observation_source==='live_quote'?'Live quote':p.reason||'')+(p.recovered_at?' · reviewed '+when(p.recovered_at):''),pct(p.return_pct)]))+
   '<p class="fine">'+esc(m.basis)+' · samples '+num(m.samples,0)+' · largest sampling gap '+num(m.max_gap_seconds,1)+'s. Sampled excursions are not tick-complete.</p>'+
   '<p class="fine">Original ref: '+esc(r.source_id)+'</p><p><a href="/api/secondary/record?id='+encodeURIComponent(r.id)+'" target="_blank" rel="noreferrer">Frozen inputs and original outcome record</a></p></details>';
 }).join(''):empty('No secondary assessments yet','New source candidates will receive a separately labeled review. Historical originals will not be turned into fresh trade alerts.');
}
$('#secondary-filter').onchange=()=>{if(lastState)renderSecondary(lastState.secondary);};
$('#secondary-horizon').onchange=()=>{if(lastState)renderSecondary(lastState.secondary);};
$('#secondary-period-apply').onclick=async()=>{
 const request=++secondaryReportRequest, since=new Date($('#secondary-since').value).getTime()/1000;
 if(!Number.isFinite(since)){$('#secondary-period-note').textContent='Choose a valid start date and time.';return;}
 $('#secondary-period-note').textContent='Loading selected review period…';
 try{
  const response=await fetch('/api/secondary/report?since='+encodeURIComponent(since));
  const report=await response.json();
  if(!response.ok)throw new Error(typeof report.detail==='string'?report.detail:'Could not load this period.');
  if(request!==secondaryReportRequest)return;
  secondaryReport=report;
  $('#secondary-period-note').textContent='Loaded '+when(report.at)+'. Click Load period again to include newer measurements. Missing observations remain excluded from returns.';
  if(lastState)renderSecondary(lastState.secondary);
 }catch(error){if(request===secondaryReportRequest)$('#secondary-period-note').textContent=error.message;}
};
$('#secondary-period-reset').onclick=()=>{
 secondaryReportRequest++;secondaryReport=null;$('#secondary-since').value='';
 $('#secondary-period-note').textContent='Rolling 30-day comparison restored.';
 if(lastState)renderSecondary(lastState.secondary);
};
if(location.hash==='#secondary')document.querySelector('[data-tab="secondary"]').click();
poll();

function renderSetupStudy(d){
 if(!d)return;
 const cohort=$('#study-cohort').value;
 const allowed=r=>cohort==='all'||(cohort==='alerted'?r.alerted:!r.alerted);
 $('#study-status').textContent=(d.enabled?'Tracking enabled. ':'Tracking disabled. ')+(d.activation?'Started '+when(d.activation.at)+'. ':'')+'Portfolio entry limit: '+(d.max_entries||'unlimited')+'. Latest study check '+when(d.worker?.at)+'. '+(d.window_days||30)+'-day report; '+(d.count||0)+' trials'+(d.truncated?' (10,000-record reporting window reached)':'')+'. Closed results include modeled fees. Pending, excluded and unresolved observations are outside win rate.';

 const variants=d.entry_variants||{};
 const variantNames={repeated:'Every entry',first_per_trend:'First entry per trend',pullback_reset:'Entry after pullback reset'};
 $('#study-variants-status').textContent='Forward research started '+when(d.entry_variants_activation?.at)+'. First entry means the first eligible setup observed per rule and direction in a completed 15-minute trend. Pullback entries require a later EMA21 touch and a separate close beyond EMA9. All variants share original fills. Unknown context and unresolved quotes remain explicit. Earlier trials outside this comparison: '+(variants.pre_activation_trials||0)+'.';
 const vg=(variants.groups||[]).filter(allowed);
 $('#study-variants').innerHTML=vg.length?table(['Contract / setup / side','Variant / cohort','Selected / all','Skipped / unknown / excluded','Closed: wins / losses / flat','Win rate / mean R','Open / unresolved'],vg.map(g=>[esc(g.symbol)+'<br>'+esc(g.strategy)+' · '+esc(g.side)+'<br><span class="fine">'+esc(g.model)+' · '+esc(g.fill_version)+'</span>',esc(variantNames[g.variant]||g.variant)+'<br>'+(g.alerted?'Alerted':'Cooldown candidate'),g.selected+' / '+g.total,g.skipped+' / '+g.unknown+' / '+g.excluded,g.wins+' / '+g.losses+' / '+g.breakeven,(g.win_rate===null?'—':num(g.win_rate*100,1)+'%')+' / '+num(g.mean_r,2),g.open+' / '+g.unresolved])):empty('Waiting for forward futures comparisons','Each new futures trial receives frozen entry selections; earlier trials are not reclassified.');
 const groups=(d.groups||[]).filter(allowed);
 $('#study-groups').innerHTML=groups.length?table(['Contract / setup / side','Cohort','Trials / closed','Wins / losses / flat','Target / stop','Win rate','Mean R','Mean duration','Open / unresolved / excluded'],groups.map(g=>[esc(g.symbol)+'<br>'+esc(g.strategy)+' · '+esc(g.side)+'<br><span class="fine">'+esc(g.version)+'</span>',g.alerted?'Alerted':'Cooldown candidate',num(g.total,0)+' / '+num(g.closed,0),g.wins+' / '+g.losses+' / '+g.breakeven,g.targets+' / '+g.stops,g.win_rate===null?'—':num(g.win_rate*100,1)+'%',num(g.mean_r,2),g.mean_seconds===null?'—':num(g.mean_seconds/60,1)+'m',g.open+' / '+g.unresolved+' / '+g.excluded])):empty('Waiting for completed setup observations','New qualifying setups are measured from activation. Earlier signals are not assigned reconstructed trades.');
 $('#study-records').innerHTML=(d.records||[]).filter(allowed).map(p=>'<details data-key="trial-'+esc(p.id)+'"><summary>'+esc(p.symbol)+' · '+esc(p.strategy)+' · '+esc(p.side)+' · '+esc(p.outcome||p.status)+' · '+when(p.started)+'</summary><p>Entry '+num(p.entry,4)+' · stop '+num(p.stop,4)+' · target '+num(p.target,4)+' · exit '+num(p.exit,4)+'</p><p>One-unit net result $'+num(p.pnl,2)+' · '+num(p.r_multiple,2)+'R · '+esc(p.exit_reason||p.reason||'Awaiting outcome')+'</p><p class="fine">'+esc(p.basis)+' · samples '+num(p.samples,0)+' · replayed samples '+num(p.replayed_samples||0,0)+' · largest quote gap '+num(p.max_gap_seconds,1)+'s · MFE '+num(p.mfe_r,2)+'R / MAE '+num(p.mae_r,2)+'R</p><p class="fine">Source '+esc(p.source_id)+' · '+esc(p.version)+' · '+esc(p.fill_version||'Legacy fill model')+'</p>'+(p.archive_check?'<p class="fine">Last archive check: '+num(p.archive_check.usable_rows,0)+' usable / '+num(p.archive_check.read_rows,0)+' read; timestamp mismatches '+num(p.archive_check.timestamp_mismatches,0)+'; future '+num(p.archive_check.future_when_recorded,0)+'; stale/invalid '+num(p.archive_check.stale_or_invalid_when_recorded,0)+(p.archive_check.read_rows===0?' · latest archived quote '+when(p.archive_check.latest_archived_ts)+' / stored '+when(p.archive_check.latest_archived_received):'')+'</p>':'')+'<a href="/api/setup-study/record?id='+encodeURIComponent(p.id)+'" target="_blank" rel="noreferrer">Full setup record</a></details>').join('')||empty('No setup records in this cohort','Trials continue independently of portfolio limits.');
}
$('#study-cohort').onchange=()=>{if(lastState)renderSetupStudy(lastState.setup_study);};
if(location.hash==='#setup-study')document.querySelector('[data-tab="setup-study"]').click();

function renderOptionIdeas(d){
 if(!d)return;
 const counts=d.counts||{},active=(counts.pending||0)+(counts.open||0);
 $('#ideas-status').textContent=(d.enabled?'Enabled. ':'New ideas disabled. ')+d.min_dte+'–'+d.max_dte+' DTE; target '+d.target_dte+' DTE. New entries from cash open until 30 minutes before close; simulations exit 15 minutes before close. Last check '+when(d.worker?.at)+'.';
 const metrics=[['Active ideas',active,'Pending or tracking live quotes'],['Completed',counts.closed||0,'Option outcomes after modeled costs'],['Unresolved',counts.unresolved||0,'Observation gaps; outside win/loss'],['Excluded',counts.excluded||0,'No qualified option entry']];
 $('#ideas-stats').innerHTML=metrics.map(([label,value,note])=>'<div class="stat"><div class="label">'+esc(label)+'</div><div class="value">'+num(value,0)+'</div><div class="fine">'+esc(note)+'</div></div>').join('');
 const filter=$('#ideas-filter').value;
 const rows=(d.records||[]).filter(p=>filter==='all'||(filter==='active'?['pending','open'].includes(p.status):p.status==='closed'));
 $('#ideas-records').innerHTML=rows.map(p=>{
  const o=p.contract,title=o?p.underlying+' '+num(o.strike)+' '+o.type.toUpperCase()+' · '+o.expiry:p.underlying+' · '+(p.underlying_side==='long'?'CALL candidate':'PUT candidate');
  const pct=p.status==='closed'?p.return_pct:p.status==='open'?p.mark_pct:null;
  return '<details data-key="idea-'+esc(p.id)+'"><summary>'+esc(title)+' · '+esc(p.outcome||p.status)+(pct!==null&&pct!==undefined?' · '+num(pct,1)+'%':'')+' · '+when(p.opened_at||p.created_at)+'</summary>'+
   '<p>'+esc(p.reason)+'</p><p>Underlying invalidation '+num(p.underlying_stop)+' · underlying target '+num(p.underlying_target)+'</p>'+
   (o?'<p>Option entry $'+num(p.entry)+' · premium stop $'+num(p.premium_stop)+' · premium target $'+num(p.premium_target)+'</p><p>Last option bid / ask $'+num(p.last_quote?.bid)+' / $'+num(p.last_quote?.ask)+' · source '+when(p.last_quote?.ts)+'</p>':'<p>'+esc(p.waiting_reason||p.exit_reason)+'</p>')+
   (p.status==='closed'?'<p>Simulated exit $'+num(p.exit)+' · net $'+num(p.pnl)+' · '+num(p.return_pct,1)+'% · '+esc(p.exit_reason)+'</p>':
    p.status==='open'?'<p>Current modeled net $'+num(p.unrealized)+' · '+num(p.mark_pct,1)+'%</p>':'<p>'+esc(p.exit_reason||'Awaiting fresh contract quote')+'</p>')+
   '<p class="fine">Best observed '+num(p.best_pct,1)+'% at '+when(p.peak_at)+' · samples '+num(p.samples,0)+' · replayed samples '+num(p.replayed_samples||0,0)+' · largest quote gap '+num(p.max_gap_seconds,1)+'s</p>'+
   '<p class="fine">Intraday exit deadline '+when(p.flatten_at)+' · '+esc(p.basis)+'</p>'+
   '<p class="fine">Source setup '+esc(p.source_id)+' · '+esc(p.version)+'</p></details>';
 }).join('')||empty('Waiting for a qualifying options idea','Fresh price triggers prioritize a chain and live quotes. Pending candidates are not option entries.');
}
$('#ideas-filter').onchange=()=>{if(lastState)renderOptionIdeas(lastState.option_ideas);};
if(location.hash==='#option-ideas')document.querySelector('[data-tab="option-ideas"]').click();

function renderSwingIdeas(d,asof){
 if(!d)return;
 const counts=d.counts||{},workerAge=asof-(d.worker?.at||0);
 $('#swings-status').textContent=(d.enabled?'Enabled. ':'New swings disabled. ')+d.min_dte+'–'+d.max_dte+' DTE; target '+d.target_dte+' DTE; maximum '+d.max_hold_sessions+' trading sessions including entry. Entries stop 30 minutes before cash close. '+(workerAge>30?'Worker heartbeat needs attention. ':'')+'Last check '+when(d.worker?.at)+'.';
 const metrics=[['Daily history ready',d.daily_ready+' / '+d.watch_symbols,'Full watchlist; completed daily and weekly bars'],['Active swings',(counts.pending||0)+(counts.open||0),'Pending or open option observations'],['Completed',counts.closed||0,'Net option results after modeled costs'],['Unresolved / excluded',(counts.unresolved||0)+' / '+(counts.excluded||0),'Outside win/loss; no invented fills']];
 $('#swings-stats').innerHTML=metrics.map(([label,value,note])=>'<div class="stat"><div class="label">'+esc(label)+'</div><div class="value">'+esc(value)+'</div><div class="fine">'+esc(note)+'</div></div>').join('');
 const filter=$('#swings-filter').value;
 const rows=(d.records||[]).filter(p=>filter==='all'||(filter==='active'?['pending','open'].includes(p.status):p.status==='closed'));
 $('#swings-records').innerHTML=rows.map(p=>{
  const o=p.contract,daily=p.daily||{},f=p.flow||{},q=p.last_quote||{};
  const title=o?p.underlying+' $'+num(o.strike)+' '+o.type.toUpperCase()+' · '+o.expiry:p.underlying+' · '+(p.underlying_side==='long'?'CALL candidate':'PUT candidate');
  const pct=p.status==='closed'?p.return_pct:p.status==='open'?p.mark_pct:null;
  return '<details data-key="swing-'+esc(p.id)+'"><summary>'+esc(title)+' · '+esc(p.outcome||p.status)+(pct!=null?' · '+num(pct,1)+'% last observed':'')+' · '+when(p.opened_at||p.created_at)+'</summary>'+
   '<p><strong>'+esc(p.rule.replaceAll('_',' '))+'</strong> · '+esc(p.reason)+'</p>'+
   '<p>Tech: SMA20 '+num(daily.sma20)+' / SMA50 '+num(daily.sma50)+' · RSI '+num(daily.rsi14,1)+' · daily through '+esc(daily.through)+' · completed weekly trend '+esc(daily.weekly_bias)+' through '+esc(daily.weekly_through)+'</p>'+
   '<p>Observed filtered flow, 30m: calls $'+compact(f.call_premium)+' / puts $'+compact(f.put_premium)+'. Vendor-classified '+d.min_dte+'–'+d.max_dte+' DTE flow: bullish $'+compact(f.bullish_premium)+' / bearish $'+compact(f.bearish_premium)+'.</p><p class="fine">'+esc(f.basis)+' Latest source print '+when(f.latest)+'.</p>'+
   '<p>Underlying invalidation '+num(p.underlying_stop)+' · target '+num(p.underlying_target)+'</p>'+
   (o?'<p>Option entry $'+num(p.entry)+' · premium stop $'+num(p.premium_stop)+' · premium target $'+num(p.premium_target)+'</p><p>Last option bid / ask $'+num(q.bid)+' / $'+num(q.ask)+' · source '+when(q.ts)+' · '+esc(p.option_source)+'</p>':'<p>'+esc(p.waiting_reason||p.exit_reason)+'</p>')+
   (p.status==='closed'?'<p>Simulated exit $'+num(p.exit)+' · net $'+num(p.pnl)+' · '+num(p.return_pct,1)+'% · '+esc(p.exit_reason)+'</p>':p.status==='open'?'<p>'+tag(p.observation_state)+' · last observed net $'+num(p.unrealized)+' ('+num(p.mark_pct,1)+'%). '+(asof-q.ts>5?'This is a previous quote, not a live mark.':'')+'</p>':'<p>'+esc(p.exit_reason||'Awaiting an eligible contract and fresh quote')+'</p>')+
   ((p.overnight_gaps||[]).length?'<h3>Opening gaps</h3>'+table(['Session','Option bid change','Stock midpoint change','Observed'],p.overnight_gaps.map(g=>[esc(g.day),'$'+num(g.option_bid_change),'$'+num(g.underlying_mid_change),when(g.observed_at)])):'')+
   '<p class="fine">Final exit deadline '+when(p.exit_deadline)+' · sessions observed '+num(p.sessions_observed?.length,0)+' · best observed '+num(p.best_pct,1)+'% · worst observed '+num(p.worst_pct,1)+'% · largest open-session gap '+num(p.max_gap_seconds,1)+'s</p>'+
   '<p class="fine">'+esc(p.basis)+' · '+esc(p.version)+' · ref '+esc(p.id.slice(0,16))+'</p></details>';
 }).join('')||empty('Waiting for a qualifying swing','Daily breakouts, pullback reclaims and reversals require fresh directional swing flow, an eligible contract and a liquid quote.');
 $('#swings-groups').innerHTML=(d.groups||[]).length?table(['Setup','Direction','State / outcome','Count','Mean net option return'],d.groups.map(g=>[esc(g.rule?.replaceAll('_',' ')),esc(g.side==='long'?'CALL':'PUT'),esc(g.outcome||g.status),num(g.count,0),g.status==='closed'?num(g.avg_return_pct,1)+'%':'—'])):empty('No swing outcomes yet','Forward observations accumulate by setup and direction.');
 $('#swings-coverage-summary').textContent=d.scanned+' / '+d.watch_symbols+' symbols checked today. '+Object.entries(d.scan_counts||{}).map(([k,v])=>k.replaceAll('_',' ')+': '+v).join(' · ')+'. Coverage timestamps show actual cadence; the full scan targets about 24 seconds plus processing.';
 $('#swings-coverage').innerHTML=table(['Symbol','Daily / weekly history','Recent minute bars','Decision','Last scan'],(d.coverage||[]).map(p=>[esc(p.symbol),p.daily_ready?'Ready':esc(p.reason||'Warming up'),p.price_ready?'Ready':'Waiting',tag(p.status),when(p.at)]));
}
$('#swings-filter').onchange=()=>{if(lastState)renderSwingIdeas(lastState.swing_ideas,lastState.asof);};
if(location.hash==='#swing-ideas')document.querySelector('[data-tab="swing-ideas"]').click();

function morningParams(){return new URLSearchParams({ticker:$('#morning-ticker').value.trim(),start:$('#morning-start').value,end:$('#morning-end').value,stream:$('#morning-stream').value.trim(),limit:'100'});}
$('#morning-load').onclick=async()=>{
 const target=$('#morning-report'),button=$('#morning-load');button.disabled=true;target.textContent='Loading original candle research…';
 const params=morningParams();$('#morning-download').href='/api/projects/morning/report?'+params+'&download=true';
 try{
  const response=await fetch('/api/projects/morning/report?'+params);if(!response.ok)throw new Error('Report unavailable (HTTP '+response.status+'). Check filters or sign in again.');
  const r=await response.json();const truncated=Object.entries(r.truncated).filter(([,v])=>v).map(([k])=>k);
  const point=p=>tag(p.status)+(p.return_pct==null?'':' · '+num(p.return_pct)+'%');
  target.innerHTML='<p>Calculated '+when(r.as_of_ms/1000)+' · '+r.summary.signals+' signals · '+r.summary.candidates+' research records.</p>'+(truncated.length?'<p><strong>Truncated: '+esc(truncated.join(', '))+'. Narrow the filters; summaries describe only the selected rows.</strong></p>':'')+'<p>Import streams: '+Object.entries(r.import_status.streams).map(([k,v])=>esc(k)+' '+tag(v.status)).join(' · ')+'</p><h3>Signal coverage and modeled exits</h3>'+table(['Signal','Version / config','Source inventory','Candle coverage','5m close','15m close','30m close','60m close','+1% / −0.5%','+2% / −1%'],r.signals.map(s=>[esc(s.ticker)+' · '+when(s.at_ms/1000),esc(s.script_version)+' / '+esc(s.config_id),tag(s.native_reconciliation.status)+(s.native_reconciliation.source_candles==null?'':' · source '+s.native_reconciliation.source_candles+' / Compass '+s.native_reconciliation.compass_candles),tag(s.stock_history.status)+' · '+s.stock_history.recorded+'/'+s.stock_history.expected,...s.modeled_exits.map(point)]))+'<h3>Stock comparisons — shared complete cohort</h3>'+table(['Setup / bucket / stream','Config','Signals / paired','Incomplete / ambiguous','Rule','Average / positive'],r.stock_comparisons.flatMap(g=>g.stock_rules.map(x=>[esc(g.setup)+' / '+esc(g.time_bucket_central)+' / '+esc(g.stream_id),esc(g.config_id),g.signals+' / '+g.paired_stock_n,g.incomplete_or_legacy+' / '+g.ambiguous_excluded,esc(x.rule),num(x.avg_return_pct)+'% / '+num(x.positive_pct)+'%'])))+'<h3>Research candidate outcomes</h3>'+table(['Candidate','Kind / stream','5m','15m','30m','60m','120m','180m'],r.candidates.map(c=>[esc(c.ticker)+' · '+when(c.at_ms/1000),esc(c.kind)+' / '+esc(c.stream_id),...[5,15,30,60,120,180].map(h=>point(c.outcomes[String(h)]))]))+'<h3>Candidate comparisons</h3>'+table(['Kind / bucket / stream','Config','Horizon','Complete / total','Average return'],r.candidate_comparisons.flatMap(g=>Object.entries(g.outcomes).map(([h,o])=>[esc(g.kind)+' / '+esc(g.time_bucket_central)+' / '+esc(g.stream_id),esc(g.config_id),h+'m',o.complete+' / '+g.records,num(o.avg_return_pct)+'%'])))+r.notes.map(n=>'<p class="fine">'+esc(n)+'</p>').join('');
 }catch(e){target.textContent=e.message;}finally{button.disabled=false;}
};
for(const id of ['morning-ticker','morning-start','morning-end','morning-stream'])$('#'+id).onchange=()=>{$('#morning-download').href='/api/projects/morning/report?'+morningParams()+'&download=true';};
