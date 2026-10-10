const assert = require('node:assert/strict');

// Runs at ui-smoke's composer handoff, once for each viewport.
module.exports = async ({ js, waitFor, capture, writes, demoState, label }) => {
  demoState.forgeNoCommits.fp1 = true;
  await js("document.querySelector('[aria-label=\"Repository for lifespan\"]').parentElement.dispatchEvent(new Event('change'))");
  await waitFor("document.querySelector('.forge-lifespan .forge-widget-body').textContent.includes('No commits yet.')");
  const start = writes.length;
  await js("document.querySelector('#forge-message').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))");
  await waitFor("!!document.querySelector('.forge-first-commit:not([hidden]) button:not(:disabled)')");
  assert.equal(await js("document.querySelector('.forge-first-commit p').textContent"),
    'Kairos garden has no commits yet. Make a first commit to start a session.', label + ' plain inline message');
  assert.equal(await js("document.querySelector('.forge-first-commit button').textContent"), 'Make the first commit');
  assert.ok(await js("document.querySelector('.forge-first-commit').nextElementSibling.classList.contains('chat-input-bottom')"), label + ' recovery is inside the composer above the pills');
  assert.equal(await js("document.querySelector('#forge-message').value"), 'Build the Forge fixture');
  assert.ok(await js("![...document.querySelectorAll('.toast')].some(n => /needed a single revision|fatal:|Could not start session|has no commits yet/.test(n.textContent))"), label + ' no failure toast');
  await capture(label + '-forge-first-commit');
  for (const choice of ['Cancel', 'Approve']) {
    await js("document.querySelector('.forge-first-commit button').click()");
    await waitFor("!!document.querySelector('.forge-app-approval')");
    assert.ok(demoState.forgeNoCommits.fp1, label + ' waits for approval');
    assert.ok(await js("document.querySelector('.forge-app-approval pre').textContent.includes('Project folder:') && document.querySelector('.forge-app-approval pre').textContent.includes('Files to commit: 2')"), label + ' folder and count');
    assert.equal(await js("document.querySelector('#forge-message').value"), 'Build the Forge fixture');
    assert.ok(await js("document.querySelector('#forge-message').readOnly && document.querySelector('.forge-composer .chat-input-bottom').inert && document.querySelector('.forge-composer [aria-label=\"Start session\"]').disabled"), label + ' composer is locked while starting');
    await js(`[...document.querySelectorAll('.forge-app-approval button')].find(b => b.textContent === ${JSON.stringify(choice)}).click()`);
    if (choice === 'Cancel') {
      await waitFor("!document.querySelector('.forge-app-approval') && !document.querySelector('.forge-first-commit button').disabled");
      assert.ok(await js("!document.querySelector('#forge-message').readOnly && !document.querySelector('.forge-composer .chat-input-bottom').inert && !document.querySelector('.forge-composer [aria-label=\"Start session\"]').disabled"), label + ' cancellation unlocks the preserved composer');
      assert.ok(demoState.forgeNoCommits.fp1, label + ' denial commits nothing');
      assert.equal(writes.slice(start).filter(w => w.path === '/api/forge/sessions').length, 1, label + ' denial does not start session');
    }
  }
  await waitFor("document.getElementById('view-content').dataset.view === 'forgeShell' && !!document.querySelector('.forge-transcript-layout')");
  assert.ok(!demoState.forgeNoCommits.fp1, label + ' approval commits');
  const attempts = writes.slice(start).filter(w => w.path === '/api/forge/sessions');
  assert.equal(attempts.length, 2, label + ' retries automatically');
  assert.deepEqual(JSON.parse(attempts[0].body), JSON.parse(attempts[1].body), label + ' keeps task and all settings');
  assert.ok(await js("!/fatal:|needed a single revision/.test(document.body.textContent)"), label + ' no raw Git text');
};
