// This view owns its paging state; dashboard polling never resets a gap page.
(()=>{
 const root=document.querySelector('#session-gaps');
 if(!root)return;
 const scope=root.querySelector('[name=scope]');
 const asset=root.querySelector('[name=asset]'),day=root.querySelector('[name=session_day]');
 const load=root.querySelector('[data-load]'),next=root.querySelector('[data-next]');
 const status=root.querySelector('[data-status]'),summary=root.querySelector('[data-summary]'),records=root.querySelector('[data-records]');
 let request=0,page=null,shown=0;
 function invalidate(){request++;page=null;shown=0;next.disabled=true;load.disabled=false;summary.textContent='';records.textContent='';status.textContent='Load the selected session.';}
 asset.onchange=()=>{scope.value=asset.value==='option'?'cash':'full';scope.disabled=asset.value==='option';day.value='';invalidate();};
 scope.onchange=()=>{day.value='';invalidate();};
 day.onchange=invalidate;
 async function fetchPage(append){
  const token=++request;
  load.disabled=true;next.disabled=true;status.textContent='Loading session observations…';
  const params=new URLSearchParams({asset:asset.value,scope:scope.value,limit:'100'});
  if(day.value)params.set('session_day',day.value);
  if(append&&page?.next_cursor)params.set('cursor',page.next_cursor);
  try{
   const response=await fetch('/api/session-gaps?'+params,{cache:'no-store'});
   if(!response.ok)throw new Error('Request failed ('+response.status+')');
   const data=await response.json();if(token!==request)return;
   page=data;day.value=data.day;
   if(!append){shown=0;records.textContent='';}
   shown+=data.records.length;
   const s=data.summary;
   summary.textContent='Full cohort: '+s.total+' · '+Object.entries(s.states).map(([k,v])=>k+': '+v).join(' · ')+'. '+data.basis+' Window '+when(data.since)+' to '+when(data.through)+'. Totals reflect current saved states.';
   records.insertAdjacentHTML('beforeend',data.records.map(r=>'<details><summary>'+esc(r.symbol||r.underlying)+' · '+esc(r.strategy)+' · '+when(r.started)+'</summary><p>Record '+esc(r.id)+'</p><p>'+esc(r.contract?.symbol||'')+' · '+esc(r.exit_reason)+'</p><pre>'+esc(JSON.stringify({gap:r.gap_detail,archive:r.archive_check},null,2))+'</pre></details>').join(''));
   status.textContent=shown+' of '+data.total+' unresolved records shown · '+(data.has_more?'More records available.':'All records in this view shown.')+' '+data.note;
  }catch(error){if(token===request)status.textContent='Could not load session observations. '+error.message+' Refresh to retry.';}
  finally{if(token===request){load.disabled=false;next.disabled=!page?.has_more;}}
 }
 load.onclick=()=>fetchPage(false);next.onclick=()=>fetchPage(true);
})();
