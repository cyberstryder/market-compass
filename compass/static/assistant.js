/* Actual server stages; source facts are not a transcript of model reasoning. */
(() => {
 async function readEvents(response, onEvent) {
  if (!response.ok) {
   const data = await response.json().catch(() => ({}));
   throw new Error(data.detail || 'Unable to start the answer. Please try again.');
  }
  if (!response.body) throw new Error('This browser could not read progress updates. Please reload.');
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let buffer = '', complete = false;
  function line(text) {
   if (!text.trim()) return;
   const event = JSON.parse(text);
   onEvent(event);
   if (event.type === 'complete' || event.type === 'error') complete = true;
  }
  try {
   while (!complete) {
    const {value, done} = await reader.read();
    buffer += decoder.decode(value, {stream: !done});
    if (buffer.length > 262144) throw new Error('The response was too large to display safely.');
    let boundary;
    while (!complete && (boundary = buffer.indexOf('\n')) >= 0) {
     const next = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 1); line(next);
    }
    if (done) {
     if (!complete && buffer.trim()) line(buffer);
     if (!complete) throw new Error('Connection ended before the answer was ready. Please retry.');
     break;
    }
   }
  } finally {
   await reader.cancel().catch(() => {});
   reader.releaseLock();
  }
 }
 if (typeof module !== 'undefined') module.exports = {readEvents};
 if (typeof document === 'undefined') return;

 const el = id => document.getElementById(id), form = el('ask');
 if (!form) return;
 const button = form.querySelector('button[type="submit"]'), cancel = el('ask-cancel');
 const stages = ['gathering', 'preparing', 'generating'];
 const labels = {gathering: 'Gathering recorded market data', preparing: 'Preparing the question context', generating: 'Writing your answer'};
 const clock = ts => Number.isFinite(ts) && ts > 0 ? new Date(ts * 1000).toLocaleString('en-US', {
  timeZone: 'America/Chicago', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit'
 }) + ' CT' : 'Time not recorded';
 const valueText = value => typeof value === 'number' ? value.toLocaleString('en-US', {maximumFractionDigits: 4}) : String(value);
 let busy = false, controller;
 cancel.addEventListener('click', () => controller?.abort());

 function facts(data) {
  el('ask-evidence').hidden = false;
  el('ask-evidence-title').textContent = 'Data supplied for ' + (data.symbols || []).join(', ');
  el('ask-evidence-note').textContent = data.note + (data.limited ? ' Some context is limited or unavailable.' : '');
  const grid = el('ask-facts'); grid.replaceChildren();
  for (const fact of data.cards || []) {
   const card = document.createElement('article'); card.className = 'ask-fact';
   const heading = document.createElement('strong'); heading.textContent = fact.symbol + ' · ' + fact.label;
   const state = document.createElement('span'); state.className = 'ask-fact-state ' + (fact.status === 'included' ? 'included' : 'limited');
   state.textContent = ({included: 'Included', limited: 'Limited preview', missing: 'Unavailable', not_included: 'Not included'})[fact.status] || 'Unavailable';
   const values = document.createElement('p'); values.textContent = (fact.values || []).map(v => v.label + ': ' + valueText(v.value)).join(' · ') || 'No value supplied';
   const source = document.createElement('p'); source.className = 'fine'; source.textContent = fact.source + ' · ' + fact.time_label + ' ' + clock(fact.at);
   card.append(heading, state, values, source);
   if (fact.note) { const note = document.createElement('p'); note.className = 'fine'; note.textContent = fact.note; card.append(note); }
   grid.append(card);
  }
 }

 form.addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  busy = true; controller = new AbortController();
  const started = performance.now(); let stage = 'gathering', receivedStage = false, timedOut = false;
  button.disabled = true; button.textContent = 'Working…'; cancel.hidden = false;
  el('question').readOnly = true; form.setAttribute('aria-busy', 'true');
  const panel = el('ask-progress'), meter = el('ask-meter');
  panel.hidden = false; panel.dataset.state = 'working'; meter.removeAttribute('value');
  el('ask-evidence').hidden = true; el('ask-evidence').open = true; el('ask-facts').replaceChildren();
  el('answer').textContent = ''; el('answer-time').textContent = ''; el('ask-wait-note').textContent = '';
  function setStage(next) {
   stage = next; el('ask-status').textContent = labels[next];
   document.querySelectorAll('[data-ask-stage]').forEach(item => {
    const index = stages.indexOf(item.dataset.askStage), current = stages.indexOf(next);
    item.classList.toggle('active', index === current); item.classList.toggle('done', index < current);
    if (index === current) item.setAttribute('aria-current', 'step'); else item.removeAttribute('aria-current');
   });
  }
  function tick() {
   const seconds = Math.floor((performance.now() - started) / 1000);
   el('ask-elapsed').textContent = seconds + 's elapsed';
   if (seconds >= 20) el('ask-wait-note').textContent = !receivedStage ? 'Waiting for the server to accept this question…' : stage === 'generating' ? 'Still writing from the recorded data below. You can cancel this request.' : 'Still gathering the recorded context. You can cancel this request.';
  }
  setStage('gathering'); el('ask-status').textContent = 'Sending your question'; tick();
  const ticker = setInterval(tick, 1000);
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, 100000);
  try {
   const response = await fetch('/api/ask/stream', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({question: el('question').value}), signal: controller.signal});
   await readEvents(response, data => {
    if (data.type === 'stage' && stages.includes(data.stage)) { receivedStage = true; setStage(data.stage); }
    if (data.type === 'context') facts(data);
    if (data.type === 'error') throw new Error(data.detail || 'Unable to finish this answer.');
    if (data.type === 'complete') {
     el('answer').textContent = data.answer; el('answer-time').textContent = 'Context captured ' + clock(data.asof);
     el('ask-evidence').open = false;
     panel.dataset.state = data.configured ? 'complete' : 'unavailable';
     el('ask-status').textContent = data.configured ? 'Answer ready' : 'Assistant is not configured';
     meter.value = data.configured ? 1 : 0;
     document.querySelectorAll('[data-ask-stage]').forEach(item => {
      item.classList.remove('active'); item.classList.toggle('done', Boolean(data.configured)); item.removeAttribute('aria-current');
     });
    }
   });
  } catch (error) {
   const cancelled = controller.signal.aborted && !timedOut;
   panel.dataset.state = cancelled ? 'cancelled' : 'error'; meter.value = 0;
   el('ask-status').textContent = cancelled ? 'Request cancelled' : 'Answer could not finish';
   el('answer').textContent = cancelled ? 'You can edit your question and try again.' : timedOut ? 'The request timed out. Please try again.' : error.message;
   document.querySelectorAll('[data-ask-stage]').forEach(item => { item.classList.remove('active'); item.removeAttribute('aria-current'); });
  } finally {
   clearInterval(ticker); clearTimeout(deadline); el('ask-wait-note').textContent = '';
   el('ask-elapsed').textContent = Math.floor((performance.now() - started) / 1000) + 's total';
   form.setAttribute('aria-busy', 'false'); el('question').readOnly = false;
   button.disabled = false; button.textContent = 'Ask Compass →'; cancel.hidden = true; busy = false;
  }
 });
})();
