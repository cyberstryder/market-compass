'use strict';
/* Compass Chart (prototype): live Compass bars with the levels the system
 * trades on — GEX call/put walls, concentration, and the gamma flip —
 * drawn as price lines. Data: /api/bars, /api/gamma-levels, /api/gamma-flip.
 * Chart library: TradingView Lightweight Charts (vendored, Apache-2.0).
 * Times are rendered in America/Chicago wall-clock by shifting each
 * timestamp by the CT UTC offset (Lightweight Charts axes are UTC). */

var SYMBOLS = [
  { key: 'SPY', label: 'SPY', lvl: 'SPY' },
  { key: 'QQQ', label: 'QQQ', lvl: 'QQQ' },
  { key: 'IWM', label: 'IWM', lvl: 'IWM' },
  { key: 'NVDA', label: 'NVDA', lvl: 'NVDA' },
  { key: 'AAPL', label: 'AAPL', lvl: 'AAPL' },
  { key: 'TSLA', label: 'TSLA', lvl: 'TSLA' },
  { key: 'META', label: 'META', lvl: 'META' },
  { key: 'MSFT', label: 'MSFT', lvl: 'MSFT' },
  { key: 'MNQ.c.0', label: 'MNQ', lvl: 'NQ' },
  { key: 'MES.c.0', label: 'MES', lvl: 'ES' },
  { key: 'MGC.v.0', label: 'MGC', lvl: null },
  { key: 'MCL.v.0', label: 'MCL', lvl: null },
  { key: 'MYM.c.0', label: 'MYM', lvl: null },
  { key: 'M2K.c.0', label: 'M2K', lvl: null },
  { key: 'M6A.c.0', label: 'M6A', lvl: null },
  { key: 'M6E.c.0', label: 'M6E', lvl: null }
];

var TZ = 'America/Chicago';
var dtf = new Intl.DateTimeFormat('en-US', {
  timeZone: TZ, hourCycle: 'h23', year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'
});

function tzOffsetSeconds(epochSec) {
  var parts = {};
  var pieces = dtf.formatToParts(new Date(epochSec * 1000));
  for (var i = 0; i < pieces.length; i++) parts[pieces[i].type] = pieces[i].value;
  var wall = Date.UTC(+parts.year, +parts.month - 1, +parts.day,
    +parts.hour % 24, +parts.minute, +parts.second) / 1000;
  return wall - epochSec;
}

function ctClock(epochSec) {
  return new Intl.DateTimeFormat('en-US', {
    timeZone: TZ, hour: '2-digit', minute: '2-digit', hour12: false
  }).format(new Date(epochSec * 1000));
}

function num(v) {
  var n = typeof v === 'number' ? v : parseFloat(v);
  return isFinite(n) && n > 0 ? n : null;
}

/* /api/bars rows: [{ts, payload:{o,h,l,c,v}}], newest first. */
function normalizeBars(rows) {
  var byTime = {};
  (rows || []).forEach(function (r) {
    var p = (r && r.payload) || {};
    var ts = num(r && r.ts);
    var o = num(p.o), h = num(p.h), l = num(p.l), c = num(p.c);
    if (!ts || !o || !h || !l || !c) return;
    var t = ts + tzOffsetSeconds(ts);
    byTime[t] = { time: t, open: o, high: h, low: l, close: c, rawTs: ts };
  });
  return Object.keys(byTime).map(function (k) { return byTime[k]; })
    .sort(function (a, b) { return a.time - b.time; });
}

function fetchJSON(url) {
  return fetch(url, { cache: 'no-store' }).then(function (res) {
    if (!res.ok) throw new Error(url + ' -> ' + res.status);
    return res.json();
  });
}

var container = document.getElementById('chart');
var statusEl = document.getElementById('status');
var legendEl = document.getElementById('legend');
var symbolsEl = document.getElementById('symbols');

var chart = LightweightCharts.createChart(container, {
  layout: { background: { color: '#0f141a' }, textColor: '#c9d4e0' },
  grid: {
    vertLines: { color: '#1c2833' },
    horzLines: { color: '#1c2833' }
  },
  timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#2d3f4e' },
  rightPriceScale: { borderColor: '#2d3f4e' },
  crosshair: { mode: LightweightCharts.CrosshairMode.Normal }
});
var series = chart.addCandlestickSeries({
  upColor: '#2ebd85', downColor: '#e5534b',
  wickUpColor: '#2ebd85', wickDownColor: '#e5534b', borderVisible: false
});

var current = SYMBOLS[0];
var priceLines = [];
var lastBarTime = 0;
var barCount = 0;

function clearPriceLines() {
  priceLines.forEach(function (pl) { series.removePriceLine(pl); });
  priceLines = [];
}

function addLine(price, color, style, title) {
  if (!price) return null;
  var pl = series.createPriceLine({
    price: price, color: color, lineWidth: 1, lineStyle: style,
    axisLabelVisible: true, title: title
  });
  priceLines.push(pl);
  return pl;
}

function loadLevels() {
  if (!current.lvl) {
    legendEl.textContent = 'Gamma levels are computed for equities, NQ and ES only — ' + current.label + ' shows bars alone for now.';
    return;
  }
  var lvl = encodeURIComponent(current.lvl);
  Promise.all([
    fetchJSON('/api/gamma-levels?symbol=' + lvl).catch(function () { return null; }),
    fetchJSON('/api/gamma-flip?symbol=' + lvl).catch(function () { return null; })
  ]).then(function (results) {
    clearPriceLines();
    var gl = results[0] || {};
    var gf = results[1] || {};
    var levels = (gl && gl.levels) || {};
    var parts = [];
    var cw = num(levels.call_wall), pw = num(levels.put_wall),
        conc = num(levels.concentration), flip = num(gf && gf.flip);
    var fmt = function (v) { return v == null ? '' : (Math.round(v * 100) / 100).toFixed(2); };
    if (cw) { addLine(cw, '#2ebd85', LightweightCharts.LineStyle.Solid, 'Call wall'); parts.push('<span><b style="color:#2ebd85">Call wall</b> ' + fmt(cw) + '</span>'); }
    if (pw) { addLine(pw, '#e5534b', LightweightCharts.LineStyle.Solid, 'Put wall'); parts.push('<span><b style="color:#e5534b">Put wall</b> ' + fmt(pw) + '</span>'); }
    if (conc) { addLine(conc, '#d29922', LightweightCharts.LineStyle.Dotted, 'GEX concentration'); parts.push('<span><b style="color:#d29922">Concentration</b> ' + fmt(conc) + '</span>'); }
    if (flip) { addLine(flip, '#b083f0', LightweightCharts.LineStyle.Dashed, 'Gamma flip'); parts.push('<span><b style="color:#b083f0">Gamma flip</b> ' + fmt(flip) + (gf.regime ? ' (' + gf.regime + ')' : '') + '</span>'); }
    legendEl.innerHTML = parts.length
      ? parts.join('')
      : 'Levels: waiting (no gamma data for ' + current.lvl + ' yet)';
  });
}

function applyBars(bars, initial) {
  if (!bars.length) {
    if (initial) {
      statusEl.textContent = current.label + ': no bars stored yet';
      barCount = 0; lastBarTime = 0;
    }
    return;
  }
  if (initial || !lastBarTime) {
    series.setData(bars);
    chart.timeScale().fitContent();
  } else {
    bars.forEach(function (b) {
      if (b.time >= lastBarTime) series.update(b);
    });
  }
  var last = bars[bars.length - 1];
  lastBarTime = last.time;
  barCount = bars.length;
  statusEl.textContent = current.label + ' · ' + barCount + ' bars · last ' +
    ctClock(last.rawTs) + ' CT · poll 10s';
}

function loadBars(initial) {
  fetchJSON('/api/bars?symbol=' + encodeURIComponent(current.key) + '&limit=600')
    .then(function (rows) { applyBars(normalizeBars(rows), initial); })
    .catch(function (err) {
      statusEl.textContent = current.label + ': bars failed to load (' + err.message + ')';
    });
}

function selectSymbol(entry) {
  current = entry;
  lastBarTime = 0; barCount = 0;
  clearPriceLines();
  series.setData([]);
  Array.prototype.forEach.call(symbolsEl.children, function (btn) {
    btn.classList.toggle('active', btn.dataset.key === entry.key);
  });
  statusEl.textContent = entry.label + ': loading…';
  loadBars(true);
  loadLevels();
}

SYMBOLS.forEach(function (entry) {
  var btn = document.createElement('button');
  btn.textContent = entry.label;
  btn.dataset.key = entry.key;
  if (entry === current) btn.classList.add('active');
  btn.addEventListener('click', function () { selectSymbol(entry); });
  symbolsEl.appendChild(btn);
});

function resize() {
  chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
}
window.addEventListener('resize', resize);
resize();

selectSymbol(current);
setInterval(function () { loadBars(false); }, 10000);
setInterval(loadLevels, 60000);
