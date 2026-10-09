// Called by ui-smoke with the same isolated fixtures and per-pass write index.
const assert = require('node:assert/strict');

module.exports = async ({ js, win, waitFor, capture, delay, navigate, demoState, writes }) => {
  const saved = Object.fromEntries(['empty', 'modelSetup', 'setupConnections', 'setupHoldSignIn', 'setupWaiting', 'setupWaitReads', 'setupLocal', 'setupInstall']
    .map(key => [key, demoState[key]]));
  const click = async (text, scope = '.model-setup') => {
    await js(`(() => { const b=[...document.querySelectorAll(${JSON.stringify(scope + ' button')})].find(b => b.textContent === ${JSON.stringify(text)}); if (!b) throw new Error('Missing model setup button'); b.click(); })()`);
  };
  const choose = async kind => {
    await waitFor(`!!document.querySelector('.model-setup [data-setup-kind=${kind}] button')`);
    await js(`document.querySelector('.model-setup [data-setup-kind=${kind}] button').click()`);
  };
  const text = wanted => `document.querySelector('.model-setup-screen')?.textContent.includes(${JSON.stringify(wanted)})`;
  const startWizard = async () => {
    await js(`(async () => { const overlay=document.getElementById('onboarding-overlay'); overlay.classList.remove('hidden'); await import('/static/js/onboarding.js').then(m => m.run(overlay, () => overlay.classList.add('hidden'))); })()`);
    await click('Get Started', '.onboarding-card');
    await waitFor("document.querySelector('.onboarding-card h2')?.textContent === 'Choose how Kairos thinks' && !!document.querySelector('[data-setup-kind=claude]')");
  };
  const setupWrites = writes.length;
  try {
    for (const [label, width, height] of [['desktop', 1440, 900], ['mobile', 390, 844]]) {
      const writeIndex = writes.length;
      win.setContentSize(width, height); await delay(100);
      demoState.empty = false; demoState.setupConnections = [];
      demoState.modelSetup = { claude: { available: true, source: 'bundled', signed_in: true },
        codex: { installed: false, version: null, path: null, signed_in: false }, node: { npm: true } };
      demoState.setupHoldSignIn = true;
      await startWizard();
      assert.ok(await js("document.querySelector('[data-setup-kind=claude]').textContent.includes('Already set up')"), label + ' live existing connection');
      await capture(label + '-onboarding-model-setup');
      await js("document.querySelector('.model-setup-words').open = true");
      assert.ok(await js("document.querySelector('.model-setup-words').textContent.includes('pay-per-use')"));
      await capture(label + '-model-setup-words');
      await js("document.querySelector('.model-setup-words').open = false");
      demoState.empty = true; demoState.modelSetup.claude.signed_in = false;
      await startWizard();
      await choose('claude');
      await click('Sign in with Claude');
      await waitFor(text('Waiting for you to finish signing in'));
      await capture(label + '-model-setup-claude');
      await click('Cancel waiting');
      await waitFor("!!document.querySelector('[data-setup-kind=claude]')");
      assert.equal(writes.slice(writeIndex).filter(w => w.path === '/api/model-setup/connections').length, 0, label + ' cancel does not connect');
      await choose('claude'); await click('Sign in with Claude');
      await waitFor(text('Waiting for you to finish signing in'));
      demoState.modelSetup.claude.signed_in = true;
      await waitFor(text('Claude Code is connected'));
      await click('Done');
      await choose('codex'); await click('Install Codex');
      await waitFor("!!document.querySelector('.permission-dialog[open]')");
      assert.ok(await js("document.querySelector('.permission-dialog').textContent.includes('npm install -g @openai/codex')"));
      await capture(label + '-model-setup-codex');
      await js("document.querySelector('.permission-dialog [data-choice=reject]').click()");
      await waitFor(text('Installation cancelled'));
      assert.equal(demoState.modelSetup.codex.installed, false, label + ' denied install');
      await click('Try again');
      await waitFor("!!document.querySelector('.permission-dialog[open]')");
      await js("document.querySelector('.permission-dialog [data-choice=once]').click()");
      await waitFor("document.querySelector('.model-setup-progress')?.textContent.includes('Installing Codex')");
      await waitFor(text('Sign in with ChatGPT'));
      await click('Sign in with ChatGPT');
      await waitFor(text('Waiting for you to finish signing in'));
      demoState.modelSetup.codex.signed_in = true;
      await waitFor(text('ChatGPT with Codex is connected'));
      await click('Done'); await choose('api');
      await js("document.querySelector('.model-setup select').value = 'anthropic'");
      await click('Continue');
      await js("document.querySelector('.model-setup input[type=password]').value = 'fixture-key'");
      await click('Test and save');
      await waitFor(text('API key saved'));
      assert.ok(writes.slice(writeIndex).some(w => w.path === '/api/model-setup/connections' && JSON.parse(w.body).provider === 'anthropic'), label + ' API test/save');
      await click('Done'); await choose('local');
      await waitFor(text('0.4 GB'));
      await click('Download in the background');
      await waitFor("!!document.querySelector('.confirm-panel')");
      await js("[...document.querySelectorAll('.confirm-panel button')].find(b=>b.textContent==='Download model').click()");
      await waitFor(text('Downloading in the background'));
      await click('Return to setup');
      await waitFor("!!document.querySelector('[data-setup-kind=local]')");
      assert.equal(demoState.setupLocal.status, 'downloading', label + ' local keeps downloading after return');
      assert.ok(writes.slice(writeIndex).some(w => w.path.includes('/api/cookbook/engine/setup/')), label + ' Cookbook owns the local handoff');
      // Finish a skipped wizard, then open from both empty views and the picker.
      demoState.setupConnections = [];
      await click('Skip for now', '.onboarding-card');
      await waitFor("document.querySelector('.onboarding-card h2')?.textContent === 'Your vault'");
      await click('Continue', '.onboarding-card'); await click('Skip / Continue', '.onboarding-card');
      await click('Skip / Continue', '.onboarding-card'); await click('Enter Kairos', '.onboarding-card');
      await waitFor("document.getElementById('onboarding-overlay').classList.contains('hidden')");
      await navigate('home');
      await waitFor("!!document.querySelector('.model-setup-empty button')");
      await click('Set up a model', '.model-setup-empty');
      await waitFor("!!document.querySelector('.model-setup-panel [data-setup-kind=claude]')");
      await js("document.querySelector('.model-setup-panel [aria-label=Close]').click()");
      await navigate('chat');
      await waitFor("!!document.querySelector('#chat-main > .model-setup-empty button')");
      await click('Set up a model', '#chat-main > .model-setup-empty');
      await waitFor("!!document.querySelector('.model-setup-panel')");
      await js("document.querySelector('.model-setup-panel [aria-label=Close]').click()");
      await js("document.getElementById('model-picker-btn').click()");
      await waitFor("!document.getElementById('model-picker-menu').classList.contains('hidden')");
      await click('Set up a model', '#model-picker-menu');
      await waitFor("!!document.querySelector('.model-setup-panel')");
      assert.ok(await js("document.documentElement.scrollWidth <= window.innerWidth"), label + ' guide fits viewport');
      await js("document.querySelector('.model-setup-panel [aria-label=Close]').click()");
      if (width <= 768) {
        await js("document.querySelector('.sidebar-settings-btn').click()");
        await waitFor("!!document.querySelector('#view-content.settings-mobile-page .settings-nav-item[data-section=add-models]')");
      } else await js("import('/static/js/views/settings.js').then(m => m.openSettingsWindow('add-models'))");
      await js("document.querySelector('.settings-nav-item[data-section=add-models]').click()");
      await waitFor("[...document.querySelectorAll('button')].some(b => b.textContent === 'Help me set up a model')");
      await click('Help me set up a model', 'body');
      await waitFor("!!document.querySelector('.model-setup-panel')");
      await js("document.querySelector('.model-setup-panel [aria-label=Close]').click()");
      if (width <= 768) await navigate('chat');
      else await js("document.querySelector('.settings-window button[title=Close]').click()");
      assert.ok(writes.slice(writeIndex).some(w => w.path === '/api/model-setup/sign-in/claude'), label + ' this pass launches Claude');
      assert.ok(writes.slice(writeIndex).some(w => w.path === '/api/model-setup/sign-in/codex'), label + ' this pass launches Codex');
    }
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete demoState[key]; else demoState[key] = value;
    }
    win.setContentSize(1440, 900);
    await js("document.getElementById('onboarding-overlay').classList.add('hidden')");
    await delay(300); writes.splice(setupWrites);
  }
};
