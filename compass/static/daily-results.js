(()=>{
 const day=$('#daily-day'),load=$('#daily-load'),status=$('#daily-status'),root=$('#daily-report');
 let request=0;
 const dollars=v=>v===null||v===undefined?'—':'$'+num(v);
 const counts=g=>[num(g.total),num(g.wins),num(g.losses),num(g.breakeven),num(g.open+g.pending),num(g.unresolved),num(g.excluded),num(g.missing_pnl),dollars(g.net_pnl)];
 const headers=['Records','Wins','Losses','Flat','Open / pending','Unresolved','Excluded','Closed missing P&L','Net modeled $'];
 function study(name,s){return '<h3>'+esc(name)+'</h3><p class="fine">'+esc(s.basis)+' '+when(s.since)+' – '+when(s.through)+'</p>'+table(headers,[counts(s.totals)])+'<details><summary>By symbol and setup; unresolved and entry-block reasons</summary>'+table(['Symbol','Setup','Version',...headers],s.groups.map(g=>[esc(g.symbol),esc(g.strategy),esc(g.version),...counts(g)]))+table(['State','Reason','Count'],s.reasons.map(r=>[esc(r.status),esc(r.reason),num(r.count)]))+'</details>';}
 function render(d){
  const p=d.paper,m=d.morning,v=d.verification;
  let h='<p><strong>'+esc(d.operating_policy?.paper_entries_enabled?'Paper benchmark enabled':'Research mode — paper entries and notifications paused')+'</strong></p><p>'+esc(d.note)+'</p>';
  if(d.published_options){const a=d.published_options;
   h+='<h3>Unique option alerts</h3><p>'+esc(a.basis)+'</p>'+table(['Category','Unique ideas','Confirmed entries','Open','Closed','Unresolved','Unpublished','Resolved delivered','Modeled net $'],a.groups.map(x=>[esc(x.category),num(x.ideas),num(x.delivered_entries),num(x.open),num(x.closed),num(x.unresolved),num(x.unpublished),num(x.resolved_delivered),x.net_pnl==null?'—':num(x.net_pnl)]));
   h+=table(['Withheld reason','Count'],(a.withheld||[]).map(x=>[esc(x.reason),num(x.count)]));
  }
  if(d.quote_reliability){const r=d.quote_reliability;
   h+='<h3>Quote reliability</h3><p>'+esc(r.basis)+'</p>'+table(['Study','Collection','Opened','Completed','Open','Unresolved','Gaps','Gap %','Pending gap checks','Recovered processing gaps'],r.cohorts.map(x=>[esc(x.study),esc(x.version),num(x.opened),num(x.completed),num(x.open),num(x.unresolved),num(x.gaps),num(x.gap_pct),num(x.pending_gaps),num(x.recovered_processing_gaps)]));
   h+='<p>Active contracts without stream capacity: '+esc((r.subscriptions.active_missing||[]).join(', ')||'None reported')+' · subscription check '+when(r.subscriptions.at)+'</p>';
   h+='<p>Active contracts awaiting acknowledgement: '+esc(r.subscriptions.acknowledgements_current?(r.subscriptions.active_unacknowledged.join(', ')||'None reported'):'Current acknowledgement evidence unavailable')+'</p>';
   h+=table(['Post-gap collection','Records','Separate samples'],r.followups.map(x=>[esc(x.status),num(x.records),num(x.samples)]));
   h+=table(['Feed','Checked','Measured symbols','Maximum latest receipt-to-storage seconds','Quote queue'],r.latency.map(x=>[esc(x.feed),when(x.health_at),num(x.measured_symbols),num(x.max_latest_socket_to_commit_seconds),num(x.quote_queue)]));
  }
  h+=study('All futures setups — full session',d.futures);
  if(d.zero_dte_setups)h+=study('All scanner 0DTE setups',d.zero_dte_setups);
  if(d.zero_dte_selection)h+='<p>0DTE selection coverage: '+num(d.zero_dte_selection.total)+' setups · '+esc(JSON.stringify(d.zero_dte_selection.states))+'</p>'+table(['Unmeasured selection reason','Count'],Object.entries(d.zero_dte_selection.reasons).map(([reason,n])=>[esc(reason),num(n)]));
  h+=study('Independent options ideas',d.options)+study('Swing option ideas',d.swing_options)+study('All stock setups',d.stock_setups);
  if(d.setup_context)h+='<details><summary>Setup outcomes by session and market condition</summary>'+table(['Symbol','Setup','Version','Session','Condition',...headers],d.setup_context.map(g=>[esc(g.symbol),esc(g.strategy),esc(g.version),esc(g.session),esc(g.market_state),...counts(g)]))+'</details>';
  h+='<details><summary>Historical paper-account benchmark (not setup evaluation)</summary><h3>Paper accounts</h3><p>'+esc(p.basis)+'</p>';
  const totals={};
  for(const r of p.realized){const t=totals[r.asset]??={closed:0,wins:0,losses:0,breakeven:0,missing_pnl:0,net_pnl:0,measured:0,entries:0,open:0};for(const k of ['closed','wins','losses','breakeven','missing_pnl'])t[k]+=r[k];if(r.net_pnl!==null){t.net_pnl+=r.net_pnl;t.measured++;}}
  for(const r of p.entries){const t=totals[r.asset]??={closed:0,wins:0,losses:0,breakeven:0,missing_pnl:0,net_pnl:0,measured:0,entries:0,open:0};t.entries+=r.total;t.open+=r.open;}
  h+=table(['Account','Entries','Closed exits','Wins','Losses','Flat','Missing P&L','Realized paper $'],Object.entries(totals).map(([k,t])=>[esc(k==='option'?'0DTE options':k),num(t.entries),num(t.closed),num(t.wins),num(t.losses),num(t.breakeven),num(t.missing_pnl),dollars(t.measured?t.net_pnl:null)]));
  h+='<details><summary>Paper results by setup and symbol</summary>'+table(['Asset','Setup',...headers],p.realized.map(r=>[esc(r.asset),esc(r.strategy),...counts(r)]))+table(['Asset','Symbol',...headers],p.by_symbol.map(r=>[esc(r.asset),esc(r.symbol),...counts(r)]))+'</details>';
  h+='<details><summary>Skipped paper entries</summary>'+table(['Symbol','Status','Reason','Count','First','Last'],d.skips.map(r=>[esc(r.symbol),esc(r.status),esc(r.reason),num(r.count),when(r.first_at),when(r.last_at)]))+'</details>';
  h+='</details>';
  h+='<h3>Futures after the loss-limit block</h3><p>'+esc(d.post_lock.status.replaceAll('_',' '))+' · '+when(d.post_lock.first_recorded_block_at)+'. '+esc(d.post_lock.basis)+'</p>';
  if(d.post_lock.cohort)h+=study('Post-block research observations',d.post_lock.cohort);

  h+='<h3>Morning Algo — native daily observations</h3><p>'+num(m.signals)+' native signals · '+num(m.original_signals)+' original mirror signals · '+num(m.native_without_original)+' native signals without original mirror records. '+esc(m.source_status.replaceAll('_',' '))+'.</p><p>'+esc(m.basis)+'</p>'+(m.original_gap_action?'<p>'+esc(m.original_gap_action)+' Latest original signal: '+when(m.original_latest_signal_at)+'.</p>':'');
  h+='<p>Direct entry receipts: '+num(m.direct_received_signals)+' · '+esc(m.intake_basis||'Direct-intake evidence unavailable in this report')+'</p>';
  h+='<p>Underlying path coverage: '+esc(JSON.stringify(m.stock_coverage))+'</p>';
  h+=table(['Contract group','Exit minutes','Measured','Positive','Negative','Flat','Unmeasured','Sum of quote measurements $'],m.options.map(r=>[esc(r.variant),num(r.minutes),num(r.measured),num(r.wins),num(r.losses),num(r.breakeven),num(r.unmeasured),dollars(r.measured?r.net_pnl:null)]));
  if(d.smoothers_daily){const s=d.smoothers_daily;
   h+='<h3>Daily Smoothers comparison — quiet research</h3><p>'+esc(s.state)+' · '+esc(s.note||'Awaiting study activation')+'</p>';
   if(s.activation)h+='<p>'+num(s.activation.configs.length)+' tickers · first full session '+esc(s.activation.sessions[0].day)+' · final entry session '+esc(s.activation.sessions.at(-1).day)+' · no additional alerts</p>';
   h+=table(['Comparator','Matched ticker/direction/days','Smoothers earlier','Scanner earlier','Mean Smoothers lead (seconds)'],(s.overlap||[]).map(x=>[esc(x.family),num(x.matched),num(x.smoothers_earlier),num(x.scanner_earlier),num(x.mean_lead_seconds)]));
   h+='<details><summary>Daily study outcomes and capture coverage</summary>'+table(['Family','Overlap group','Confirmation timing','Weekday','Horizon','Candidates','Measured','Pending','Unavailable','Mean directional stock %'],(s.groups||[]).map(x=>[esc(x.family),esc(x.group),esc(x.confirmation),esc(x.weekday),esc(x.horizon),num(x.records),num(x.measured),num(x.pending),num(x.unavailable),num(x.mean_directional_pct)]));
   h+=table(['Session','Daily formula census','Processed','Data errors / missed'],(s.days||[]).filter(x=>x.day).map(x=>[esc(x.day),esc(x.state),num(x.index),(x.rows||[]).filter(r=>r.status==='unavailable').length+(x.unprocessed||[]).length]))+'</details>';
  }
  h+='<h3>Smoothers — week of '+esc(d.smoothers.week)+'</h3><p>'+esc(d.smoothers.basis)+'</p>'+table(['Status','Count'],Object.entries(d.smoothers.states).map(([k,n])=>[esc(k),num(n)]));
  h+='<h3>Scheduled SPY option observations</h3><p>'+esc(d.spy_options.basis)+'</p>'+table(headers,[counts(d.spy_options.totals)]);
  for(const r of d.research)h+='<h3>'+esc(r.program)+'</h3><p>'+esc(r.basis)+' Total '+num(r.total)+' · '+esc(JSON.stringify(r.states))+'</p>'+table(['Horizon','Checkpoint state','Count'],r.checkpoints.map(x=>[esc(x.horizon),esc(x.status),num(x.count)]));
  h+='<h3>SPY and Ask Compass verification</h3><p>'+esc(v.basis)+'</p><p>SPY daily readiness: '+esc(v.spy_daily.status)+' · '+num((v.spy_daily.usable_days||[]).length)+'/15 daily sessions · checked '+when(v.spy_daily.checked_at)+'.</p><p>Recorded SPY checks: '+num(v.spy_checks)+' · '+esc(JSON.stringify(v.spy_decisions))+'</p><p>Drawing messages queued: '+num(v.drawing_messages)+' · unchanged drawings suppressed: '+num(v.drawing_suppressions)+' · '+esc(JSON.stringify(v.drawing_reasons))+'</p>';
  h+=table(['SPY phase','Decision','Data blocks','Observation eligibility','Checked'],v.spy_details.map(r=>[esc(r.phase),esc(r.decision),esc(r.data_blocks.join('; ')),esc(r.eligibility_reason),when(r.at)]));
  h+=v.ask_options.length?table(['Symbol','Horizon','Status','Candidates','Fresh quotes','Returned expirations','Checked'],v.ask_options.map(r=>[esc(r.symbol),esc(r.horizon),esc(r.status),num(r.candidate_count),num(r.fresh_quotes),esc(r.returned_expirations.join(', ')),when(r.at)])):'<p>No recorded Ask Compass lookup verification for this date yet. Ask a question for the requested horizon to collect evidence.</p>';
  root.innerHTML=h;
 }
 day.onchange=()=>{request++;load.disabled=false;root.textContent='';status.textContent='Load the selected date.';$('#daily-download').href='/api/daily-results?download=true&session_day='+encodeURIComponent(day.value);};
 load.onclick=async()=>{const token=++request;load.disabled=true;status.textContent='Loading complete daily results…';try{const params=new URLSearchParams();if(day.value)params.set('session_day',day.value);const r=await fetch('/api/daily-results?'+params,{cache:'no-store'});if(!r.ok)throw Error('Request failed ('+r.status+')');const d=await r.json();if(token!==request)return;day.value=d.day;render(d);status.textContent='Trading day '+d.day+' · as of '+when(d.asof)+' · complete aggregate counts';$('#daily-download').href='/api/daily-results?download=true&session_day='+encodeURIComponent(d.day);}catch(e){if(token===request){root.textContent='';status.textContent=e.message;}}finally{if(token===request)load.disabled=false;}};
})();
