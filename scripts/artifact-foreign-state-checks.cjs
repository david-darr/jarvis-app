// Runs the production draft + streaming modules without a browser. Electron
// smoke checks cover DOM/layout; this checks transport ownership and races.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../static/js', name), 'utf8');
const moduleURL = text => 'data:text/javascript;base64,' + Buffer.from(text).toString('base64');
const tick = () => new Promise(resolve => setImmediate(resolve));

async function main() {
  const apiURL = moduleURL(`
    export class Node {
      constructor(tag, attrs = {}, children = []) { this.tag = tag; this.children = []; this.events = {}; Object.assign(this, attrs); this.textContent = attrs.text || ''; this.append(...children); }
      append(...children) { this.children.push(...children); }
      replaceChildren(...children) { this.children = children; }
      addEventListener(name, callback) { this.events[name] = callback; }
      remove() { this.removed = true; }
      click() { if (!this.disabled) this.onclick?.(); }
    }
    export const el = (tag, attrs, children) => new Node(tag, attrs, children);
    export const toast = () => {};
  `);
  const stream = await import(moduleURL(source('chatStream.js')));
  const streamURL = moduleURL(source('chatStream.js')); // ESM caches the identical URL.
  const reviewURL = moduleURL(source('artifactReview.js').replace("'./api.js'", JSON.stringify(apiURL)));
  const foreign = await import(moduleURL(source('artifactForeignReview.js')
    .replace("'./api.js'", JSON.stringify(apiURL))
    .replace("'./chatStream.js'", JSON.stringify(streamURL))
    .replace("'./artifactReview.js'", JSON.stringify(reviewURL))));
  const { el } = await import(apiURL);
  const requests = [], controllers = [];
  global.fetch = async (url, options) => {
    assert.equal(url, '/api/chat/stream');
    requests.push(JSON.parse(options.body));
    return new Response(new ReadableStream({ start(controller) { controllers.push(controller); } }));
  };
  const emit = async (index, payload, close = false) => {
    controllers[index].enqueue(new TextEncoder().encode('data: ' + JSON.stringify(payload) + '\n\n'));
    if (close) controllers[index].close();
    await tick(); await tick();
  };
  let saved = 0, refreshed = 0, opened = 0, edited;
  const make = (id, restored = {}) => {
    const tab = { sessionId: id, container: el('div'), review: { edit(item) { edited = item; } } };
    const controls = foreign.mountForeignReview(tab, { title: () => 'Origin chat', openChat: () => opened++, save: () => saved++, completed: () => refreshed++ }, restored);
    const actions = tab.foreignBar.children[3].children;
    return { tab, controls, send: actions[1], cancel: actions[2], open: actions[3], status: actions[0] };
  };
  const comment = text => ({ kind: 'artifact_comment', url: '/generated-files/012345abcdef_plan.md', comment: text, picks: ['md:lines=1-1'], label: 'plan.md · lines 1–1' });
  const first = make('origin'); first.controls.resume();
  const item = comment('Revise the plan.');
  assert.equal(first.tab.addForeignComment(item), true);
  assert.equal(first.tab.foreignBar.children[0].textContent, '1 comment for Origin chat');
  first.tab.foreignBar.children[1].children[0].children[0].click(); assert.equal(edited, item);
  first.send.click();
  assert.equal(requests.length, 1);
  assert.deepEqual(requests[0], { session_id: 'origin', message: 'Please apply these review comments.', attachment_ids: [], references: [item] });
  assert.equal(first.tab.foreign.comments.length, 0);
  assert.equal(first.status.textContent, 'Working…');
  await emit(0, { permission: { id: 'p1' } });
  assert.equal(first.status.textContent, 'Needs approval'); first.open.click(); assert.equal(opened, 1);
  stream.clearPermission('origin', 'wrong-id'); assert.equal(first.status.textContent, 'Needs approval');
  stream.clearPermission('origin', 'p1'); assert.equal(first.status.textContent, 'Working…');
  await emit(0, { error: 'fixture failed' }, true);
  assert.equal(first.status.textContent, 'Failed: fixture failed');
  assert.deepEqual(first.tab.foreign.comments, [item]); assert.equal(first.send.textContent, 'Retry');
  first.send.click(); assert.equal(requests.length, 2); assert.deepEqual(requests[1].references, [item]);
  await emit(1, { done: true }, true); assert.equal(first.status.textContent, 'Done');

  // Busy submission waits, permits cancellation, and never changes origin.
  stream.startTurn('origin', 'Origin', 'Busy turn', []);
  first.tab.addForeignComment(comment('Queued')); first.send.click();
  assert.equal(requests.length, 3); assert.equal(first.tab.foreign.queued, true);
  assert.match(first.status.textContent, /is busy/);
  assert.equal(first.tab.addForeignComment(comment('Blocked edit')), false);
  first.cancel.click(); await emit(2, { done: true }, true);
  assert.equal(requests.length, 3); assert.equal(first.tab.foreign.comments.length, 1);

  // Two tabs queued to the same origin serialize on the real registry.
  stream.startTurn('origin', 'Origin', 'Another busy turn', []);
  first.send.click();
  const second = make('origin'); second.controls.resume(); second.tab.addForeignComment(comment('Second tab')); second.send.click();
  await emit(3, { done: true }, true);
  assert.equal(requests.length, 5); assert.equal(second.tab.foreign.queued, true);
  assert.equal(requests[4].session_id, 'origin'); assert.equal(requests[4].references[0].comment, 'Queued');
  await emit(4, { done: true }, true);
  assert.equal(requests.length, 6); assert.equal(requests[5].references[0].comment, 'Second tab');
  await emit(5, { done: true }, true);

  // Restarted queues wait for saved-tab validation before they can send.
  const restored = make('restored-origin', { comments: [comment('Restored')], note: 'Optional note', queued: true });
  await tick(); assert.equal(requests.length, 6);
  restored.controls.resume(); await tick();
  assert.equal(requests.length, 7); assert.equal(requests[6].session_id, 'restored-origin'); assert.equal(requests[6].message, 'Optional note');
  await emit(6, { done: true }, true);
  const discarded = make('discarded-origin', { comments: [comment('Do not send')], queued: true });
  discarded.controls.resume(); discarded.controls.dispose(); await tick();
  assert.equal(requests.length, 7, 'Disposal cancels pending automatic sends');
  for (const current of [first, second, restored]) current.controls.dispose();
  assert.ok(saved > 0 && refreshed > 0);
  console.log('PASS: foreign draft ownership, request metadata, approval updates, failure/retry, busy cancellation, multi-tab queue races and validated restart.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
