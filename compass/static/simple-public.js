/* Market Compass public results page — plain-English layout, real data.
   Reads /api/simple (scoreboards + drill-downs) and /api/simple/calendar. */
(function(){
'use strict';

var TYPES = [
  { id:'all',       label:'All trackers' },
  { id:'smoothers', label:'Smoothers',   api:'smoothers' },
  { id:'futures',   label:'Futures',     api:'futures' },
  { id:'zeroDte',   label:'0DTE',        api:'0dte' },
  { id:'swings',    label:'Swings',      api:'swings' },
  { id:'flowPulse', label:'Flow Pulse',  api:'flow_pulse' }
];
var TYPE_LABEL = { smoothers:'Smoothers', futures:'Futures', zeroDte:'0DTE', swings:'Swings', flowPulse:'Flow Pulse' };
var DOLLAR = ['smoothers','futures','flowPulse','zeroDte','swings']; /* mockup ids that carry P&L */

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
function expR(v){
  if(v === null || v === undefined) return '';
  var s = (v >= 0 ? '+' : '') + Number(v).toFixed(2) + 'R';
  return ' · <b class="' + (v >= 0 ? 'pos' : 'neg') + '">' + s + ' expectancy</b>';
}

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
  return (d['0dte'] ? d['0dte'].count : 0);
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
  var sm = T.smoothers.stats, fu = T.futures.stats, zd = T['0dte'].stats, fl2 = T.flow_pulse.stats;
  set('smoothers', 'All time <b class="' + signCls(sm.pnl) + '">' + money(sm.pnl) + '</b>');
  set('futures',   'All time <b class="' + signCls(fu.pnl) + '">' + money(fu.pnl) + '</b>');
  if(zd.tracked > 0){
    set('0dte', 'All time <b class="' + signCls(zd.pnl) + '">' + money(zd.pnl) + '</b>');
    document.getElementById('pill-0dte').textContent = 'Paper / proving';
  } else {
    set('0dte', '<b>' + num(zd.plans) + '</b> plans · <b>' + num(zd.alerts) + '</b> alerts · paper P&amp;L starts with the next signal');
  }
  if(fl2.tracked > 0){
    set('flow', 'All time <b class="' + signCls(fl2.pnl) + '">' + money(fl2.pnl) + '</b>');
  } else {
    set('flow', 'No tracks closed yet');
  }
  /* digest: next weekday ~4pm CT */
  var d = new Date(); var day = d.getDay();
  document.getElementById('digestNext').textContent = (day === 0 || day === 6) ? 'Mon ~4:00pm CT' : 'Today ~4:00pm CT';
}

/* ---------------- drill-down ---------------- */
var DRILL = {
  smoothers: { title:'Smoothers, ticker by ticker',
    sub:'One card per ticker the Smoothers book has touched. Real paper P&L per name — bought at the ask, sold at the bid, fees in.',
    pill:'Live', pillCls:'live', hit:false, deeper:'View weeks ›' },
  futures: { title:'Futures, method by method',
    sub:'Every pattern detector, scored separately. Paper trades on micro futures — everything forced flat by 3:45pm CT.',
    pill:'Paper / proving', pillCls:'test', hit:false, deeper:'View trades ›' },
  zeroDte: { title:'0DTE, name by name',
    sub:'The morning SPY plan plus every scanner alert. Paper trades 1 contract at the ask — exits at the plan target/stop or 2:55pm CT.',
    pill:'Paper / proving', pillCls:'test', hit:false, deeper:'View entries ›' },
  flowPulse: { title:'Flow Pulse, by direction',
    sub:'Split by which way the big money bet. Tap a direction to see the actual pulses — each opens a two-week paper track when a contract can be suggested.',
    pill:'Paper tracking', pillCls:'test', hit:false, deeper:'View pulses ›' },
  swings: { title:'Swings, idea by idea',
    sub:'Every swing idea (1-21 DTE), newest first. Paper-tracked, 1 contract each.',
    pill:'Paper / proving', pillCls:'test', hit:false, deeper:'View ideas ›' }
};
var drillType = null; /* mockup id of the level-2 view being shown */

function tagFor(name, id){
  if(id === 'smoothers') return name.length <= 6 ? name : name.slice(0,6);
  var words = String(name).split(/[^A-Za-z0-9]+/).filter(Boolean);
  var tag = words.slice(0,2).map(function(w){ return w[0]; }).join('').toUpperCase();
  return tag || '?';
}
var DOT_CLASS = { smoothers:'d-smooth', futures:'d-futures', zeroDte:'d-0dte', swings:'d-swings', flowPulse:'d-flow' };

function drillCard(it, id, cfg){
  var st = it.stats || {};
  var n = st.tracked || 0, w = st.wins || 0;
  var res;
  if(cfg.hit){
    var r = st.resolved || 0;
    if(r > 0){
      res = '<span><b>' + num(w) + ' of ' + num(r) + ' targets · ' + pct(st.win_rate) + '</b>' + expR(st.expectancy_r) + '</span>';
    } else if(n > 0){
      res = '<span><b>' + num(n) + '</b> logged · none resolved yet</span>';
    } else {
      res = '<span>No setups yet</span>';
    }
    res += '<span class="pill ' + cfg.pillCls + '">' + cfg.pill + '</span>';
  } else if(n > 0){
    var rate = pct(st.win_rate);
    res = '<span><b class="' + signCls(st.pnl) + '">' + money(st.pnl) + '</b> · ' + num(n) + ' trades · ' + rate + ' wins'
      + (st.data_gaps ? ' · ' + num(st.data_gaps) + ' excluded (no data)' : '')
      + (st.undelivered ? ' · ' + num(st.undelivered) + ' excluded (not alerted)' : '') + '</span>'
      + '<span class="pill ' + cfg.pillCls + '">' + cfg.pill + '</span>';
  } else if((st.pulses || 0) > 0){
    res = '<span><b>' + num(st.pulses) + '</b> pulses · no tracks closed yet</span>'
      + '<span class="pill ' + cfg.pillCls + '">' + cfg.pill + '</span>';
  } else {
    res = '<span>No trades yet</span><span class="pill">Watching</span>';
  }
  var deep = ((st.tracked || 0) + (st.pulses || 0) > 0) && cfg.deeper
    ? '<span class="cta">' + esc(cfg.deeper) + '</span>' : '';
  return '<article class="card' + (deep ? ' deeper' : '') + '"'
    + (deep ? ' data-detail="' + esc(it.name) + '" tabindex="0" role="button" aria-label="Show ' + esc(cfg.deeper.replace(' ›','')) + ' for ' + esc(it.name) + '"' : '')
    + '>'
    + '<div class="card-top"><span class="dot tag ' + (DOT_CLASS[id] || 'd-flash') + '">' + esc(tagFor(it.name, id)) + '</span>'
    + '<div><h3>' + esc(it.name) + '</h3></div></div>'
    + '<p>' + esc(it.watches || '') + '</p>'
    + deep
    + '<div class="result">' + res + '</div></article>';
}

function selectDrill(id, scroll, showList){
  var cfg = DRILL[id];
  if(!cfg || !SUMMARY) return;
  // For Smoothers, default to this week's picks view unless showList is true
  // (e.g., when coming back via the back button)
  if(id === 'smoothers' && !showList){
    openDetail(id, 'current-week');
    return;
  }
  drillType = id;
  var back = document.getElementById('drillBack');
  back.hidden = true;
  document.getElementById('drillHintText').textContent = 'Same cards, one level deeper';
  var api = TYPES.filter(function(x){ return x.id === id; })[0].api;
  var block = SUMMARY.types[api];
  document.getElementById('drill-h').textContent = cfg.title;
  document.getElementById('drillSub').textContent = cfg.sub;
  var st = block.stats, n = st.tracked || 0, w = st.wins || 0;
  var sum;
  if(cfg.hit){
    var r = st.resolved || 0;
    sum = ['<b>' + num(n) + '</b> setups logged',
           '<b>' + num(w) + '</b> clean targets' + (r ? ' · ' + pct(st.win_rate) : '') + expR(st.expectancy_r),
           'No dollars attached — evidence only'];
  } else {
    sum = ['<b>' + num(n) + '</b> trades',
           '<b>' + num(w) + '</b> wins · ' + pct(st.win_rate),
           '<b>' + money(st.pnl) + '</b> net'];
    if(st.open) sum.push('<b>' + num(st.open) + '</b> still open');
    if(st.data_gaps) sum.push('<b>' + num(st.data_gaps) + '</b> excluded — no live data (' + money(st.data_gap_pnl) + ')');
    if(st.undelivered) sum.push('<b>' + num(st.undelivered) + '</b> excluded — alert not delivered (' + money(st.undelivered_pnl) + ')');
    if(id === 'zeroDte') sum.push('<b>' + num(st.plans) + '</b> plans · <b>' + num(st.alerts) + '</b> alerts');
    if(id === 'flowPulse') sum.push('<b>' + num(st.pulses) + '</b> pulses');
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
  wireDeeper(id);
  if(scroll){
    var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    document.getElementById('drillSection').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block:'start' });
  }
}

function wireDeeper(id){
  document.querySelectorAll('#drillGrid .card.deeper').forEach(function(c){
    var open = function(){ openDetail(id, c.getAttribute('data-detail')); };
    c.addEventListener('click', open);
    c.addEventListener('keydown', function(e){
      if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); open(); }
    });
  });
}

function detailCard(it, dotCls){
  var rows = (it.rows || []).map(function(r){
    return '<div><dt>' + esc(r[0]) + '</dt><dd>' + esc(r[1]) + '</dd></div>';
  }).join('');
  return '<article class="card">'
    + '<div class="card-top"><span class="dot tag ' + dotCls + '">' + esc(it.tag || '?') + '</span>'
    + '<div><h3>' + esc(it.title) + '</h3></div></div>'
    + (it.sub ? '<p>' + esc(it.sub) + '</p>' : '')
    + (rows ? '<dl class="rows">' + rows + '</dl>' : '')
    + '<div class="result"><span class="' + (it.result_cls || '') + '">' + esc(it.result || '') + '</span></div></article>';
}

async function openDetail(id, itemName){
  var cfg = DRILL[id];
  var api = TYPES.filter(function(x){ return x.id === id; })[0].api;
  document.getElementById('drill-h').textContent = itemName;
  document.getElementById('drillSub').textContent = 'Loading…';
  document.getElementById('drillGrid').innerHTML = '';
  document.getElementById('drillSum').innerHTML = '';
  try{
    var res = await fetch('/api/simple/detail?type=' + encodeURIComponent(api) + '&name=' + encodeURIComponent(itemName));
    if(!res.ok) throw new Error('HTTP ' + res.status);
    var d = await res.json();
    document.getElementById('drill-h').textContent = d.title || itemName;
    document.getElementById('drillSub').textContent = d.sub || '';
    var items = d.items || [];
    document.getElementById('drillSum').innerHTML =
      '<span><b>' + items.length + '</b> ' + (items.length === 1 ? 'entry' : 'entries') + '</span>';
    var dotCls = DOT_CLASS[id] || 'd-flash';
    document.getElementById('drillGrid').innerHTML = items.length
      ? items.map(function(it){ return detailCard(it, dotCls); }).join('')
      : '<p class="muted-note">Nothing logged here yet.</p>';
    var back = document.getElementById('drillBack');
    back.hidden = false;
    back.textContent = '← ' + cfg.title;
    back.onclick = function(){ selectDrill(id, true, true); };
    document.getElementById('drillHintText').textContent = '';
  }catch(err){
    document.getElementById('drillSub').textContent = 'Couldn\u2019t load the detail — try again.';
  }
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.getElementById('drillSection').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block:'start' });
}

/* ---------------- scoreboard ---------------- */
var BOARD = [
  { id:'smoothers', api:'smoothers',  status:'Live',           pill:'live' },
  { id:'futures',   api:'futures',    status:'Paper / proving',pill:'test' },
  { id:'zeroDte',   api:'0dte',       status:'Paper / proving',pill:'test' },
  { id:'flowPulse', api:'flow_pulse', status:'Paper tracking', pill:'test' }
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
      result = rr > 0 ? '<b>' + num(w) + '/' + num(rr) + ' targets · ' + Math.round(100*w/rr) + '%</b>' + expR(st.expectancy_r)
                      : '<b>' + num(n) + '</b> logged';
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

/* ---------------- what we're tracking ---------------- */
var FAMILY_LABEL = { session_sweep_reclaim:'Session sweep reclaim', trend_pullback:'Trend pullback',
  volume_breakout:'Volume breakout', level_breakdown:'Level breakdown',
  exposure_level_break:'Exposure level break', flow_price_breakout:'Flow price breakout' };
function famLabel(f){ return FAMILY_LABEL[f] || f; }
function pfFmt(x){ return (x === null || x === undefined) ? '—' : Number(x).toFixed(2); }
function rateFmt(x){ return (x === null || x === undefined) ? '—' : (Math.round(x*10)/10) + '%'; }
function dollars(n){
  if(n === null || n === undefined || isNaN(n)) return '—';
  var v = Math.round(n);
  return (v < 0 ? '-$' : '$') + Math.abs(v).toLocaleString('en-US');
}
function whenFmt(ts){
  if(!ts) return '—';
  return new Date(ts*1000).toLocaleString('en-US', { month:'short', day:'numeric', hour:'numeric', minute:'2-digit' });
}
function strikeFmt(x){
  if(x === null || x === undefined) return '';
  var v = Number(x);
  return isNaN(v) ? String(x) : (Math.round(v*100)/100).toLocaleString('en-US');
}

function renderTracking(){
  var host = document.getElementById('trackingBody');
  if(!host) return;
  var T = SUMMARY && SUMMARY.tracking;
  if(!T){ host.innerHTML = '<p class="track-note">Tracking view unavailable right now — refresh to retry.</p>'; return; }
  var html = '';

  /* ---- futures book: live combos + paper-only watch list ---- */
  var fb = T.futures_book;
  html += '<div class="track-block"><h3 class="track-h3">Futures book — the live combos and the paper-only watch list</h3>';
  if(fb && fb.combos && fb.combos.length){
    html += '<div class="board static"><table><thead><tr>'
      + '<th>Combo</th><th>Book</th><th class="num">Trades · ' + num(fb.window_days) + 'd</th>'
      + '<th class="num">Win rate</th><th class="num">PF</th><th class="num">Net</th><th>Watch</th>'
      + '</tr></thead><tbody>';
    fb.combos.forEach(function(x){
      html += '<tr>'
        + '<td data-l="Combo"><b>' + esc(x.label) + ' · ' + esc(x.symbol) + '</b></td>'
        + '<td data-l="Book"><span class="pill ' + (x.live ? 'live' : 'test') + '">' + (x.live ? 'Live' : 'Paper only') + '</span></td>'
        + '<td class="num" data-l="Trades">' + num(x.trades) + '</td>'
        + '<td class="num" data-l="Win rate">' + rateFmt(x.win_rate) + '</td>'
        + '<td class="num" data-l="PF">' + pfFmt(x.pf) + '</td>'
        + '<td class="num" data-l="Net"><b class="' + signCls(x.net) + '">' + money(x.net) + '</b></td>'
        + '<td data-l="Watch">' + (x.kill_watch ? '<span class="pill warn">Kill watch</span>' : '—') + '</td>'
        + '</tr>';
    });
    html += '</tbody></table></div>'
      + '<p class="track-note">Trailing ' + num(fb.window_days) + ' days of closed trades per combo. <b>Live</b> combos route to TradersPost (Tradeify); <b>paper only</b> combos are tracked for attribution while they prove themselves. A combo whose profit factor drops under 1.0 goes on kill watch — that is where the disable conversation starts.</p>';
  } else {
    html += '<p class="track-note">Futures book unavailable right now.</p>';
  }
  html += '</div>';

  /* ---- 0DTE scanner: seats + shadow ---- */
  var sc = T.scanner;
  html += '<div class="track-block"><h3 class="track-h3">0DTE scanner — who kept a paper seat, who went to shadow</h3>';
  if(sc){
    var seatBits = [];
    Object.keys(sc.paper_seats || {}).forEach(function(fam){
      seatBits.push('<b>' + esc(famLabel(fam)) + ' — ' + esc((sc.paper_seats[fam] || []).join(' · ')) + '</b>');
    });
    html += '<p class="track-line">' + (sc.available ? 'Paper seats: ' : 'Paper seats decided Oct 25 (split deploying): ')
      + (seatBits.length ? seatBits.join('; ') : 'none') + '. '
      + 'Every other scanner cell is shadow-scored — alerts and evidence recorded, outcomes scored on fresh quotes, nothing booked.</p>';
    var seated = sc.seated || [];
    var anySeated = seated.some(function(x){ return (x.trades || 0) > 0 || (x.open || 0) > 0; });
    if(anySeated){
      html += '<div class="board static"><table><thead><tr>'
        + '<th>Seated cell</th><th class="num">Trades</th><th class="num">Win rate</th>'
        + '<th class="num">PF</th><th class="num">Net</th><th class="num">Open</th>'
        + '</tr></thead><tbody>';
      seated.forEach(function(x){
        html += '<tr>'
          + '<td data-l="Seated cell"><b>' + esc(x.family_label) + ' × ' + esc(x.underlying) + '</b></td>'
          + '<td class="num" data-l="Trades">' + num(x.trades) + '</td>'
          + '<td class="num" data-l="Win rate">' + rateFmt(x.win_rate) + '</td>'
          + '<td class="num" data-l="PF">' + pfFmt(x.pf) + '</td>'
          + '<td class="num" data-l="Net"><b class="' + signCls(x.net) + '">' + money(x.net) + '</b></td>'
          + '<td class="num" data-l="Open">' + num(x.open) + '</td>'
          + '</tr>';
      });
      html += '</tbody></table></div>';
    } else {
      html += '<p class="track-note">No seated paper trades yet — the seats start booking with the next session\'s qualifying alerts.</p>';
    }
    var sh = sc.shadow;
    if(sh){
      var tot = sh.totals || {};
      if((tot.alerts || 0) > 0){
        html += '<p class="track-line">Shadow scorecard: <b>' + num(tot.alerts) + '</b> alerts tracked across <b>'
          + num(sh.cells_tracking) + '</b> cells · <b>' + num(tot.scored) + '</b> scored · <b>' + num(tot.open)
          + '</b> still open' + (tot.data_gaps ? ' · ' + num(tot.data_gaps) + ' data gaps (excluded)' : '') + '.</p>';
        if(sh.cells && sh.cells.length){
          var bar = sh.bar || { min_scored:20, min_pf:1.2 };
          html += '<div class="board static"><table><thead><tr>'
            + '<th>Shadow cell</th><th class="num">Alerts</th><th class="num">Scored</th>'
            + '<th class="num">PF</th><th>Graduation — ' + num(bar.min_scored) + ' scored · PF ≥ ' + pfFmt(bar.min_pf) + '</th>'
            + '</tr></thead><tbody>';
          sh.cells.forEach(function(x){
            var grad;
            if(x.ready) grad = '<span class="pill live">Ready for a seat decision</span>';
            else {
              grad = num(x.scored) + '/' + num(bar.min_scored) + ' scored';
              if((x.scored_needed || 0) === 0 && !x.pf_met) grad += ' · PF below the bar';
              else if((x.scored_needed || 0) === 0) grad += ' · family not promoted';
            }
            html += '<tr>'
              + '<td data-l="Shadow cell"><b>' + esc(x.family_label) + ' × ' + esc(x.underlying) + '</b></td>'
              + '<td class="num" data-l="Alerts">' + num(x.alerts) + '</td>'
              + '<td class="num" data-l="Scored">' + num(x.scored) + '</td>'
              + '<td class="num" data-l="PF">' + pfFmt(x.pf) + '</td>'
              + '<td data-l="Graduation">' + grad + '</td>'
              + '</tr>';
          });
          html += '</tbody></table></div>';
        }
        if(sh.ready && sh.ready.length){
          html += '<p class="track-note">Cleared the graduation bar: <b>' + esc(sh.ready.join(', ')) + '</b> — flagged for a seat decision; promotion is never automatic.</p>';
        }
      } else {
        html += '<p class="track-note">No shadow alerts yet — shadow tracking starts with the next session: every non-seated alert is recorded here and scored on fresh quotes instead of booked.</p>';
      }
    } else if(sc.available){
      html += '<p class="track-note">Shadow scorecard has not reported yet — it starts with the next session\'s alerts.</p>';
    }
  } else {
    html += '<p class="track-note">Scanner split unavailable right now.</p>';
  }
  html += '</div>';

  /* ---- iceberg watch ---- */
  var ib = T.iceberg;
  html += '<div class="track-block"><h3 class="track-h3">Iceberg watch — hidden institutional options flow</h3>';
  if(ib){
    if(ib.diag_present){
      html += '<p class="track-line">Detector ' + (ib.enabled === false ? 'off' : 'on')
        + ' · <b>' + num(ib.prints_seen) + '</b> prints seen · tracking <b>' + num(ib.contracts_tracked)
        + '</b> contracts · <b>' + num(ib.alerts_fired) + '</b> alerts fired'
        + ((ib.errors || 0) > 0 ? ' · <b class="neg">' + num(ib.errors) + ' errors</b>' : ' · 0 errors') + '.</p>';
    } else {
      html += '<p class="track-line">The detector reports in from the Collector while markets stream — no report yet today.</p>';
    }
    var latest = ib.latest || [];
    if(latest.length){
      html += '<div class="board static"><table><thead><tr>'
        + '<th>When</th><th>Contract</th><th class="num">Est. size</th>'
        + '<th class="num">Est. premium</th><th class="num">Score</th><th class="num">At close</th>'
        + '</tr></thead><tbody>';
      latest.forEach(function(a){
        var contract = esc(a.underlying) + ' ' + strikeFmt(a.strike) + ' ' + (a.type === 'call' ? 'Call' : 'Put')
          + ' <span class="rec-note">exp ' + esc(a.expiry || '') + '</span>';
        var close = (a.close_ret_pct === null || a.close_ret_pct === undefined) ? 'pending'
          : '<b class="' + signCls(a.close_ret_pct) + '">' + (a.close_ret_pct >= 0 ? '+' : '') + Number(a.close_ret_pct).toFixed(1) + '%</b>';
        html += '<tr>'
          + '<td data-l="When">' + whenFmt(a.alert_ts) + '</td>'
          + '<td data-l="Contract"><b>' + contract + '</b></td>'
          + '<td class="num" data-l="Est. size">~' + num(a.est_contracts) + '</td>'
          + '<td class="num" data-l="Est. premium">' + dollars(a.est_premium) + '</td>'
          + '<td class="num" data-l="Score">' + (a.score === null || a.score === undefined ? '—' : Number(a.score).toFixed(2)) + '</td>'
          + '<td class="num" data-l="At close">' + close + '</td>'
          + '</tr>';
      });
      html += '</tbody></table></div>';
    } else {
      html += '<p class="track-note">No iceberg alerts yet — the detector watches the live options stream for a sliced institutional order (≥500 contracts and ≥$25k of premium, mostly at the ask, inside 30 minutes). When accumulation fires, the alert and its scored outcome land here.</p>';
    }
    var promo = ib.promotion || {};
    var closeH = ib.close || {};
    if((ib.alerts_window || 0) > 0){
      html += '<p class="track-note">Scored at the close: <b>' + num(closeH.scored) + '</b> alerts'
        + (closeH.mean_contract_ret_pct !== null && closeH.mean_contract_ret_pct !== undefined
            ? ' · average contract move <b class="' + signCls(closeH.mean_contract_ret_pct) + '">'
              + (closeH.mean_contract_ret_pct >= 0 ? '+' : '') + Number(closeH.mean_contract_ret_pct).toFixed(1) + '%</b>' : '')
        + '. Promotion bar: ≥40 scored with two straight positive 4-week windows — currently <b>'
        + num(promo.scored_total) + ' scored</b>, ' + (promo.meets_bar ? '<b class="pos">bar met</b>' : 'not met yet') + '.</p>';
    }
  } else {
    html += '<p class="track-note">Iceberg watch unavailable right now.</p>';
  }
  html += '</div>';

  host.innerHTML = html;
}

/* ---------------- calendar ---------------- */
function apiKey(id){ return TYPES.filter(function(x){ return x.id === id; })[0].api; }

function dayTotal(d, filter){
  if(!d) return 0;
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
  var total = 0, best = null, worst = null, days = 0;
  monthDays().forEach(function(k){
    var v = dayTotal(calDays[k], activeFilter);
    total += v; days++;
    if(best === null || v > best.v) best = { k:k, v:v };
    if(worst === null || v < worst.v) worst = { k:k, v:v };
  });
  var sumHtml = '<span>' + win + ' · ' + label + ': <b class="' + signCls(total) + '">' + money(total) + '</b></span>'
    + '<span>Best day <b>' + (best ? money(best.v) : '—') + '</b></span>'
    + '<span>Roughest day <b>' + (worst ? money(worst.v) : '—') + '</b></span>'
    + '<span>' + days + ' trading days with data</span>';
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
  var mid = '<span class="total ' + signCls(dayTotal(d, activeFilter)) + '">' + money(dayTotal(d, activeFilter)) + '</span>';
  var sub = (activeFilter === 'all') ? 'all trackers'
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
    + '<p>Every tracker below is scored in dollars.</p>'
    + '<div class="detail-grid">';
  DOLLAR.forEach(function(id){
    var k = apiKey(id), rec = d[k] || { pnl:0, count:0 };
    var v = rec.pnl || 0;
    html += '<div class="mini"><label><i class="m-' + id + '"></i>' + TYPE_LABEL[id] + '</label>'
      + '<strong class="' + signCls(v) + '">' + money(v) + '</strong>'
      + '<span>' + (v > 0 ? 'Made money' : (v < 0 ? 'Lost money' : 'No trades / flat')) + '</span></div>';
  });
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
    renderTracking();
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
