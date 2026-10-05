/* Market Compass public results page — plain-English layout, real data.
   Reads /api/simple (scoreboards + drill-downs) and /api/simple/calendar. */
(function(){
'use strict';

var TYPES = [
  { id:'all',       label:'All trackers' },
  { id:'smoothers', label:'Smoothers',   api:'smoothers' },
  { id:'futures',   label:'Futures',     api:'futures' },
  { id:'zeroDte',   label:'0DTE',        api:'0dte' },
  { id:'flowPulse', label:'Flow Pulse',  api:'flow_pulse' },
  { id:'flash',     label:'Flash',       api:'flash' }
];
var TYPE_LABEL = { smoothers:'Smoothers', futures:'Futures', zeroDte:'0DTE', flowPulse:'Flow Pulse', flash:'Flash' };
var DOLLAR = ['smoothers','futures','flowPulse']; /* mockup ids that carry P&L */
var COUNT  = ['flash','zeroDte'];

var activeFilter = 'all';
var selectedDay = null;
var calYear = null, calMonth = null; /* 1-12 */
var calDays = {};   /* 'YYYY-MM-DD' -> day record */
var SUMMARY = null;

function esc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function money(n){
  if(n === null || n === undefined || isNaN(n)) return '—';
  var v = Math.round(n);
  if(v === 0) return '$0';
  return (v < 0 ? '-$' : '+$') + Math.abs(v).toLocaleString('en-US');
}
function signCls(n){ return (n === null || n === undefined || isNaN(n)) ? '' : (n > 0 ? 'pos' : (n < 0 ? 'neg' : '')); }
function pct(x){ return (x === null || x === undefined) ? '—' : (Math.round(x*10)/10) + '%'; }
function num(x){ return (x === null || x === undefined) ? '—' : Number(x).toLocaleString('en-US'); }

function todayKey(){
  var d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
}
function fmtDay(key){
  var d = new Date(key + 'T12:00:00');
  return d.toLocaleDateString('en-US', { weekday:'short', month:'short', day:'numeric' });
}
function fmtDayLong(key){
  var d = new Date(key + 'T12:00:00');
  return d.toLocaleDateString('en-US', { weekday:'long', month:'long', day:'numeric', year:'numeric' });
}

function monthDays(){
  var prefix = monthStr() + '-';
  return Object.keys(calDays).filter(function(k){ return k.indexOf(prefix) === 0; });
}

/* ---------------- today strip ---------------- */
function dayPnl(key){
  var d = calDays[key]; if(!d) return 0;
  var t = 0;
  DOLLAR.forEach(function(id){ var k = TYPES.filter(function(x){return x.id===id;})[0].api; if(d[k]) t += (d[k].pnl || 0); });
  return t;
}
function daySetups(key){
  var d = calDays[key]; if(!d) return 0;
  return (d.flash ? d.flash.count : 0) + (d['0dte'] ? d['0dte'].count : 0);
}
function renderToday(){
  var t = todayKey();
  var label = fmtDay(t);
  document.getElementById('t0l').textContent = 'Today · ' + label;
  var pv = dayPnl(t), ps = daySetups(t);
  var v0 = document.getElementById('t0v');
  v0.textContent = money(pv); v0.className = signCls(pv);
  document.getElementById('t0s').textContent = ps + (ps === 1 ? ' setup logged' : ' setups logged') + ' today';

  /* week to date (Mon..today) */
  var now = new Date(); var dow = (now.getDay()+6)%7; /* Mon=0 */
  var monday = new Date(now); monday.setDate(now.getDate()-dow);
  var wv = 0, wdays = 0;
  for(var i=0;i<=dow;i++){
    var dd = new Date(monday); dd.setDate(monday.getDate()+i);
    var k = dd.getFullYear()+'-'+String(dd.getMonth()+1).padStart(2,'0')+'-'+String(dd.getDate()).padStart(2,'0');
    if(calDays[k]){ wv += dayPnl(k); wdays++; }
  }
  var v1 = document.getElementById('t1v');
  v1.textContent = money(wv); v1.className = signCls(wv);
  document.getElementById('t1s').textContent = wdays + (wdays===1?' trading day':' trading days') + ' so far';

  /* month (displayed month only — calDays also holds the previous month for the grid) */
  var mv = 0, mdays = 0;
  monthDays().forEach(function(k){ mv += dayPnl(k); if(calDays[k]) mdays++; });
  var months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  document.getElementById('t2l').textContent = months[calMonth-1] + ' so far';
  var v2 = document.getElementById('t2v');
  v2.textContent = money(mv); v2.className = signCls(mv);
  document.getElementById('t2s').textContent = mdays + (mdays===1?' trading day':' trading days');

  /* all-time win rate across the dollar trackers */
  var wins = 0, n = 0;
  ['smoothers','futures'].forEach(function(api){
    var b = SUMMARY && SUMMARY.types && SUMMARY.types[api];
    if(b && b.stats){ wins += (b.stats.wins||0); n += (b.stats.tracked||0); }
  });
  var v3 = document.getElementById('t3v');
  v3.textContent = n ? Math.round(100*wins/n) + '%' : '—';
  document.getElementById('t3s').textContent = num(wins) + ' wins · ' + num(n-wins) + ' losses';
}

/* ---------------- tracker cards ---------------- */
function renderCards(){
  function set(id, html){ document.getElementById('res-'+id).innerHTML = html; }
  var T = SUMMARY.types;
  var sm = T.smoothers.stats, fu = T.futures.stats, zd = T['0dte'].stats, fl2 = T.flow_pulse.stats, fl = T.flash.stats;
  set('smoothers', 'All time <b class="' + signCls(sm.pnl) + '">' + money(sm.pnl) + '</b>');
  set('futures',   'All time <b class="' + signCls(fu.pnl) + '">' + money(fu.pnl) + '</b>');
  set('0dte',      '<b>' + num(zd.tracked) + '</b> plans &amp; alerts logged');
  if(fl2.tracked > 0){
    set('flow', 'All time <b class="' + signCls(fl2.pnl) + '">' + money(fl2.pnl) + '</b>');
  } else {
    set('flow', 'No tracks closed yet');
  }
  if((fl.resolved||0) > 0){
    set('flash', 'Clean targets <b>' + num(fl.wins) + ' of ' + num(fl.resolved) + ' · ' + pct(fl.win_rate) + '</b>');
  } else {
    set('flash', '<b>' + num(fl.tracked) + '</b> setups logged');
  }
  /* digest: next weekday ~4pm CT */
  var d = new Date(); var day = d.getDay();
  document.getElementById('digestNext').textContent = (day === 0 || day === 6) ? 'Mon ~4:00pm CT' : 'Today ~4:00pm CT';
}

/* ---------------- drill-down ---------------- */
var DRILL = {
  smoothers: { title:'Smoothers, ticker by ticker',
    sub:'One card per ticker the Smoothers book has touched. Real paper P&L per name — bought at the ask, sold at the bid, fees in.',
    pill:'Live', pillCls:'live', hit:false },
  futures: { title:'Futures, method by method',
    sub:'Every pattern detector, scored separately. Paper trades on micro futures — everything forced flat by 3:45pm CT.',
    pill:'Paper / proving', pillCls:'test', hit:false },
  zeroDte: { title:'0DTE, name by name',
    sub:'The morning SPY plan plus every scanner alert. 0DTE outcomes are not tracked yet — this is the activity log.',
    pill:'Scanner only', pillCls:'test', hit:false, counts:true },
  flowPulse: { title:'Flow Pulse, by direction',
    sub:'Split by which way the big money bet. Every pulse that fires opens a two-week paper track — win or lose, it stays on the record.',
    pill:'Paper tracking', pillCls:'test', hit:false },
  flash: { title:'Flash Agentic, pattern by pattern',
    sub:'Setups grouped by pattern type, scored like the pick checker: target touched before invalidation wins, over five sessions.',
    pill:'Evidence only', pillCls:'test', hit:true }
};

function tagFor(name, id){
  if(id === 'smoothers') return name.length <= 6 ? name : name.slice(0,6);
  var words = String(name).split(/[^A-Za-z0-9]+/).filter(Boolean);
  var tag = words.slice(0,2).map(function(w){ return w[0]; }).join('').toUpperCase();
  return tag || '?';
}
var DOT_CLASS = { smoothers:'d-smooth', futures:'d-futures', zeroDte:'d-0dte', flowPulse:'d-flow', flash:'d-flash' };

function drillCard(it, id, cfg){
  var st = it.stats || {};
  var n = st.tracked || 0, w = st.wins || 0;
  var res;
  if(cfg.hit){
    var r = st.resolved || 0;
    if(r > 0){
      res = '<span><b>' + num(w) + ' of ' + num(r) + ' targets · ' + pct(st.win_rate) + '</b></span>';
    } else if(n > 0){
      res = '<span><b>' + num(n) + '</b> logged · none resolved yet</span>';
    } else {
      res = '<span>No setups yet</span>';
    }
    res += '<span class="pill ' + cfg.pillCls + '">' + cfg.pill + '</span>';
  } else if(cfg.counts){
    res = n > 0
      ? '<span><b>' + num(n) + '</b> logged</span><span class="pill ' + cfg.pillCls + '">' + cfg.pill + '</span>'
      : '<span>No activity yet</span><span class="pill">Watching</span>';
  } else if(n > 0){
    var rate = pct(st.win_rate);
    res = '<span><b class="' + signCls(st.pnl) + '">' + money(st.pnl) + '</b> · ' + num(n) + ' trades · ' + rate + ' wins</span>'
      + '<span class="pill ' + cfg.pillCls + '">' + cfg.pill + '</span>';
  } else {
    res = '<span>No trades yet</span><span class="pill">Watching</span>';
  }
  return '<article class="card">'
    + '<div class="card-top"><span class="dot tag ' + (DOT_CLASS[id] || 'd-flash') + '">' + esc(tagFor(it.name, id)) + '</span>'
    + '<div><h3>' + esc(it.name) + '</h3></div></div>'
    + '<p>' + esc(it.watches || '') + '</p>'
    + '<div class="result">' + res + '</div></article>';
}

function selectDrill(id, scroll){
  var cfg = DRILL[id];
  if(!cfg || !SUMMARY) return;
  var api = TYPES.filter(function(x){ return x.id === id; })[0].api;
  var block = SUMMARY.types[api];
  document.getElementById('drill-h').textContent = cfg.title;
  document.getElementById('drillSub').textContent = cfg.sub;
  var st = block.stats, n = st.tracked || 0, w = st.wins || 0;
  var sum;
  if(cfg.hit){
    var r = st.resolved || 0;
    sum = ['<b>' + num(n) + '</b> setups logged',
           '<b>' + num(w) + '</b> clean targets' + (r ? ' · ' + pct(st.win_rate) : ''),
           'No dollars attached — evidence only'];
  } else if(cfg.counts){
    sum = ['<b>' + num(n) + '</b> logged', 'Outcomes not tracked yet'];
  } else {
    sum = ['<b>' + num(n) + '</b> trades',
           '<b>' + num(w) + '</b> wins · ' + pct(st.win_rate),
           '<b>' + money(st.pnl) + '</b> net'];
    if(st.open) sum.push('<b>' + num(st.open) + '</b> still open');
  }
  document.getElementById('drillSum').innerHTML = sum.map(function(s){ return '<span>' + s + '</span>'; }).join('');
  var items = (block.drill || []).slice().sort(function(a,b){ return (b.stats.pnl||0) - (a.stats.pnl||0); });
  document.getElementById('drillGrid').innerHTML = items.map(function(it){ return drillCard(it, id, cfg); }).join('')
    || '<p class="muted-note">Nothing logged yet.</p>';
  var cards = document.querySelectorAll('.card.pick');
  for(var i=0;i<cards.length;i++){
    var on = cards[i].getAttribute('data-drill') === id;
    cards[i].classList.toggle('selected', on);
    cards[i].setAttribute('aria-pressed', on ? 'true' : 'false');
  }
  if(scroll){
    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    document.getElementById('drillSection').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block:'start' });
  }
}

/* ---------------- scoreboard ---------------- */
var BOARD = [
  { id:'smoothers', api:'smoothers',  status:'Live',           pill:'live' },
  { id:'futures',   api:'futures',    status:'Paper / proving',pill:'test' },
  { id:'zeroDte',   api:'0dte',       status:'Scanner only',   pill:'test', counts:true },
  { id:'flowPulse', api:'flow_pulse', status:'Paper tracking', pill:'test' },
  { id:'flash',     api:'flash',      status:'Evidence only',  pill:'test', hit:true }
];

function renderBoard(){
  var html = '';
  BOARD.forEach(function(r){
    var st = SUMMARY.types[r.api].stats;
    var n = st.tracked || 0, w = st.wins || 0;
    var rate, rateNum;
    if(r.hit){
      var res = st.resolved || 0;
      rateNum = res ? w/res : 0;
      rate = res ? Math.round(100*w/res) + '%' : '—';
    } else {
      rateNum = n ? w/n : 0;
      rate = n ? Math.round(100*w/n) + '%' : '—';
    }
    var result;
    if(r.hit){
      var rr = st.resolved || 0;
      result = rr > 0 ? '<b>' + num(w) + '/' + num(rr) + ' targets · ' + Math.round(100*w/rr) + '%</b>'
                      : '<b>' + num(n) + '</b> logged';
    } else if(r.counts){
      result = '<b>' + num(n) + '</b> logged';
    } else {
      result = '<b class="' + signCls(st.pnl) + '">' + money(st.pnl) + '</b>';
    }
    var barPct = Math.round(rateNum*100);
    html += '<tr data-drill="' + r.id + '" title="Drill into ' + TYPE_LABEL[r.id] + '">'
      + '<td><span class="tname"><i class="t-' + r.id + '"></i>' + TYPE_LABEL[r.id] + '</span></td>'
      + '<td class="num" data-l="Trades">' + num(n) + '</td>'
      + '<td class="num" data-l="Win rate">' + rate + '</td>'
      + '<td data-l="Record"><div class="bar"><i class="b-' + r.id + '" data-w="' + barPct + '"></i></div>'
      + '<span class="rec-note">' + num(w) + 'W · ' + num(n-w) + 'L</span></td>'
      + '<td class="num" data-l="Result">' + result + '</td>'
      + '<td data-l="Status"><span class="pill ' + r.pill + '">' + r.status + '</span></td>'
      + '</tr>';
  });
  var body = document.getElementById('boardBody');
  body.innerHTML = html;
  /* bar widths via CSSOM (CSP: no inline styles) */
  body.querySelectorAll('.bar i').forEach(function(el){ el.style.width = el.getAttribute('data-w') + '%'; });
}

/* ---------------- calendar ---------------- */
function apiKey(id){ return TYPES.filter(function(x){ return x.id === id; })[0].api; }

function dayTotal(d, filter){
  if(!d) return 0;
  if(filter === 'flash' || filter === 'zeroDte') return 0;
  if(filter === 'all') return DOLLAR.reduce(function(t,id){ var k=apiKey(id); return t + (d[k] ? d[k].pnl||0 : 0); }, 0);
  var k = apiKey(filter);
  return d[k] ? d[k].pnl||0 : 0;
}
function dayCount(d, filter){
  if(!d) return 0;
  var k = apiKey(filter);
  return d[k] ? d[k].count||0 : 0;
}
function hasActivity(d, id){
  if(!d) return false;
  var k = apiKey(id);
  if(!d[k]) return false;
  return (d[k].pnl && d[k].pnl !== 0) || (d[k].count && d[k].count > 0);
}

function renderFilters(){
  var html = '';
  TYPES.forEach(function(t){
    var dot = t.id === 'all' ? '' : '<i class="f-' + t.id + '"></i>';
    html += '<button type="button" class="fbtn" data-f="' + t.id + '" aria-pressed="' + (t.id === activeFilter) + '">' + dot + t.label + '</button>';
  });
  document.getElementById('filters').innerHTML = html;
}

function monthName(){ return ['January','February','March','April','May','June','July','August','September','October','November','December'][calMonth-1]; }

function renderCalendar(){
  var grid = document.getElementById('calGrid');
  var html = '';
  var first = new Date(calYear, calMonth-1, 1);
  var lead = (first.getDay()+6)%7; /* Mon-first */
  var daysInMonth = new Date(calYear, calMonth, 0).getDate();
  var prevDays = new Date(calYear, calMonth-1, 0).getDate();
  for(var l=lead; l>0; l--){
    var pk = pad(calYear, calMonth-1 < 1 ? 12 : calMonth-1) + '-' + String(prevDays-l+1).padStart(2,'0');
    if(calMonth === 1) pk = (calYear-1) + '-' + pk.slice(5);
    html += dayCell(pk, prevDays-l+1, true);
  }
  for(var d=1; d<=daysInMonth; d++){
    var key = calYear + '-' + String(calMonth).padStart(2,'0') + '-' + String(d).padStart(2,'0');
    html += dayCell(key, d, false);
  }
  var trail = (7 - ((lead + daysInMonth) % 7)) % 7;
  for(var t2=1; t2<=trail; t2++){
    html += '<div class="day weekend" aria-hidden="true"><span class="d">' + t2 + '</span></div>';
  }
  grid.innerHTML = html;

  /* summary */
  var label = activeFilter === 'all' ? 'All trackers' : TYPE_LABEL[activeFilter];
  var win = monthName() + ' ' + calYear;
  var sumHtml;
  if(activeFilter === 'flash'){
    var fh = 0;
    monthDays().forEach(function(k){ fh += dayCount(calDays[k], 'flash'); });
    sumHtml = '<span>' + win + ' · ' + label + ': <b>' + num(fh) + '</b> setups logged</span>'
      + '<span>Scored on target-before-invalidation — no dollars attached</span>';
  } else if(activeFilter === 'zeroDte'){
    var zh = 0;
    monthDays().forEach(function(k){ zh += dayCount(calDays[k], 'zeroDte'); });
    sumHtml = '<span>' + win + ' · ' + label + ': <b>' + num(zh) + '</b> plans &amp; alerts</span>'
      + '<span>Outcomes not tracked yet — counts only</span>';
  } else {
    var total = 0, best = null, worst = null, days = 0;
    monthDays().forEach(function(k){
      var v = dayTotal(calDays[k], activeFilter);
      total += v; days++;
      if(best === null || v > best.v) best = { k:k, v:v };
      if(worst === null || v < worst.v) worst = { k:k, v:v };
    });
    sumHtml = '<span>' + win + ' · ' + label + ': <b class="' + signCls(total) + '">' + money(total) + '</b></span>'
      + '<span>Best day <b>' + (best ? money(best.v) : '—') + '</b></span>'
      + '<span>Roughest day <b>' + (worst ? money(worst.v) : '—') + '</b></span>'
      + '<span>' + days + ' trading days with data</span>';
  }
  document.getElementById('calSummary').innerHTML = sumHtml;
  renderDetail();
}
function pad(y,m){ return y + '-' + String(m).padStart(2,'0'); }

function dayCell(key, dnum, isLead){
  var d = calDays[key];
  var date = new Date(key + 'T12:00:00');
  var dow = date.getDay();
  var isWeekend = (dow === 0 || dow === 6);
  var isFuture = key > todayKey();
  var isToday = key === todayKey();
  var cls = 'day' + (isWeekend ? ' weekend' : '') + (isFuture ? ' future' : '') + (isToday ? ' today' : '') + (key === selectedDay ? ' selected' : '');
  if(isFuture || !d){
    return '<button type="button" class="' + cls + '" data-day="' + key + '"' + (isFuture ? ' disabled' : '') + '>'
      + '<span class="d">' + dnum + (isToday ? ' · today' : '') + '</span>'
      + '<span class="sub">' + (isWeekend ? 'Market closed' : (isFuture ? '—' : 'No trades logged')) + '</span></button>';
  }
  var mid = (activeFilter === 'flash' || activeFilter === 'zeroDte')
    ? '<span class="total">' + num(dayCount(d, activeFilter)) + '</span>'
    : '<span class="total ' + signCls(dayTotal(d, activeFilter)) + '">' + money(dayTotal(d, activeFilter)) + '</span>';
  var sub = (activeFilter === 'all') ? 'all trackers'
    : (activeFilter === 'flash') ? 'Flash setups'
    : (activeFilter === 'zeroDte') ? 'plans & alerts'
    : TYPE_LABEL[activeFilter] + ' only';
  var dots = '';
  TYPES.forEach(function(t){
    if(t.id === 'all') return;
    if((activeFilter === 'all' || activeFilter === t.id) && hasActivity(d, t.id)){
      dots += '<i class="dt-' + t.id + '" title="' + t.label + '"></i>';
    }
  });
  return '<button type="button" class="' + cls + '" data-day="' + key + '">'
    + '<span class="d">' + dnum + (isToday ? ' · today' : '') + (isLead ? ' · ' + date.toLocaleDateString('en-US',{month:'short'}) : '') + '</span>'
    + mid + '<span class="sub">' + sub + '</span>'
    + '<span class="dots">' + dots + '</span></button>';
}

function renderDetail(){
  var el = document.getElementById('dayDetail');
  var d = calDays[selectedDay];
  if(!d){
    el.innerHTML = '<h3>' + esc(fmtDayLong(selectedDay)) + '</h3><p>No trades logged for this day.</p>';
    return;
  }
  var total = dayTotal(d, 'all');
  var html = '<h3>' + esc(fmtDayLong(selectedDay)) + ' — <span class="' + signCls(total) + '">' + money(total) + '</span> total</h3>'
    + '<p>Smoothers, futures, and Flow Pulse are scored in dollars; Flash and 0DTE are scored by counts, not dollars.</p>'
    + '<div class="detail-grid">';
  DOLLAR.forEach(function(id){
    var k = apiKey(id), rec = d[k] || { pnl:0, count:0 };
    var v = rec.pnl || 0;
    html += '<div class="mini"><label><i class="m-' + id + '"></i>' + TYPE_LABEL[id] + '</label>'
      + '<strong class="' + signCls(v) + '">' + money(v) + '</strong>'
      + '<span>' + (v > 0 ? 'Made money' : (v < 0 ? 'Lost money' : 'No trades / flat')) + '</span></div>';
  });
  var fc = (d.flash && d.flash.count) || 0;
  var zc = (d['0dte'] && d['0dte'].count) || 0;
  html += '<div class="mini"><label><i class="m-flash"></i>Flash</label>'
    + '<strong>' + num(fc) + ' logged</strong><span>Setups — evidence, not P&amp;L</span></div>';
  html += '<div class="mini"><label><i class="m-zeroDte"></i>0DTE</label>'
    + '<strong>' + num(zc) + ' logged</strong><span>Plans &amp; alerts</span></div>';
  html += '</div>';
  el.innerHTML = html;
}

/* ---------------- boot ---------------- */
function monthStr(){ return calYear + '-' + String(calMonth).padStart(2,'0'); }

async function loadMonth(){
  var res = await fetch('/api/simple/calendar?month=' + monthStr());
  if(!res.ok) throw new Error('calendar HTTP ' + res.status);
  var j = await res.json();
  calDays = j.days || {};
  /* merge the previous month too, so leading grid days and week-to-date have real data */
  try{
    var py = calMonth === 1 ? calYear - 1 : calYear;
    var pm = calMonth === 1 ? 12 : calMonth - 1;
    var pres = await fetch('/api/simple/calendar?month=' + py + '-' + String(pm).padStart(2,'0'));
    if(pres.ok){
      var pj = await pres.json();
      Object.keys(pj.days || {}).forEach(function(k){ if(!calDays[k]) calDays[k] = pj.days[k]; });
    }
  }catch(e){ /* previous month is a nice-to-have */ }
  var t = todayKey();
  if(monthStr() === t.slice(0,7)){
    selectedDay = calDays[t] ? t : Object.keys(calDays).sort().pop() || t;
  } else {
    selectedDay = Object.keys(calDays).sort().pop() || null;
  }
  renderToday();
  renderCalendar();
}

async function init(){
  try{
    var res = await fetch('/api/simple');
    if(!res.ok) throw new Error('HTTP ' + res.status);
    SUMMARY = await res.json();
    var now = new Date();
    calYear = now.getFullYear(); calMonth = now.getMonth()+1;
    selectedDay = null;
    renderCards();
    renderBoard();
    renderFilters();
    selectDrill('futures', false);
    await loadMonth();

    document.getElementById('filters').addEventListener('click', function(e){
      var b = e.target.closest('.fbtn'); if(!b) return;
      activeFilter = b.getAttribute('data-f');
      var btns = this.querySelectorAll('.fbtn');
      for(var i=0;i<btns.length;i++) btns[i].setAttribute('aria-pressed', btns[i] === b ? 'true' : 'false');
      renderCalendar();
    });
    document.getElementById('calGrid').addEventListener('click', function(e){
      var b = e.target.closest('.day[data-day]'); if(!b || b.disabled) return;
      selectedDay = b.getAttribute('data-day');
      renderCalendar();
    });
    document.querySelectorAll('.card.pick').forEach(function(c){
      c.addEventListener('click', function(){ selectDrill(c.getAttribute('data-drill'), true); });
      c.addEventListener('keydown', function(e){
        if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); selectDrill(c.getAttribute('data-drill'), true); }
      });
    });
    document.querySelectorAll('#boardBody tr[data-drill]').forEach(function(r){
      r.addEventListener('click', function(){ selectDrill(r.getAttribute('data-drill'), true); });
    });
    document.getElementById('calPrev').addEventListener('click', function(){
      calMonth--; if(calMonth < 1){ calMonth = 12; calYear--; }
      loadMonth();
    });
    document.getElementById('calNext').addEventListener('click', function(){
      var t = todayKey().slice(0,7);
      if(monthStr() >= t) return; /* don't go past the current month */
      calMonth++; if(calMonth > 12){ calMonth = 1; calYear++; }
      loadMonth();
    });
  }catch(err){
    document.getElementById('t0s').textContent = 'Couldn\u2019t load results — refresh to retry.';
  }
}

if(document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

})();
