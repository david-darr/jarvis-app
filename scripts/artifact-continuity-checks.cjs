const assert = require('node:assert/strict');

module.exports = async function checkArtifactContinuity({ js, win, waitFor, capture, base, delay, navigate, writes, demoState, reads }) {
  const active = '.chat-layout .artifact-tab-panel:not([hidden])';
  const title = 'A clearer direction for the workspace';
  const click = selector => js(`document.querySelector(${JSON.stringify(selector)}).click()`);
  const reset = () => js("import('/static/js/chatContent.js').then(m => m.closeArtifact())");
  const select = index => js(`document.querySelectorAll('.chat-layout .artifact-tab-label')[${index}].click()`);
  const add = async text => {
    await js(`(() => { const button = document.querySelector('${active} .artifact-select'); if (button.getAttribute('aria-pressed') !== 'true') button.click(); })()`);
    await js(`document.querySelector('${active} [data-pick="md:lines=1-1"]').dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true }))`);
    await js(`(() => { const box = document.querySelector('${active} .artifact-comment-box'); box.querySelector('textarea').value = ${JSON.stringify(text)}; box.querySelector('button').click(); })()`);
    await delay(50);
  };
  const send = () => js(`document.querySelector('${active} .artifact-foreign-actions .primary').click()`);
  const count = () => js("document.querySelectorAll('.chat-layout .artifact-tab-label').length");
  const heading = () => js(`document.querySelector('${active} .artifact-foreign-bar strong').textContent`);
  const assertS2 = async () => assert.equal(await js("document.querySelector('.chat-title').textContent"), 'Planning the week ahead');
  const startWrites = writes.length;
  win.setContentSize(1920, 1080);
  await navigate('chat', { sessionId: 's1' }); await reset();
  await click('.artifact-card'); await waitFor(`!!document.querySelector('${active} .artifact-document')`);
  await js(`window.retainedArtifact = document.querySelector('${active} .artifact-document'); document.querySelector('${active} .artifact-preview').scrollTop = 180`);
  await js("import('/static/js/views/chat.js').then(m => m.openSessionById('s2'))");
  assert.equal(await count(), 1);
  assert.equal(await js(`document.querySelector('${active} .artifact-origin').textContent`), `from ${title}`);
  assert.equal(await js(`document.querySelector('${active} .artifact-origin').hidden`), false);
  assert.ok(await js(`document.querySelector('${active} .artifact-document') === window.retainedArtifact`));
  // Let the restored scroll offset's scroll event land first; app menus
  // close on any outside scroll, as a person can't click within that frame.
  await delay(100);
  await click('.artifact-tab-add');
  await waitFor("[...document.querySelectorAll('.artifact-open-menu .custom-select-item')].some(n => n.textContent === 'weekly-plan.md')");
  assert.equal(await js("document.querySelector('.artifact-open-menu .custom-select-heading').textContent"), 'This chat');
  await js("document.querySelector('.artifact-open-menu input').value='weekly'; document.querySelector('.artifact-open-menu input').dispatchEvent(new Event('input')); document.querySelector('.artifact-open-menu .custom-select-item').click()");
  await waitFor(`document.querySelector('${active} h2')?.textContent === 'weekly-plan.md' && !!document.querySelector('${active} .artifact-document')`);
  assert.equal(await count(), 2);
  await select(0);
  await capture('artifact-two-chats-origin');
  await js("document.querySelector('.chat-pane-resizer').dispatchEvent(new KeyboardEvent('keydown', { key:'ArrowRight', bubbles:true }))");
  await delay(250);
  const width = await js("document.querySelector('#chat-main').getBoundingClientRect().width");
  await click('.artifact-maximize');
  await navigate('home');
  assert.equal(await js("!!document.querySelector('.chat-layout .artifact-viewer')"), false);
  await navigate('chat', { sessionId: 's2' });
  assert.equal(await count(), 2);
  assert.equal(await js("document.querySelector('.chat-layout').classList.contains('pane-maximized')"), true);
  assert.ok(await js(`document.querySelector('${active} .artifact-document') === window.retainedArtifact && document.querySelector('${active} .artifact-preview').scrollTop === 180`));
  await click('.artifact-maximize'); await delay(250);
  assert.ok(Math.abs(await js("document.querySelector('#chat-main').getBoundingClientRect().width") - width) < 2);
  await add('Make this plan clearer.');
  assert.equal(await js("document.querySelectorAll('.artifact-comment-chip').length"), 0);
  assert.equal(await heading(), `1 comment for ${title}`);
  assert.equal(await js(`document.querySelector('${active} .artifact-comment-actions .primary').textContent`), 'Add comment');
  assert.equal(await js(`document.querySelectorAll('${active} .artifact-commented').length`), 1);
  await js(`document.querySelector('${active} [aria-label="Note for the chat"]').value = 'Please revise the plan.'; document.querySelector('${active} [aria-label="Note for the chat"]').dispatchEvent(new Event('input'))`);
  await capture('artifact-foreign-comment-bar');
  // Drafts restore; only the selected tab has a renderer after restart.
  const readStart = reads.length;
  await win.loadURL(base); await waitFor("[...document.querySelectorAll('.dashboard-stat:not([data-stat=waiting]) .dashboard-stat-value')].filter(n => n.textContent !== '…').length === 4");
  await navigate('chat', { sessionId: 's2' });
  await waitFor(`!!document.querySelector('${active} .artifact-document')`);
  assert.equal(await count(), 2); assert.equal(await heading(), `1 comment for ${title}`);
  await delay(250);
  assert.ok(Math.abs(await js("document.querySelector('#chat-main').getBoundingClientRect().width") - width) < 2, 'Restart restores the chat width');
  assert.equal(await js(`document.querySelector('${active} [aria-label="Note for the chat"]').value`), 'Please revise the plan.');
  assert.equal(reads.slice(readStart).filter(read => read.path === '/api/chat/artifacts/content' && read.query.url.endsWith('weekly-plan.md')).length, 0);
  await send();
  await waitFor(`document.querySelector('${active} .artifact-foreign-actions [role="status"]')?.textContent === 'Done'`);
  const sent = JSON.parse(writes.slice(startWrites).find(write => write.path === '/api/chat/stream').body);
  assert.equal(sent.session_id, 's1'); assert.equal(sent.message, 'Please revise the plan.');
  assert.deepEqual(sent.attachment_ids, []); assert.equal(sent.references.length, 1);
  assert.equal(sent.references[0].kind, 'artifact_comment');
  assert.equal(sent.references[0].url, '/generated-files/012345abcdef_project-brief.md');
  assert.deepEqual(Object.keys(sent.references[0]).sort(), ['comment', 'kind', 'label', 'picks', 'url']);
  await assertS2();
  // Hold actual chatStream readers open, exercising busy, approval, failure,
  // retry and completion through the same subscriptions used in production.
  await js(`(() => {
    window.artifactRealFetch = window.fetch; window.artifactRequests = []; window.artifactStreams = [];
    window.fetch = (url, options) => {
      if (url !== '/api/chat/stream') return window.artifactRealFetch(url, options);
      window.artifactRequests.push(JSON.parse(options.body));
      return Promise.resolve(new Response(new ReadableStream({ start(controller) { window.artifactStreams.push(controller); } }), { headers: {'Content-Type':'text/event-stream'} }));
    };
    window.artifactEmit = (index, payload, close = false) => { const controller = window.artifactStreams[index]; controller.enqueue(new TextEncoder().encode('data: ' + JSON.stringify(payload) + '\\n\\n')); if (close) controller.close(); };
  })()`);
  try {
    await js("import('/static/js/chatStream.js').then(m => { m.startTurn('s1', 'Origin', 'Already working', []); })");
    await add('Queued change.'); await send();
    assert.match(await js(`document.querySelector('${active} .artifact-foreign-actions [role="status"]').textContent`), /is busy; your comments will send when it finishes/);
    assert.equal(await js("window.artifactRequests.length"), 1);
    await js(`document.querySelector('${active} .artifact-foreign-actions button:nth-child(3)').click()`);
    assert.equal(await heading(), `1 comment for ${title}`, 'Cancelling preserves the draft');
    await send();
    await js("window.artifactEmit(0, {done:true}, true)");
    await waitFor("window.artifactRequests.length === 2 && window.artifactStreams.length === 2");
    assert.equal(await js("window.artifactRequests[1].session_id"), 's1');
    assert.equal(await js("window.artifactRequests[1].references[0].comment"), 'Queued change.');
    await js("window.artifactEmit(1, {permission:{id:'artifact-approval', description:'Edit the plan'}})");
    await waitFor(`document.querySelector('${active} .artifact-foreign-actions [role="status"]').textContent === 'Needs approval'`);
    assert.equal(await js(`document.querySelector('${active} .artifact-foreign-actions button:last-child').hidden`), false);
    await js("window.artifactEmit(1, {error:'Review fixture failure'}, true)");
    await waitFor(`document.querySelector('${active} .artifact-foreign-actions .primary').textContent === 'Retry'`);
    assert.equal(await heading(), `1 comment for ${title}`);
    assert.match(await js(`document.querySelector('${active} .artifact-foreign-actions [role="status"]').textContent`), /Review fixture failure/);
    await send(); await waitFor("window.artifactRequests.length === 3 && window.artifactStreams.length === 3");
    demoState.artifactReviewNewer = true;
    await navigate('home'); await js("window.artifactEmit(2, {done:true}, true)");
    await navigate('chat', { sessionId: 's2' });
    await waitFor(`[...document.querySelectorAll('${active} .artifact-toolbar button')].some(button => button.textContent === 'Newer version' && !button.hidden)`);
    assert.equal(await js(`document.querySelector('${active} .artifact-toolbar button:not(.artifact-select)').textContent`), 'Newer version');
    assert.equal(await js(`document.querySelector('${active} .artifact-foreign-actions [role="status"]').textContent`), 'Done');
    await assertS2();
  } finally {
    await js("window.fetch = window.artifactRealFetch; delete window.artifactRealFetch");
    demoState.artifactReviewNewer = false;
  }
  // Background opens preserve the selected document.
  await js("document.querySelectorAll('.chat-layout .artifact-tab-close')[1].click()");
  await js("document.querySelector('.artifact-card').dispatchEvent(new MouseEvent('click', {ctrlKey:true, bubbles:true, cancelable:true}))");
  assert.equal(await count(), 2);
  assert.equal(await js(`document.querySelector('${active} h2').textContent`), 'project-brief.md', 'Control-click keeps the selected tab');
  // Replacing the visible pane only parks documents; the header restores it.
  await js("document.querySelectorAll('.chat-layout .artifact-tab-close')[1].click()");
  await click('.chat-files-toggle'); await waitFor("!!document.querySelector('.chat-file-row')");
  await js("document.querySelector('.chat-file-row').dispatchEvent(new MouseEvent('auxclick', {button:1, bubbles:true, cancelable:true}))");
  assert.equal(await count(), 2);
  assert.equal(await js(`document.querySelector('${active} h2').textContent`), 'project-brief.md');
  await js("document.querySelectorAll('.chat-layout .artifact-tab-close')[1].click()");
  await click('.chat-files-toggle'); await waitFor("!!document.querySelector('.chat-file-row')");
  await click('.chat-files-group-heading button');
  assert.equal(await count(), 2, 'Open all adds the group without changing the active document');
  await click('.chat-files-toggle'); await waitFor("!!document.querySelector('.chat-file-row')");
  await click('.chat-documents-toggle'); assert.equal(await count(), 2);
  await click(`${active} .artifact-origin`);
  await waitFor("document.querySelector('.chat-title').textContent === 'A clearer direction for the workspace'");
  assert.equal(await count(), 2);
  await js("import('/static/js/views/chat.js').then(m => m.openSessionById('s2'))");
  // New-chat reset leaves the same documents; phones return with no overlay.
  await click('.chat-header-new'); assert.equal(await count(), 2);
  win.setContentSize(390, 844); await delay(200); await navigate('home'); await navigate('chat', {sessionId:'s2'});
  assert.equal(await js("!!document.querySelector('.chat-layout .artifact-viewer')"), false);
  await click('.chat-documents-toggle'); assert.equal(await count(), 2);
  win.setContentSize(1920, 1080); await delay(200);
  const stale = '/generated-files/012345abcdef_deleted.md'; demoState.staleArtifacts = [stale];
  await js(`(() => { const saved = JSON.parse(localStorage.getItem('jarvis:artifact-pane')); saved.tabs.push({sessionId:'s1', url:${JSON.stringify(stale)}, name:'deleted.md'}); const foreign = saved.tabs.find(t => t.sessionId === 's1' && t.url !== ${JSON.stringify(stale)}); foreign.comments = [{kind:'artifact_comment', url: foreign.url, comment:'Queued before restart', label: foreign.name, picks:['text:line=1']}]; foreign.queued = true; localStorage.setItem('jarvis:artifact-pane', JSON.stringify(saved)); })()`);
  await win.loadURL(base); await waitFor("[...document.querySelectorAll('.dashboard-stat:not([data-stat=waiting]) .dashboard-stat-value')].filter(n => n.textContent !== '…').length === 4"); await navigate('chat', {sessionId:'s2'});
  assert.equal(await count(), 2);
  // A send queued before a restart comes back as a draft and never fires on its own.
  const queuedWrites = writes.length;
  await waitFor("[...document.querySelectorAll('.artifact-foreign-bar .meta')].some(n => n.textContent.includes('Not sent: the app closed'))");
  await delay(300);
  assert.equal(writes.slice(queuedWrites).filter(w => w.path === '/api/chat/stream').length, 0, 'restored queued comments are not auto-sent');
  assert.ok(await js("[...document.querySelectorAll('.chat-layout .artifact-tab-label')].some(button => button.textContent === 'shared-notes.txt')"), 'Chat-file uploads survive restart too');
  assert.equal(await js("[...document.querySelectorAll('.toast')].filter(n => n.textContent.includes('1 saved document removed')).length"), 1);
  demoState.staleArtifacts = [];
  // Moving the parked DOM must keep an HTML iframe's browsing context too.
  await reset();
  await js("import('/static/js/chatContent.js').then(m => m.openArtifact('s1', '/generated-files/012345abcdef_preview.html', 'preview.html'))");
  await waitFor(`document.querySelector('${active} .artifact-frame')?.contentDocument?.body?.dataset.pick === 'html:path=body'`);
  await js(`window.retainedFrameDocument = document.querySelector('${active} .artifact-frame').contentDocument; null`);
  await navigate('home'); await navigate('chat', {sessionId:'s2'});
  assert.ok(await js(`document.querySelector('${active} .artifact-frame').contentDocument === window.retainedFrameDocument`), 'HTML browsing context survives parking');
  await reset(); await navigate('home'); win.setContentSize(1440, 900);
  writes.splice(startWrites);
  console.log('PASS: documents across chats/navigation/restart, origins, lazy restore, foreign sends/queue/approval/retry/versions, background opens and stale removal.');
};
