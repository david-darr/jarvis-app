// Isolated desktop/phone passes. Restore fixtures and writes before navigation checks.
const assert = require('node:assert/strict');

module.exports = async ({ js, win, waitFor, capture, delay, navigate, demoState, writes }) => {
  const start = writes.length;
  const saved = Object.fromEntries(['empty', 'isAdmin', 'sessionFields'].map(key => [key, demoState[key]]));
  const picker = "document.querySelector('.slash-command-picker')";
  const lastReply = "[...document.querySelectorAll('#chat-messages .msg.assistant .msg-body')].at(-1)?._rawText";
  const draft = async text => js(`(() => { const input=document.getElementById('chat-input'); input.focus(); input.value=${JSON.stringify(text)}; input.setSelectionRange(input.value.length,input.value.length); input.dispatchEvent(new Event('input',{bubbles:true})); })()`);
  const key = async key => js(`document.getElementById('chat-input').dispatchEvent(new KeyboardEvent('keydown',{key:${JSON.stringify(key)},bubbles:true,cancelable:true}))`);
  const send = async (text, expected) => {
    await draft(text);
    await js("document.getElementById('chat-send').click()");
    await waitFor(`(${lastReply} || '').includes(${JSON.stringify(expected)})`);
    // Wait for the command log, not just the rendered reply, before taking a write index.
    await delay(150);
    return js(lastReply);
  };
  try {
    for (const [label, width, height] of [['desktop', 1440, 900], ['mobile', 390, 844]]) {
      const writeIndex = writes.length;
      demoState.empty = false; demoState.isAdmin = true; demoState.sessionFields = {};
      win.setContentSize(width, height); await delay(100);
      await navigate('chat');
      await waitFor("!!document.getElementById('chat-input')");
      await draft('/');
      await waitFor(`${picker}?.hidden === false`);
      assert.equal(await js(`${picker}.getAttribute('role')`), 'listbox');
      assert.ok(await js("document.getElementById(document.getElementById('chat-input').getAttribute('aria-activedescendant'))?.getAttribute('role') === 'option'"), label + ' active option is accessible');
      assert.ok(await js(`(() => {const r=${picker}.getBoundingClientRect(); const m=document.querySelector('.mobile-menu-btn'); const mb=m && getComputedStyle(m).display!=='none' ? m.getBoundingClientRect().bottom : 0; return r.left>=0 && r.right<=innerWidth && r.top>=mb && r.bottom<=innerHeight;})()`), label + ' suggestions fit below the menu button');
      await capture(label + '-slash-suggestions');
      await key('ArrowDown'); await key('Enter');
      assert.equal(await js("document.getElementById('chat-input').value"), '/demo ', label + ' arrows and Enter insert without running');
      assert.equal(writes.length, writeIndex, label + ' suggestions never write');
      await draft('/com');
      assert.deepEqual(await js(`[...${picker}.querySelectorAll('.chat-reference-kind')].map(n=>n.textContent)`), ['/compact', '/computer']);
      await key('ArrowUp'); await key('Tab');
      assert.equal(await js("document.getElementById('chat-input').value"), '/computer ', label + ' Up wraps and Tab inserts');
      await draft('/mo'); await js(`${picker}.querySelector('button').click()`);
      assert.equal(await js("document.getElementById('chat-input').value"), '/model ', label + ' mouse inserts');
      await draft('/demo'); await key('Enter');
      await waitFor(`(${lastReply} || '').includes('A quick tour of Kairos')`);
      assert.ok(await js(`${picker}.hidden`), label + ' a fully typed command runs on the first Enter');
      await delay(150);
      await draft('/'); await key('Escape');
      assert.ok(await js(`${picker}.hidden && !document.getElementById('chat-input').hasAttribute('aria-activedescendant')`), label + ' Escape clears active option');
      for (const text of ['/help ', 'hello /help', '/does-not-exist']) {
        await draft(text); assert.ok(await js(`${picker}.hidden`), label + ' invalid suggestion draft closes');
      }
      const help = await send('/help', 'Type / to see commands as you type.');
      for (const category of ['Getting started', 'Chats', 'Go to', 'Agent', 'Models', 'Memory']) assert.ok(help.includes(category + ':'), label + ' help group ' + category);
      assert.ok(help.includes('Example: /rename Weekend plans') && help.includes('Keyboard shortcuts:'));
      assert.ok(!help.includes('—') && !help.includes('Odysseus'));
      assert.ok(await js("[...document.querySelectorAll('#chat-messages .msg.assistant .msg-body')].at(-1).querySelectorAll('li').length >= 30"), label + ' help renders as separate command and shortcut lines');
      await capture(label + '-slash-help');
      assert.ok((await send('/help compact', 'Usage: /compact')).includes('full conversation stays visible'));
      const noModel = 'No model chosen. Pick one from the menu above the chat box, or type /setup.';
      assert.equal(await send('/model', noModel), noModel, label + ' empty chat has no model');
      await send('/compact', 'Open or create a chat first');
      await send('/setup', 'Opened the model setup guide.');
      await waitFor("!!document.querySelector('.model-setup-panel [data-setup-kind=claude]')");
      assert.ok(await js("document.querySelector('.model-setup-panel').contains(document.activeElement)"), label + ' guide retains focus');
      await js("document.querySelector('.model-setup-panel [aria-label=Close]').click()");
      await draft('/shortcuts'); await js("document.getElementById('chat-send').click()");
      // Closing the desktop window only hides it, so scope to the Settings that is showing:
      // the phone page removes that hidden window while it loads.
      const page = (width > 768 ? '.settings-window' : '#view-content.settings-mobile-page') + ' .set-page[data-page=shortcuts]';
      const titles = `[...document.querySelectorAll(${JSON.stringify(page + ' .set-section-title')})].map(n=>n.textContent)`;
      await waitFor(`${titles}.length === 4`);
      assert.deepEqual(await js(titles), ['Anywhere', 'Composer', 'Chat', 'Documents']);
      assert.ok(await js(`document.querySelector(${JSON.stringify(page)}).textContent.includes('Shift + 6')`), label + ' Quick Entry default');
      assert.ok(await js("document.documentElement.scrollWidth <= innerWidth"), label + ' shortcuts fit');
      await capture(label + '-settings-shortcuts');
      if (width > 768) await js("document.querySelector('.settings-window button[title=Close]').click()");
      await navigate('chat', { sessionId: 's1' });
      await send('/find', 'Opened find in chat.');
      assert.ok(await js("!document.querySelector('.chat-find').hidden && document.activeElement === document.querySelector('.chat-find-input')"), label + ' slash find uses and focuses existing bar');
      await js("document.querySelector('.chat-find-input').dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}))");
      await send('/model', 'Model connection: Claude.');
      demoState.sessionFields.s1 = { model_endpoint_id: 'm1', model_override: 'exact-model-id' };
      assert.ok((await send('/model', 'Model: exact-model-id')).includes('Model connection: Claude.'));
      demoState.sessionFields.s1 = { model_endpoint_id: null };
      assert.equal(await send('/model', noModel), noModel, label + ' saved chat with no model');
      demoState.isAdmin = false;
      await navigate('chat'); await draft('/');
      assert.ok(await js(`![...${picker}.querySelectorAll('.chat-reference-kind')].some(n=>n.textContent==='/forge')`), label + ' non-admin suggestions hide Forge');
      const publicHelp = await send('/help', 'Type / to see commands as you type.');
      assert.ok(!publicHelp.includes('/forge'), label + ' non-admin help hides Forge');
      assert.ok((await send('/help forge', 'Unknown command: /forge.')).startsWith('Unknown command: /forge.'));
      await navigate('home');
      await delay(200);
      writes.splice(writeIndex);
    }
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete demoState[key]; else demoState[key] = value;
    }
    win.setContentSize(1440, 900);
    await navigate('home'); await delay(200);
    writes.splice(start);
  }
};
