// Shared desktop/phone checks, invoked by ui-smoke with its synthetic server.
module.exports = async ({ js, waitFor, capture, navigate, overflow, reads, writes, label }) => {
  const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
  const requestAfter = async (records, before, predicate) => {
    for (let i = 0; i < 80; i++) {
      if (records.slice(before).some(predicate)) return;
      await delay(50);
    }
    // Name what arrived instead, so a failure says which step went wrong.
    throw new Error('Google UI action did not send its own expected request ('
      + predicate.toString().slice(0, 160) + '); after it: ' + JSON.stringify(records.slice(before).slice(-6)).slice(0, 600));
  };
  const assert = require('node:assert/strict');
  const writeStart = writes.length;
  await navigate('library', { section: 'google' });
  await waitFor("document.querySelectorAll('.google-file-row').length === 8");
  await waitFor("document.querySelector('.google-thumbnail img')?.naturalWidth > 0");
  assert.equal(await js("document.querySelectorAll('.google-rail .google-section').length"), 5);
  assert.ok(await js("document.querySelector('.google-storage').textContent.includes('2.0 GB')"));
  assert.deepEqual(await overflow(), [], label + ' Drive grid fits');
  await capture(label + '-drive-grid');
  await js("[...document.querySelectorAll('.google-toolbar button')].find(b=>b.textContent==='List').click()");
  await waitFor("!document.querySelector('.google-file-list').classList.contains('google-grid')");
  await capture(label + '-drive-list');
  for (const [section, count] of [['shared', 1], ['starred', 1], ['recent', 9], ['trash', 1], ['my_drive', 8]]) {
    const before = reads.length;
    const index = ['my_drive', 'shared', 'starred', 'recent', 'trash'].indexOf(section);
    await js(`document.querySelectorAll('.google-section')[${index}].click()`);
    await requestAfter(reads, before, r => r.path === '/api/google/drive/files' && r.query.section === section);
    await waitFor(`document.querySelectorAll('.google-file-row').length === ${count}`);
  }
  await js("[...document.querySelectorAll('.google-toolbar button')].find(b=>b.textContent==='Grid').click()");
  const folderRead = reads.length;
  await js("document.querySelector('[data-file-id=drive-folder] .google-file-open').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))");
  await requestAfter(reads, folderRead, r => r.path === '/api/google/drive/files' && r.query.parent === 'drive-folder');
  await waitFor("!!document.querySelector('[data-file-id=drive-child]')");
  assert.ok(await js("document.querySelector('.google-breadcrumbs').textContent.includes('Project files')"));
  const rootRead = reads.length;
  await js("document.querySelector('.google-breadcrumbs button').click()");
  await requestAfter(reads, rootRead, r => r.path === '/api/google/drive/files' && !r.query.parent);
  await waitFor("document.querySelectorAll('.google-file-row').length === 8");
  await js("document.querySelectorAll('.google-file-row input')[1].click(); document.querySelectorAll('.google-file-row input')[2].click()");
  assert.ok(await js("document.querySelector('.google-selection-toolbar').textContent.includes('2 selected')"));
  await capture(label + '-drive-selection');
  await js("[...document.querySelectorAll('.google-selection-toolbar button')].find(b=>b.textContent==='Clear selection').click()");
  for (const [id, ready, route] of [
    ['doc', "!!document.querySelector('.google-reader')", '/api/google/drive/files/drive-doc/preview'],
    ['slides', "!!document.querySelector('.google-viewer canvas')", '/api/google/drive/files/drive-slides/preview'],
    ['pdf', "!!document.querySelector('.google-viewer canvas')", '/api/google/drive/files/drive-pdf/preview'],
    ['sheet', "document.querySelectorAll('.google-sheet-grid input').length === 240", '/api/google/sheets/drive-sheet'],
    ['form', "document.querySelector('.google-form-items')?.textContent.includes('What would you improve?')", '/api/google/forms/drive-form'],
    ['image', "document.querySelector('.google-viewer img')?.naturalWidth > 0", '/api/google/drive/files/drive-image/preview'],
    ['text', "document.querySelector('.google-text-preview')?.textContent.includes('print(focus)')", '/api/google/drive/files/drive-text/preview'],
  ]) {
    const before = reads.length;
    await js(`document.querySelector('[data-file-id=drive-${id}] .google-file-open').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
    await requestAfter(reads, before, r => r.path === route).catch(error => { throw new Error(`opening the ${id}: ${error.message}`); });
    await waitFor(ready);
    if (id === 'doc') assert.equal(await js("document.querySelector('.google-reader').getAttribute('sandbox')"), '');
    await js("document.querySelector('.google-detail').scrollIntoView({block:'start'})");
    await capture(label + '-drive-viewer-' + id);
    await js("[...document.querySelectorAll('.google-detail-head button')].find(b=>b.textContent==='Close details').click(); document.querySelector('.google-toolbar').scrollIntoView({block:'start'})");
  }
  await js("document.querySelector('[data-file-id=drive-doc]').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true}))");
  await waitFor("!!document.querySelector('.google-context-menu')");
  assert.ok(await js("document.querySelector('.google-context-menu').textContent.includes('Share')"));
  await capture(label + '-drive-context-menu');
  await js("[...document.querySelectorAll('.google-context-menu button')].find(b=>b.textContent==='Close menu').click()");
  await navigate('library', { section: 'documents' });
  await navigate('calendar');
  await waitFor("document.querySelectorAll('.cal-calendar-choice').length === 2");
  const initialMonthRead = reads.length;
  await js("[...document.querySelectorAll('.cal-nav button')].find(b=>b.textContent==='Month').click()");
  await requestAfter(reads, initialMonthRead, r => r.path === '/api/calendar/events');
  await waitFor("document.querySelectorAll('.cal-day').length === 42");
  await waitFor("!!document.querySelector('.cal-event-pill.cal-google-event')");
  await capture(label + '-calendar-month-google');
  const weekRead = reads.length;
  await js("[...document.querySelectorAll('.cal-nav button')].find(b=>b.textContent==='Week').click()");
  await requestAfter(reads, weekRead, r => r.path === '/api/calendar/events');
  await waitFor("document.querySelectorAll('.cal-week-day').length === 7 && !!document.querySelector('.cal-today-line')");
  await capture(label + '-calendar-week-google');
  assert.deepEqual(await overflow(), [], label + ' Week scrolls within Calendar');
  const settingsWrite = writes.length;
  await js("document.querySelector('.cal-calendar-choice input').click()");
  await requestAfter(writes, settingsWrite, w => w.path === '/api/google/calendar/settings' && w.method === 'PATCH');
  const restoreWrite = writes.length;
  await waitFor("!document.querySelector('.cal-calendar-choice input').disabled");
  await js("document.querySelector('.cal-calendar-choice input').click()");
  await requestAfter(writes, restoreWrite, w => w.path === '/api/google/calendar/settings' && w.method === 'PATCH');
  await waitFor("!document.querySelector('.cal-calendar-choice input').disabled");
  await js("document.querySelector('.cal-create-bar button').click()");
  await waitFor("!!document.querySelector('.cal-event-dialog')");
  assert.equal(await js("document.querySelector('.cal-event-dialog select').value"), 'demo@example.com');
  assert.equal(await js("document.querySelectorAll('.cal-event-dialog select option').length"), 3);
  await capture(label + '-calendar-create-picker');
  const createWrite = writes.length;
  await js(`document.querySelector('.cal-event-dialog input[aria-label="Event title"]').value='Smoke Google event'; document.querySelector('.cal-event-dialog').requestSubmit()`);
  await requestAfter(writes, createWrite, w => w.path === '/api/google/calendar/action' && JSON.parse(w.body).action === 'create');
  await waitFor("!document.querySelector('.cal-event-dialog')");
  const created = writes.slice(createWrite).find(w => w.path === '/api/google/calendar/action');
  assert.equal(JSON.parse(created.body).calendar_id, 'demo@example.com');
  await waitFor("[...document.querySelectorAll('.cal-day-panel .card')].some(c=>c.textContent.includes('Smoke Google event'))");
  await js("[...document.querySelectorAll('.cal-day-panel .card')].find(c=>c.textContent.includes('Smoke Google event')).querySelector('button').click()");
  await waitFor("!!document.querySelector('.cal-event-dialog')");
  assert.ok(await js("document.querySelector('.cal-event-dialog select').disabled"));
  const editWrite = writes.length;
  await js(`document.querySelector('.cal-event-dialog input[aria-label="Event title"]').value='Smoke edited event'; document.querySelector('.cal-event-dialog').requestSubmit()`);
  await requestAfter(writes, editWrite, w => w.path === '/api/google/calendar/action' && JSON.parse(w.body).action === 'update');
  const edited = JSON.parse(writes.slice(editWrite).find(w => w.path === '/api/google/calendar/action').body);
  await waitFor("!document.querySelector('.cal-event-dialog') && [...document.querySelectorAll('.cal-day-panel .card')].some(c=>c.textContent.includes('Smoke edited event'))");
  await js(`[...document.querySelectorAll('.cal-day-panel .card')].find(c=>c.textContent.includes('Smoke edited event')).querySelector('button[aria-label="Delete event"]').click()`);
  await waitFor("!!document.querySelector('.confirm-panel')");
  const deleteWrite = writes.length;
  await js("document.querySelector('.confirm-panel .btn.danger').click()");
  await requestAfter(writes, deleteWrite, w => w.path === '/api/google/calendar/action' && JSON.parse(w.body).action === 'delete' && JSON.parse(w.body).event_id === edited.event_id);
  await waitFor("![...document.querySelectorAll('.cal-day-panel .card')].some(c=>c.textContent.includes('Smoke edited event'))");
  const monthRead = reads.length;
  await js("[...document.querySelectorAll('.cal-nav button')].find(b=>b.textContent==='Month').click()");
  await requestAfter(reads, monthRead, r => r.path === '/api/calendar/events');
  await waitFor("document.querySelectorAll('.cal-day').length === 42");
  writes.splice(writeStart);
};
