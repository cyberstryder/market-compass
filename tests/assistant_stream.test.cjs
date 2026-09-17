const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readEvents} = require('../compass/static/assistant.js');

function response(text, chunkSize = 3) {
 const bytes = new TextEncoder().encode(text); let at = 0;
 return new Response(new ReadableStream({pull(controller) {
  if (at === bytes.length) {controller.close(); return;}
  controller.enqueue(bytes.slice(at, at + chunkSize)); at = Math.min(at + chunkSize, bytes.length);
 }}));
}

test('stages and Unicode survive byte boundaries before the final answer', async () => {
 const expected = [{type:'stage',stage:'gathering'}, {type:'context',label:'TSLA · VWAP'}, {type:'heartbeat'}, {type:'complete',answer:'Data ✓'}];
 const got = [];
 await readEvents(response(expected.map(e => JSON.stringify(e)).join('\n')), e => got.push(e));
 assert.deepEqual(got, expected);
});

test('missing terminal event fails instead of showing completion', async () => {
 await assert.rejects(readEvents(response('{"type":"stage","stage":"generating"}\n'), () => {}), /Connection ended/);
});

test('HTTP rejection and streamed provider errors remain errors', async () => {
 await assert.rejects(readEvents(new Response('{"detail":"Sign in required"}', {status:401}), () => {}), /Sign in required/);
 await assert.rejects(readEvents(response('{"type":"error","detail":"Provider unavailable"}\n'), event => {throw new Error(event.detail);}), /Provider unavailable/);
});

test('consumer failure closes the underlying stream without retrying', async () => {
 let cancelled = false;
 const source = new ReadableStream({start(c) {c.enqueue(new TextEncoder().encode('{"type":"context"}\n'));}, cancel() {cancelled = true;}});
 await assert.rejects(readEvents(new Response(source), () => {throw new Error('cancelled');}), /cancelled/);
 assert.equal(cancelled, true);
});
