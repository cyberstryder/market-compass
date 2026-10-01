/* Ticker read: assemble a mechanical directional read for one ticker from
   Compass's own data. Read-only GET to /api/ticker-read. Evidence only. */
(()=>{
 const DIR_CLS={bullish:'good',bearish:'bad',neutral:''};
 const dirTag=d=>'<span class="tag '+(DIR_CLS[d]||'')+'">'+esc(d||'neutral')+'</span>';
 const LEAN_TXT={strongly_bullish:'Strongly bullish',leaning_bullish:'Leaning bullish',
  mixed:'Mixed',leaning_bearish:'Leaning bearish',strongly_bearish:'Strongly bearish'};

 async function j(url){const r=await fetch(url);const b=await r.json().catch(()=>({}));if(!r.ok)throw new Error(b.detail||('HTTP '+r.status));return b;}

 function money(x){return x==null?'—':'$'+num(x);}

 function renderResult(r){
  const lean=r.lean||{score:0,label:'mixed'}, sess=r.session||{}, st=r.structure||{},
        apex=r.apex||{}, gamma=r.gamma||{}, tape=r.tape||{}, gap=r.gap||{};
  const stats=
   '<div class="stat"><div class="label">Spot</div><div class="value">'+money(r.spot)+'</div></div>'
   +'<div class="stat"><div class="label">Session</div><div class="value" style="font-size:18px">'+esc(sess.date||'—')+'</div></div>'
   +'<div class="stat"><div class="label">Open / High / Low</div><div class="value" style="font-size:18px">'+money(sess.open)+' / '+money(sess.high)+' / '+money(sess.low)+'</div></div>'
   +'<div class="stat"><div class="label">Change</div><div class="value">'+(sess.change_pct==null?'—':(sess.change_pct>=0?'+':'')+num(sess.change_pct,2)+'%')+'</div></div>'
   +'<div class="stat"><div class="label">VWAP</div><div class="value">'+money(st.vwap)+(st.vs_vwap?' <span class="fine">'+esc(st.vs_vwap)+'</span>':'')+'</div></div>'
   +'<div class="stat"><div class="label">Trend</div><div class="value" style="font-size:18px">'+esc((st.trend||'—').replaceAll('_',' '))+'</div></div>';
  let detail='<div class="stats">';
  if(apex.nearest_resistance||apex.nearest_support){
   detail+='<div class="stat"><div class="label">Resistance</div><div class="value">'+(apex.nearest_resistance?money(apex.nearest_resistance.magnet):'—')+'</div></div>'
    +'<div class="stat"><div class="label">Support</div><div class="value">'+(apex.nearest_support?money(apex.nearest_support.magnet):'—')+'</div></div>';
  }
  if(gamma.flip!=null){
   detail+='<div class="stat"><div class="label">Gamma flip</div><div class="value">'+money(gamma.flip)+' <span class="fine">'+esc(gamma.regime||'')+'</span></div></div>';
  }
  if(tape.has_data){
   detail+='<div class="stat"><div class="label">Tape trials</div><div class="value" style="font-size:18px">'+tape.long_trials+' long / '+tape.short_trials+' short</div></div>';
  }
  if(gap.on_board){
   detail+='<div class="stat"><div class="label">Gap</div><div class="value">'+(gap.gap_pct==null?'—':num(gap.gap_pct*100,2)+'%')+(gap.qualified?' <span class="fine">qualified</span>':'')+'</div></div>';
  }
  detail+='</div>';
  $('#tickerread-result').innerHTML=
   '<article class="panel"><div class="panel-head"><h2>'+esc(r.symbol)+' · '+(LEAN_TXT[lean.label]||lean.label)+'</h2><span class="pill">lean '+(lean.score>=0?'+':'')+lean.score+'</span></div>'
   +'<div class="stats">'+stats+'</div>'+detail
   +'<p class="fine">Read '+when(r.asof)+'. '+esc(r.note||'')+'</p></article>'
   +'<article class="panel"><h2>Factors</h2>'+table(['Factor','Direction','Note'],(r.factors||[]).map(f=>[esc(f.factor.replaceAll('_',' ')),dirTag(f.direction),esc(f.note||'')]))+'</article>';
 }

 async function runRead(){
  const btn=$('#tickerread-run');btn.disabled=true;
  const sym=$('#tickerread-symbol').value.trim().toUpperCase();
  $('#tickerread-result').innerHTML='<p class="muted">Reading '+esc(sym||'…')+'…</p>';
  try{
   if(!sym)throw new Error('Enter a ticker symbol.');
   const r=await j('/api/ticker-read?symbol='+encodeURIComponent(sym));
   if(r.error)throw new Error(r.error);
   renderResult(r);
  }catch(e){$('#tickerread-result').innerHTML=empty('Read failed',e.message);}
  btn.disabled=false;
 }

 document.addEventListener('DOMContentLoaded',()=>{
  if(!$('#ticker-read'))return;
  $('#tickerread-run').onclick=runRead;
  $('#tickerread-symbol').addEventListener('keydown',e=>{if(e.key==='Enter')runRead();});
 });
})();
