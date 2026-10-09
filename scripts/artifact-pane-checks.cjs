const assert = require('node:assert/strict');

module.exports = async function checkArtifactPanes({ js, win, waitFor, capture, base, delay, navigate }) {
  const open = name => js(`import('/static/js/chatContent.js').then(m => m.openArtifact('s1', '/generated-files/012345abcdef_${name}', '${name}', document.querySelector('.artifact-card')))`);
  const close = () => js("import('/static/js/chatContent.js').then(m => m.closeArtifact())");
  const escape = () => js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))");
  const width = () => js("document.querySelector('#chat-main').getBoundingClientRect().width");
  const active = ".artifact-tab-panel:not([hidden])";
  const select = index => js(`document.querySelectorAll('.artifact-tab-label')[${index}].click()`);
  // The default chat column is 60% of the chat area; the document gets the rest (about 40%).
  const defaultWidth = () => js("Math.round(document.querySelector('.has-pane').clientWidth * 0.6)");
  const split = async expected => {
    await delay(250);
    if (expected === 'default') expected = await defaultWidth();
    assert.ok(Math.abs(await width() - expected) < 2, `Chat column is about ${expected}px`);
    assert.ok(await js("(() => { const a = document.querySelector('#chat-main').getBoundingClientRect(), b = document.querySelector('.artifact-panel').getBoundingClientRect(); return b.width > 0 && b.left >= a.right; })()"), 'The pane sits beside the chat');
    assert.ok(await js("document.querySelector('#chat-main').scrollWidth <= document.querySelector('#chat-main').clientWidth + 2 && document.querySelector('.chat-input-bar').scrollWidth <= document.querySelector('.chat-input-bar').clientWidth + 2"), 'The narrow chat and composer fit');
  };
  win.setContentSize(1920, 1080);
  await navigate('chat', { sessionId: 's1' });
  await waitFor("!!document.querySelector('.artifact-card')");
  await js("localStorage.removeItem('kairos:chat-pane-width'); if (document.querySelector('#chat-history-toggle').getAttribute('aria-expanded') === 'true') document.querySelector('#chat-history-toggle').click()");
  await js("document.querySelector('.artifact-card').click()");
  await waitFor("!!document.querySelector('.artifact-document')");
  await split('default');
  assert.equal(await js("document.querySelector('.chat-pane-resizer').getAttribute('role')"), 'separator');
  assert.equal(await js("document.querySelector('.chat-pane-resizer').getAttribute('aria-orientation')"), 'vertical');
  await capture('artifact-split-1920');

  // Real Chromium pointer input exercises pointer capture across the pane.
  const handle = await js("(() => { const r = document.querySelector('.chat-pane-resizer').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + 150 }; })()");
  const mouse = params => win.webContents.debugger.sendCommand('Input.dispatchMouseEvent', params);
  await mouse({ type: 'mousePressed', ...handle, button: 'left', clickCount: 1 });
  await mouse({ type: 'mouseMoved', x: handle.x + 96, y: handle.y, button: 'left', buttons: 1 });
  assert.equal(await js("getComputedStyle(document.querySelector('#chat-main')).transitionDuration"), '0s', 'Dragging has no width transition');
  await mouse({ type: 'mouseReleased', x: handle.x + 96, y: handle.y, button: 'left', clickCount: 1 });
  const dragged = (await defaultWidth()) + 96;
  await split(dragged);
  assert.equal(await js("localStorage.getItem('kairos:chat-pane-width')"), String(dragged));
  await close(); await open('project-brief.md'); await split(dragged);
  // Persistence also survives a fresh document/module instance.
  await win.loadURL(base);
  await waitFor("document.querySelectorAll('.dashboard-stat').length === 4");
  await navigate('chat', { sessionId: 's1' });
  await open('project-brief.md'); await split(dragged);
  const arrow = key => js(`document.querySelector('.chat-pane-resizer').dispatchEvent(new KeyboardEvent('keydown', { key: '${key}', bubbles: true, cancelable: true }))`);
  await arrow('ArrowLeft'); await split(dragged - 24);
  assert.equal(await js("document.querySelector('.chat-pane-resizer').getAttribute('aria-valuenow')"), String(dragged - 24));
  await js("document.querySelector('.chat-pane-resizer').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await split('default');
  // Keyboard resizing clamps at both limits and exposes the same ARIA range.
  await js("{ const h = document.querySelector('.chat-pane-resizer'); for (let i = 0; i < 100; i++) h.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true })); }");
  await delay(250); assert.ok(Math.abs(await width() - 380) < 2);
  assert.ok(await js("document.querySelector('#chat-main').scrollWidth <= document.querySelector('#chat-main').clientWidth + 2 && document.querySelector('.chat-input-bar').scrollWidth <= document.querySelector('.chat-input-bar').clientWidth + 2"), 'The minimum-width chat fits');
  await js("{ const h = document.querySelector('.chat-pane-resizer'); for (let i = 0; i < 100; i++) h.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true })); }");
  await delay(250);
  assert.ok(await js("Math.abs(document.querySelector('#chat-main').getBoundingClientRect().width - Number(document.querySelector('.chat-pane-resizer').getAttribute('aria-valuemax'))) < 2"));
  await js("document.querySelector('.chat-pane-resizer').dispatchEvent(new MouseEvent('dblclick'))");
  await split('default');

  await js("import('/static/js/sideChat.js').then(m => m.openSideChat('s3'))");
  await waitFor("!!document.querySelector('.side-chat')");
  await js("document.querySelector('.artifact-maximize').click()");
  assert.equal(await js("getComputedStyle(document.querySelector('#chat-main')).display"), 'none');
  assert.equal(await js("document.querySelector('#chat-main').inert"), true);
  assert.equal(await js("getComputedStyle(document.querySelector('.chat-pane-resizer')).display"), 'none');
  assert.equal(await js("getComputedStyle(document.querySelector('.side-chat')).display"), 'none', 'Maximize also hides the existing side chat');
  assert.ok(await js("Math.abs(document.querySelector('.artifact-panel').getBoundingClientRect().width - document.querySelector('.chat-layout').clientWidth) < 2"));
  await capture('artifact-maximized');
  await js("document.querySelector('.artifact-maximize').click()");
  assert.ok(await js("!document.querySelector('.side-chat').inert && getComputedStyle(document.querySelector('.side-chat')).display === 'flex'"), 'Restore preserves the side chat');
  await js("import('/static/js/sideChat.js').then(m => m.closeSideChat())");
  await split('default');
  await js("document.querySelector('.artifact-maximize').click()");
  await escape(); assert.ok(await js("!!document.querySelector('.artifact-panel') && !document.querySelector('#chat-main').inert"), 'First Escape restores');
  await escape(); assert.equal(await js("!!document.querySelector('.artifact-panel')"), false, 'Second Escape closes');

  await open('project-brief.md');
  await js(`(() => { const p = document.querySelector('${active}'); [...p.querySelectorAll('button')].find(b => b.textContent === 'Source').click(); p.querySelector('.artifact-preview').scrollTop = 180; window.artifactSource = p.querySelector('.artifact-source'); })()`);
  await open('review-notes.txt');
  assert.equal(await js("document.querySelectorAll('.artifact-tab-label').length"), 2);
  assert.ok(await js(`document.querySelector('${active}').textContent.includes('Review notes')`));
  await capture('artifact-two-tabs');
  await select(0);
  assert.ok(await js(`document.querySelector('${active} .artifact-source') === window.artifactSource && document.querySelector('${active} .artifact-preview').scrollTop === 180`), 'Source selection, DOM and scroll position survive switching');
  await open('review-notes.txt');
  assert.equal(await js("document.querySelectorAll('.artifact-tab-label').length"), 2, 'Reopening a URL activates without duplicating');
  await js("document.querySelectorAll('.artifact-tab-close')[0].click(); document.querySelector('.artifact-tab').dispatchEvent(new MouseEvent('auxclick', { button: 1, bubbles: true, cancelable: true }))");
  assert.equal(await js("!!document.querySelector('.artifact-panel') || document.querySelector('.chat-layout').classList.contains('has-pane')"), false, 'Closing the last tab removes pane and split');
  assert.equal(await js("document.activeElement.classList.contains('artifact-card')"), true, 'Close restores the opener');

  // Every existing renderer still builds into its own tab.
  await open('workspace.pptx');
  await js("document.querySelector('[aria-label=\"Next slide\"]').click()");
  await open('budget.xlsx');
  await js(`document.querySelector('${active} .office-tab:last-child').click()`);
  await select(0);
  assert.ok(await js(`document.querySelector('${active} .office-body .office-slide').textContent.includes('Next steps')`), 'Slide state survives switching');
  await select(1);
  assert.equal(await js(`document.querySelector('${active} .office-tab.active').textContent`), 'Actual', 'Sheet state survives switching');
  for (const [name, selector] of [['brief.docx', '.office-doc'], ['budget.csv', '.office-grid'], ['preview.html', '.artifact-frame'], ['reference.pdf', '.artifact-pdf-page'], ['download.zip', '.artifact-fallback'], ['workspace.png', '.artifact-image-viewport img']]) {
    await open(name); await waitFor(`!!document.querySelector('${active} ${selector}')`);
  }
  await waitFor(`document.querySelector('${active} .artifact-image-viewport img')?.naturalWidth === 1`);
  await select(5);
  await js(`document.querySelector('${active} [aria-label="Next PDF page"]').click()`);
  await waitFor(`document.querySelector('${active} .artifact-pdf-page')?.getAttribute('aria-label').includes('page 2 of 2')`);
  await select(1); await select(5);
  assert.ok(await js(`document.querySelector('${active} .artifact-pdf-page').getAttribute('aria-label').includes('page 2 of 2')`), 'PDF page state survives switching');
  await capture('artifact-pdf-tabs');
  await js("import('/static/js/appearance.js').then(m => m.updateAppearance({ mode: 'color', color: '#23302e' }))");
  await capture('artifact-dark-theme');
  await js("import('/static/js/appearance.js').then(m => m.updateAppearance({ mode: 'default' }))");
  await close();

  // Pane exclusivity includes the native browser bridge. This synthetic
  // bridge records closing without navigating to an external site.
  await js(`(() => { window.originalJarvis = window.jarvis; window.nativeBrowserClosed = 0; window.nativeBrowserVisibility = [];
    window.jarvis = { ...window.jarvis, browser: {
      setBounds() {}, setVisible(value) { window.nativeBrowserVisibility.push(value); }, close() { window.nativeBrowserClosed++; },
      onHostResized() { return () => {}; }, onState() { return () => {}; }, onError() { return () => {}; },
      open() { return Promise.resolve({ ok: true, url: 'https://example.com/' }); }
    } }; })()`);
  await js("import('/static/js/browserPane.js').then(m => m.openBrowser('https://example.com/'))");
  await split('default');
  const browserHandle = await js("(() => { const r = document.querySelector('.chat-pane-resizer').getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + 150 }; })()");
  await mouse({ type: 'mousePressed', ...browserHandle, button: 'left', clickCount: 1 });
  await mouse({ type: 'mouseMoved', x: browserHandle.x + 24, y: browserHandle.y, button: 'left', buttons: 1 });
  assert.deepEqual(await js("window.nativeBrowserVisibility"), [false], 'Native browser hides during a captured drag');
  await mouse({ type: 'mouseReleased', x: browserHandle.x + 24, y: browserHandle.y, button: 'left', clickCount: 1 });
  assert.deepEqual(await js("window.nativeBrowserVisibility"), [false, true], 'Native browser returns after the drag');
  await js("document.querySelector('.chat-pane-resizer').dispatchEvent(new MouseEvent('dblclick'))");
  await split('default');
  await open('project-brief.md');
  assert.equal(await js("window.nativeBrowserClosed"), 1);
  assert.equal(await js("document.querySelectorAll('.artifact-panel').length"), 1);
  await js("window.jarvis = window.originalJarvis; delete window.originalJarvis");
  await js("import('/static/js/chatFilesPane.js').then(m => m.openChatFiles('s1'))");
  await waitFor("!!document.querySelector('.chat-file-row')"); await split('default');
  await js("import('/static/js/chatComputerPane.js').then(m => m.openChatComputer('s1', { closed_at: 1, title: 'Saved computer' }))");
  await split('default');
  assert.equal(await js("document.querySelectorAll('.artifact-panel').length"), 1);
  await open('project-brief.md');

  win.setContentSize(1280, 900); await split('default'); await capture('artifact-split-1280');
  await js("document.querySelector('.artifact-maximize').click()");
  win.setContentSize(900, 900); await delay(300);
  assert.equal(await js("getComputedStyle(document.querySelector('.artifact-panel')).position"), 'absolute');
  assert.equal(await js("getComputedStyle(document.querySelector('.chat-pane-resizer')).display"), 'none');
  assert.equal(await js("getComputedStyle(document.querySelector('.artifact-maximize')).display"), 'none');
  assert.ok(await js("!document.querySelector('#chat-main').inert && document.querySelector('.artifact-panel').getBoundingClientRect().left < document.querySelector('#chat-main').getBoundingClientRect().right"), '900px restores the chat and overlays the pane');
  await capture('artifact-overlay-900');
  win.setContentSize(390, 844); await delay(250);
  assert.equal(await js("Math.round(document.querySelector('.artifact-panel').getBoundingClientRect().width)"), 390);
  assert.ok(await js("[...document.querySelectorAll('.artifact-viewer button')].filter(b => b.getBoundingClientRect().width > 0).every(b => b.getBoundingClientRect().width >= 44 && b.getBoundingClientRect().height >= 44)"), 'Phone controls have 44px targets');
  await js("document.documentElement.classList.add('electron-shell')");
  assert.equal(await js("document.querySelector('.artifact-panel').getBoundingClientRect().top"), 36, 'Phone preview clears the desktop titlebar');
  await capture('artifact-phone-titlebar');
  await js("document.documentElement.classList.remove('electron-shell')");
  win.setContentSize(1920, 1080);
  await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  assert.equal(await js("getComputedStyle(document.querySelector('#chat-main')).transitionDuration"), '0s');
  await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [] });
  // A chat switch keeps the documents and replaces their origin labels.
  await navigate('chat', { sessionId: 's2' });
  assert.equal(await js("document.querySelectorAll('.chat-layout .artifact-tab-label').length"), 1);
  assert.match(await js("document.querySelector('.artifact-origin').textContent"), /^from /);
  await close();
  // Storage failures cannot prevent opening, resizing, resetting or closing.
  await js("window.originalStorageGet = Storage.prototype.getItem; window.originalStorageSet = Storage.prototype.setItem; Storage.prototype.getItem = Storage.prototype.setItem = () => { throw new Error('Storage unavailable'); }; true");
  try {
    await open('review-notes.txt'); await arrow('ArrowRight'); await delay(250);
    assert.ok(Math.abs(await width() - ((await defaultWidth()) + 24)) < 2);
    await close();
  } finally {
    await js("Storage.prototype.getItem = window.originalStorageGet; Storage.prototype.setItem = window.originalStorageSet; delete window.originalStorageGet; delete window.originalStorageSet; localStorage.removeItem('kairos:chat-pane-width')");
  }
  win.setContentSize(1440, 900); await navigate('home');
  console.log('PASS: artifact split, pointer/keyboard resize and persistence, maximize/Escape, retained tabs/renderers, pane exclusivity and overlays.');
};
