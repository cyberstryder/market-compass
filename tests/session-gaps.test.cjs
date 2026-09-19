const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const elements=Object.fromEntries(['[name=scope]','[name=asset]','[name=session_day]','[data-load]','[data-next]','[data-status]','[data-summary]','[data-records]'].map(k=>[k,{value:k==='[name=asset]'?'future':k==='[name=scope]'?'full':'',textContent:'',html:'',insertAdjacentHTML(_,html){this.html+=html;}}]));
const root={querySelector:k=>elements[k]},calls=[];let response;
vm.runInNewContext(fs.readFileSync('compass/static/session-gaps.js','utf8'),{
 document:{querySelector:()=>root},URLSearchParams,
 esc:v=>String(v??'').replaceAll('<','&lt;'),when:String,
 fetch:async url=>{calls.push(url);return {ok:true,json:async()=>response};}
});
const page={day:'2026-09-18',summary:{total:2105,states:{closed:2000,unresolved:105}},total:105,basis:'Cash cohort',since:1,through:2,note:'Pinned',records:[{id:'one',symbol:'<script>',started:1,gap_detail:{reason:'gap'}}],has_more:true,next_cursor:'next'};
(async()=>{
 response=page;await elements['[data-load]'].onclick();
 assert.match(calls[0],/scope=full/);
 assert.match(elements['[data-summary]'].textContent,/2105/);
 assert.match(elements['[data-records]'].html,/&lt;script>/);
 assert.equal(elements['[data-next]'].disabled,false);
 response={...page,records:[{id:'two',symbol:'MNQ',started:1}],has_more:false,next_cursor:''};
 await elements['[data-next]'].onclick();
 assert.match(calls[1],/cursor=next/);assert.match(calls[1],/session_day=2026-09-18/);
 assert.equal(elements['[data-next]'].disabled,true);
 assert.match(elements['[data-status]'].textContent,/All records in this view shown/);
 elements['[name=scope]','[name=asset]'].value='option';elements['[name=scope]','[name=asset]'].onchange();
 assert.equal(elements['[data-next]'].disabled,true);
 assert.equal(elements['[data-summary]'].textContent,'');
 response={...page,records:[],has_more:false};await elements['[data-load]'].onclick();
 assert.equal(elements['[name=scope]'].disabled,true);
 assert.match(calls[2],/scope=cash/);
 assert.doesNotMatch(calls[2],/session_day=/);
 assert.match(calls[2],/asset=option/);assert.doesNotMatch(calls[2],/cursor=/);
 elements['[name=asset]'].value='future';elements['[name=asset]'].onchange();
 assert.equal(elements['[name=scope]'].value,'full');
 assert.equal(elements['[name=scope]'].disabled,false);
 elements['[name=scope]'].value='cash';elements['[name=scope]'].onchange();
 assert.equal(elements['[name=session_day]'].value,'');
 assert.equal(elements['[data-next]'].disabled,true);
 console.log('Session gap UI paging, escaping, and filter reset passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
