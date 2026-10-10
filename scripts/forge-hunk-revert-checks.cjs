const assert = require('node:assert/strict');

// Called with the two-hunk Changes diff open, once per viewport.
module.exports = async ({ js, waitFor, capture, writes, reads, demoState, label, forgeSessionId }) => {
  const start = writes.length;
  const base = '/api/forge/sessions/' + forgeSessionId;
  const postHunks = () => writes.slice(start).filter(w => w.path === base + '/revert-hunk');
  const buttons = '.forge-pane .forge-revert-hunk';
  try {
    await waitFor(`document.querySelectorAll('${buttons}').length === 2`);
    assert.ok(await js(`[...document.querySelectorAll('${buttons}')].every(b => b.tagName === 'BUTTON' && b.tabIndex === 0 && b.getAttribute('aria-label') === 'Revert this change in src/garden.js')`), label + ' accessible hunk buttons');
    await js("window.__hunkReviews = 0; window.__hunkReviewListener = () => window.__hunkReviews++; document.addEventListener('kairos:forge-review', window.__hunkReviewListener)");
    await js(`document.querySelector('${buttons}').focus(); document.querySelector('${buttons}').click()`);
    await waitFor("!!document.querySelector('.confirm-panel')");
    assert.ok(await js("document.querySelector('.confirm-panel').textContent.includes('src/garden.js, line 1')"), label + ' names file and range');
    assert.equal(postHunks().length, 0, label + ' asks before posting');
    assert.ok(await js(`[...document.querySelectorAll('${buttons}, .forge-revert')].every(b => b.disabled)`), label + ' pending revert disables controls');
    await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Cancel').click()");
    await waitFor(`!document.querySelector('.confirm-panel') && !document.querySelector('${buttons}').disabled`);
    assert.equal(postHunks().length, 0, label + ' cancel keeps both hunks');
    const readStart = reads.length;
    await js("window.__hunkReviews = 0");
    await js(`document.querySelector('${buttons}').click()`);
    await waitFor("!!document.querySelector('.confirm-panel')");
    await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Revert hunk').click()");
    await waitFor(`document.querySelectorAll('${buttons}').length === 1 && !document.querySelector('${buttons}').disabled && window.__hunkReviews > 0`);
    assert.equal(postHunks().length, 1, label + ' exactly one hunk POST');
    assert.equal(postHunks()[0].method, 'POST');
    assert.deepEqual(JSON.parse(postHunks()[0].body), { path: 'src/garden.js', hunk_hash: '1'.repeat(64), confirmed: true }, label + ' exact hunk payload');
    assert.ok(reads.slice(readStart).some(r => r.path === base + '/changes'), label + ' reloads changes');
    assert.ok(await js("!document.querySelector('.forge-pane .forge-diff-viewer').textContent.includes('shortcut') && document.querySelector('.forge-change-row .forge-added').textContent === '+1'"), label + ' only selected hunk disappears and counts refresh');
    await capture(label + '-forge-hunk-reverted');

    demoState.forgeHunkConflict = true;
    const staleReads = reads.length;
    await js("window.__hunkReviews = 0");
    await js(`document.querySelector('${buttons}').click()`);
    await waitFor("!!document.querySelector('.confirm-panel')");
    assert.ok(await js("document.querySelector('.confirm-panel').textContent.includes('src/garden.js, line 4')"), label + ' second hunk range');
    await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Revert hunk').click()");
    await waitFor("[...document.querySelectorAll('.toast')].some(n => n.textContent === 'This change moved since you opened it. The view has been refreshed.')");
    await waitFor(`document.querySelector('${buttons}')?.dataset.hunkHash === '${'3'.repeat(64)}' && !document.querySelector('${buttons}').disabled && window.__hunkReviews > 0`);
    assert.equal(postHunks().length, 2, label + ' stale request posts once without retry');
    assert.deepEqual(JSON.parse(postHunks()[1].body), { path: 'src/garden.js', hunk_hash: '2'.repeat(64), confirmed: true });
    assert.ok(reads.slice(staleReads).some(r => r.path === base + '/changes'), label + ' stale view reloads');
    assert.equal(await js("[...document.querySelectorAll('.toast')].filter(n => n.textContent.includes('hunk changed')).length"), 0, label + ' no duplicate server toast');
    await capture(label + '-forge-hunk-stale');
  } finally {
    delete demoState.forgeHunkConflict;
    await js("document.removeEventListener('kairos:forge-review', window.__hunkReviewListener); delete window.__hunkReviewListener; delete window.__hunkReviews");
    // Keep these writes out of ui-smoke's unrelated global mutation assertions.
    writes.splice(start);
  }
};
