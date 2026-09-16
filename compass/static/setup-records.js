// Individual records are fetched only when this view is open. Summary polling
// cannot reset an older page or mix a response from a previous cohort filter.
(()=>{
 let report=null,page=null,loading=false,failed=false,request=0,index=0,cursors=[''];
 let cohort=document.querySelector('#study-cohort').value;
 const previous=document.querySelector('#study-previous');
 const next=document.querySelector('#study-next');
 const newest=document.querySelector('#study-newest');
 const status=document.querySelector('#study-record-status');
 function controls(){
  previous.disabled=loading||index===0;
  next.disabled=loading||!page?.has_more;
  newest.disabled=loading;
 }
 function renderPage(){
  const first=index*100+1,last=index*100+page.records.length;
  status.textContent=(page.records.length?'Records '+num(first,0)+'–'+num(last,0):'0 records')+
   ' of '+num(page.total,0)+' in this cohort. 30-day window ending '+when(page.asof)+
   '. 100 per page. Outcomes may update; use Newest records to refresh the window.';
 $('#study-records').innerHTML=(page.records||[]).map(p=>'<details data-key="trial-'+esc(p.id)+'"><summary>'+esc(p.symbol)+' · '+esc(p.strategy)+' · '+esc(p.side)+' · '+esc(p.outcome||p.status)+' · '+when(p.started)+'</summary><p>Entry '+num(p.entry,4)+' · stop '+num(p.stop,4)+' · target '+num(p.target,4)+' · exit '+num(p.exit,4)+'</p><p>One-unit net result $'+num(p.pnl,2)+' · '+num(p.r_multiple,2)+'R · '+esc(p.exit_reason||p.reason||'Awaiting outcome')+'</p><p class="fine">'+esc(p.basis)+' · samples '+num(p.samples,0)+' · replayed samples '+num(p.replayed_samples||0,0)+' · largest quote gap '+num(p.max_gap_seconds,1)+'s · MFE '+num(p.mfe_r,2)+'R / MAE '+num(p.mae_r,2)+'R</p><p class="fine">Source '+esc(p.source_id)+' · '+esc(p.version)+' · '+esc(p.fill_version||'Legacy fill model')+'</p>'+(p.archive_check?'<p class="fine">Last archive check: '+num(p.archive_check.usable_rows,0)+' usable / '+num(p.archive_check.read_rows,0)+' read; timestamp mismatches '+num(p.archive_check.timestamp_mismatches,0)+'; future '+num(p.archive_check.future_when_recorded,0)+'; stale/invalid '+num(p.archive_check.stale_or_invalid_when_recorded,0)+(p.archive_check.read_rows===0?' · latest archived quote '+when(p.archive_check.latest_archived_ts)+' / stored '+when(p.archive_check.latest_archived_received):'')+'</p>':'')+'<a href="/api/setup-study/record?id='+encodeURIComponent(p.id)+'" target="_blank" rel="noreferrer">Full setup record</a></details>').join('')||empty('No setup records in this cohort','Trials continue independently of portfolio limits.');
 }
 async function loadPage(cursor,pageIndex,asof){
  const token=++request;
  loading=true;failed=false;controls();status.textContent='Loading setup records…';
  const params=new URLSearchParams({cohort,limit:'100'});
  if(cursor)params.set('cursor',cursor);
  else if(asof)params.set('asof',String(asof));
  try{
   const response=await fetch('/api/setup-study/records?'+params.toString(),{cache:'no-store'});
   if(!response.ok)throw new Error('Request failed ('+response.status+')');
   const data=await response.json();
   if(token!==request)return;
   page=data;index=pageIndex;if(index===0)cursors=[''];cursors[index]=cursor;renderPage();
  }catch(error){
   if(token!==request)return;
   failed=true;status.textContent='Could not load setup records. '+error.message+'. Use Newest records to retry.';
  }finally{
   if(token===request){loading=false;controls();}
  }
 }
 window.renderSetupRecords=d=>{
  report=d;
  const selected=document.querySelector('#study-cohort').value;
  if(cohort!==selected){
   request++;cohort=selected;page=null;loading=false;failed=false;index=0;cursors=[''];
   document.querySelector('#study-records').innerHTML='';
  }
  if(document.querySelector('#setup-study').classList.contains('active')&&!page&&!loading&&!failed)
   loadPage('',0,report.at);
  controls();
 };
 previous.onclick=()=>{if(!loading&&index>0)loadPage(cursors[index-1],index-1,page.asof);};
 next.onclick=()=>{if(!loading&&page?.has_more)loadPage(page.next_cursor,index+1,page.asof);};
 newest.onclick=()=>{if(!loading)loadPage('',0);};
 controls();
})();
