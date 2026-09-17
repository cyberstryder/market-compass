/* Read-only projection of the authenticated research inventory. */
let researchAdminData=null;
function researchMetricValue(value){
 return value===null||value===undefined?'Not measured':typeof value==='number'?num(value,Number.isInteger(value)?0:1):esc(value);
}
function renderResearchAdmin(data){
 if(!data)return;
 researchAdminData=data;
 const stats=[['Workstreams',data.counts.streams,'Separate programs and studies'],
  ['Needs attention',data.counts.attention,'Coverage gaps or unfinished work'],
  ['Closed / paired results',data.counts.with_measured_results,'Studies with explicit completed counts; not proof of an edge'],
  ['Discord queue',data.counts.discord_pending,'Waiting for delivery confirmation']];
 $('#research-admin-summary').innerHTML=stats.map(([label,value,note])=>'<div class="stat"><div class="label">'+esc(label)+'</div><div class="value">'+researchMetricValue(value)+'</div><div class="fine">'+esc(note)+'</div></div>').join('');
 const audit=data.swing_audit||{};
 const tm=data.flow_research||{},inventory=tm.inventory||{};
 $('#research-admin-coverage').innerHTML='<p><strong>New TM records receive a frozen research assessment.</strong> '+researchMetricValue(tm.prospective_assessments)+' prospective assessments; '+researchMetricValue(tm.records_rated_by_compass)+' complete Compass scores; '+researchMetricValue(tm.incomplete_assessments)+' incomplete assessments. Older inventory stays separate. <a href="/#tm-study">Open TM scoring study →</a></p><p class="fine">Lifetime received '+researchMetricValue(inventory.received_records)+' · registered '+researchMetricValue(inventory.registered_records)+' · awaiting registration '+researchMetricValue(inventory.unregistered_records)+'. Score and outcome counts use the complete 30-day receipt window. Qualifying Unusual Options alerts continue under their existing rules.</p>'+
  (audit.technical_candidates!=null?'<p class="fine">Swing audit: '+num(audit.technical_candidates,0)+' technical candidates; '+num(audit.without_flow_confirmation,0)+' without flow confirmation. Since '+when(audit.since)+', checked '+when(audit.checked_at)+(audit.truncated?' · reporting limit reached':'')+'. These are candidate counts, not completed trades.</p>':'<p class="fine">Swing candidate audit has not reported yet.</p>');
 const store=data.storage||{};
 $('#research-admin-storage').innerHTML='<div class="research-storage"><p><strong>Primary store</strong><br>'+esc(store.database==='postgresql'?'PostgreSQL':store.database)+' · '+tag(store.status)+'</p><p><strong>Database size</strong><br>'+(store.database_bytes==null?'Not measured':num(store.database_bytes/1e9,2)+' GB')+'</p><p><strong>Archive scheduler</strong><br>'+tag(store.archive_scheduler)+'</p><p><strong>Storage check</strong><br>'+when(store.checked_at)+'</p></div><p class="fine">Stored evidence includes source records, native observations, research ledgers and delivery receipts. Each card keeps its own reporting window. Recent-row views are not lifetime totals, and an archive scheduler heartbeat does not establish a verified backup or restoration.</p>';
 $('#research-admin-notes').innerHTML='<ul class="research-notes">'+data.notes.map(n=>'<li>'+esc(n)+'</li>').join('')+'</ul><p class="fine">Status captured '+when(data.asof)+' · '+esc(data.version)+'</p>';
 renderResearchAdminCards();
}
function renderResearchAdminCards(){
 if(!researchAdminData)return;
 const term=$('#research-admin-search').value.toLowerCase().trim();
 const filter=$('#research-admin-filter').value;
 const rows=researchAdminData.streams.filter(r=>(!term||[r.name,r.family,r.collection,r.assessment,r.outcomes].join(' ').toLowerCase().includes(term))&&
  (filter==='all'||(filter==='attention'?r.needs_attention:(r.measured||0)>0)));
 $('#research-admin-count').textContent=rows.length+' of '+researchAdminData.streams.length+' workstreams';
 $('#research-admin-streams').innerHTML=rows.map(r=>{
  const a=r.alerts||{};
  const route=a.route?'<p class="fine">'+esc(a.channel?'#'+a.channel:a.route.replaceAll('_',' '))+' · '+esc(a.destination||'destination unobserved')+' · '+tag(a.health)+' · queue '+researchMetricValue(a.pending)+(a.last_confirmed_at?' · last confirmed '+when(a.last_confirmed_at):' · no confirmed delivery recorded')+'</p>':'';
  return '<article class="panel research-card'+(r.needs_attention?' research-attention':'')+'"><div class="panel-head"><div><p class="eyebrow">'+esc(r.family)+'</p><h2>'+esc(r.name)+'</h2></div>'+tag(r.stage)+'</div>'+
   '<div class="research-card-metrics">'+r.metrics.map(m=>'<div><strong>'+researchMetricValue(m.value)+'</strong><span>'+esc(m.label)+'</span></div>').join('')+'</div>'+
   '<p class="fine research-window">'+esc(r.window)+'</p><dl class="research-stages"><dt>Collection</dt><dd>'+esc(r.collection)+'</dd><dt>Assessment</dt><dd>'+esc(r.assessment)+'</dd><dt>Outcomes</dt><dd>'+esc(r.outcomes)+'</dd><dt>Alerts</dt><dd>'+esc(a.policy)+'<span class="research-owner">Owner: '+esc(a.owner)+'</span></dd></dl>'+route+
   (r.issues.length?'<div class="research-issues"><strong>Needs attention</strong><ul>'+r.issues.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul></div>':'')+
   '<p class="research-next"><strong>Next check</strong> '+esc(r.next_step)+'</p><div class="research-card-foot"><span class="fine">Checked '+when(r.checked_at)+(r.version?' · '+esc(r.version):'')+'</span>'+
   (r.id==='spy'?'<a href="/api/spy-morning-brief" target="_blank" rel="noopener">Open plan details ↗</a>':'<a href="'+esc(r.href)+'" data-research-link>Open details →</a>')+'</div></article>';
 }).join('')||empty('No matching research','Change the program name or filter.');
}
document.addEventListener('DOMContentLoaded',()=>{
 $('#research-admin-search').addEventListener('input',renderResearchAdminCards);
 $('#research-admin-filter').addEventListener('change',renderResearchAdminCards);
 $('#research-admin-streams').addEventListener('click',event=>{
  const link=event.target.closest('a[data-research-link]');
  if(!link||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
  event.preventDefault();selectTab(new URL(link.href).hash.slice(1));window.scrollTo(0,0);
 });
});
