const assert = require('node:assert/strict');

module.exports = async function checkArtifactReview({ js, win, waitFor, capture, delay, navigate, writes }) {
  const active = '.artifact-tab-panel:not([hidden])';
  const open = name => js(`import('/static/js/chatContent.js').then(m => m.openArtifact('s1', '/generated-files/012345abcdef_${name}', '${name}')).then(() => null)`);
  const click = selector => js(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const add = async text => {
    await js(`(() => { const box = document.querySelector('${active} .artifact-comment-box'); box.querySelector('textarea').value = ${JSON.stringify(text)}; box.querySelector('button').click(); })()`);
    await delay(40);
  };
  const point = (selector, x = .5, y = .5) => js(`(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return {x:r.left+r.width*${x}, y:r.top+r.height*${y}}; })()`);
  const mouse = (type, p, modifiers = 0) => win.webContents.debugger.sendCommand('Input.dispatchMouseEvent', { type, x:p.x, y:p.y, button:type === 'mouseMoved' ? 'none' : 'left', buttons:type === 'mouseReleased' ? 0 : 1, clickCount:1, modifiers });
  const pick = async (selector, modifiers = 0, x = .5) => { const p = await point(selector, x); await mouse('mouseMoved', p); await mouse('mousePressed', p, modifiers); await mouse('mouseReleased', p, modifiers); };
  const drag = async (from, to) => { await mouse('mouseMoved', from); await mouse('mousePressed', from); await mouse('mouseMoved', to); await mouse('mouseReleased', to); await delay(80); };
  win.setContentSize(1920, 1080); await navigate('chat', { sessionId: 's1' });
  await open('workspace.pptx'); await click(`${active} [aria-label="Go to slide 2"]`);
  await click(`${active} .artifact-select`);
  await pick(`${active} [data-pick="pptx:slide=2;shape=2"]`);
  await pick(`${active} [data-pick="pptx:slide=2;shape=3"]`, 2); // Control
  assert.equal(await js(`document.querySelectorAll('${active} .artifact-picked').length`), 2);
  await js(`document.querySelector('${active} .artifact-comment-box textarea').value = 'Make the title and plan clearer.'`);
  await capture('artifact-selection-comment-box');
  await add('Make the title and plan clearer.');
  assert.equal(await js("document.querySelector('.artifact-comment-open span').textContent"), 'workspace.pptx · Slide 2 · 2 items');
  assert.equal(await js(`document.querySelectorAll('${active} .artifact-commented').length`), 2);
  await click(`${active} [aria-label="Go to slide 3"]`);
  await click('.artifact-comment-open');
  // The chip reopens the pane asynchronously (dynamic import, then the slide).
  await waitFor(`!!document.querySelector('${active} [data-pick="pptx:slide=2;shape=2"]')`);
  assert.equal(await js(`document.querySelector('${active} .artifact-comment-box textarea').value`), 'Make the title and plan clearer.');
  await js(`document.querySelector('${active} .artifact-comment-box textarea').dispatchEvent(new KeyboardEvent('keydown', { key:'Escape', bubbles:true, cancelable:true }))`);
  assert.equal(await js(`document.querySelector('${active}').dataset.selecting`), 'false');
  await capture('artifact-comment-chip');
  await open('review-notes.txt'); await click(`${active} .artifact-select`);
  await pick(`${active} [data-pick="text:line=1"]`, 0, .1); await pick(`${active} [data-pick="text:line=2"]`, 8, .1); // Shift
  await add('Rewrite these lines.');
  assert.ok(await js("[...document.querySelectorAll('.artifact-comment-open span')].some(n => n.textContent === 'review-notes.txt · lines 1–2')"));
  await open('review-grid.xlsx'); await click(`${active} .artifact-select`);
  await drag(await point(`${active} [data-pick="xlsx:sheet=Sheet1;cell=B2"]`), await point(`${active} [data-pick="xlsx:sheet=Sheet1;cell=C3"]`));
  assert.equal(await js(`document.querySelectorAll('${active} td.artifact-picked').length`), 4);
  await add('Change the selected cells.');
  assert.ok(await js("[...document.querySelectorAll('.artifact-comment-open span')].some(n => n.textContent === 'review-grid.xlsx · Sheet1!B2:C3')"));
  await open('review-image.png'); await waitFor(`document.querySelector('${active} [data-pick="image"] img')?.naturalWidth === 320`);
  await click(`${active} .artifact-select`);
  await drag(await point(`${active} [data-pick="image"] img`, .1, .2), await point(`${active} [data-pick="image"] img`, .6, .45));
  assert.equal(await js(`document.querySelectorAll('${active} .artifact-review-rect.artifact-picked').length`), 1);
  await add('Adjust this region.');
  await click('.artifact-comment-chip:last-child > button[aria-label^="Remove"]');
  assert.equal(await js(`document.querySelectorAll('${active} .artifact-review-rect').length`), 0, 'Removing a chip removes its outline');
  await drag(await point(`${active} [data-pick="image"] img`, .1, .2), await point(`${active} [data-pick="image"] img`, .6, .45));
  await add('Adjust this region.');
  const start = writes.length;
  await js("(() => { const input = document.querySelector('#chat-input'); input.value = 'Apply these review comments.'; input.dispatchEvent(new Event('input')); document.querySelector('#chat-send').click(); })()");
  for (let n = 0; n < 80 && !writes.slice(start).some(w => w.path === '/api/chat/stream'); n++) await delay(50);
  const request = JSON.parse(writes.slice(start).find(w => w.path === '/api/chat/stream')?.body || '{}');
  const refs = request.references?.filter(r => r.kind === 'artifact_comment');
  assert.equal(refs?.length, 4);
  assert.deepEqual(refs[0].picks, ['pptx:slide=2;shape=2', 'pptx:slide=2;shape=3']);
  assert.deepEqual(refs[1].picks, ['text:lines=1-2']);
  assert.deepEqual(refs[2].picks, ['xlsx:sheet=Sheet1;cell=B2:C3']);
  assert.match(refs[3].picks[0], /^image;rect=0\.1000,0\.2000,0\.5000,0\.2500$/);
  for (const ref of refs) assert.deepEqual(Object.keys(ref).sort(), ['comment', 'kind', 'label', 'picks', 'url']);
  await waitFor("!document.querySelector('.artifact-comment-chip')");
  assert.equal(await js(`document.querySelectorAll('${active} .artifact-commented').length`), 0);
  await js("import('/static/js/chatContent.js').then(m => m.closeArtifact())");
  await navigate('home'); win.setContentSize(1440, 900);
  // This check's own send is the only write it makes; later checks assert
  // that plain navigation writes nothing, so leave the log as it was.
  writes.splice(start);
  console.log('PASS: artifact review selection, reopening, line/cell ranges, fractional rectangles and request metadata.');
};
