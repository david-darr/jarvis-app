// Real CRM renderer, isolated synthetic API. No user account or outside network.
const { app, BrowserWindow, session } = require('electron');
const { exitAfterFlush } = require('./electron-exit.cjs');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'data', 'ui-review', 'crm-' + Date.now());
const fixture = require('../demo/fixtures.js')({ state: { empty: false } }).fixture;
const errors = [], outside = [], writes = [];
const now = Date.now() / 1000;
const day = new Date().toISOString().slice(0, 10);
const original = { id: 'task1', owner: 'local', title: 'Send revised proposal', contact: 'customer@example.com', project: 'Proposal',
  priority: 'high', priority_reason: 'The proposal has a requested deadline.', status: 'active', due_date: day,
  deadline_text: 'Friday', deadline_kind: 'explicit', notes: '', source_id: 'source1', evidence: [
    { message_id: 'message1', quote: 'Please send the revised proposal by Friday.', label: 'Work inbox', sent_at: new Date().toISOString() }],
  overrides: [], snoozed_until: null, proposal: null };
const state = { tasks: [structuredClone(original)], sources: [{ id: 'source1', label: 'Work inbox', kind: 'email', enabled: true, last_scan_at: now }],
  settings: { endpoint_id: 'model1', timezone: 'America/New_York', review_all: false, auto_scan: false, lookback_days: 14, interval_minutes: 30 },
  runs: [], failed_messages: 0, can_connect: true, scanning: false };
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    res.setHeader('Content-Type', 'application/json');
    let value;
    if (url.pathname === '/api/system/custom-tabs') value = [{ id: 'crm', label: 'CRM', icon_svg: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/></svg>', user_tab: false, format: 'folder', view_url: '/tab-files/crm/view.js', style_url: '/tab-files/crm/view.css' }];
    else if (url.pathname === '/api/tab-crm/connections') value = { connections: [{ kind: 'email', id: 'email1', label: 'Work inbox' }], models: [{ id: 'model1', name: 'Local extraction', kind: 'local' }] };
    else if (url.pathname === '/api/agents') value = [];
    else if (url.pathname === '/api/tab-crm/messages/message1') value = { subject: 'Proposal', sender: 'customer@example.com', account: 'work@example.com',
      sent_at: new Date().toISOString(), body: 'Please send the revised proposal by Friday.\n<script>steal()</script><img src="https://example.com/tracker">', url: null };
    else if (url.pathname === '/api/tab-crm' && req.method === 'GET') value = state;
    else if (url.pathname.startsWith('/api/tab-crm/') && req.method !== 'GET') {
      let body = ''; for await (const chunk of req) body += chunk;
      const fields = body ? JSON.parse(body) : {};
      writes.push({ path: url.pathname, fields });
      if (url.pathname === '/api/tab-crm/tasks' && req.method === 'POST') {
        value = { ...original, ...fields, id: 'created-' + writes.length, evidence: [], source_id: null };
        state.tasks.push(value);
      } else if (url.pathname.startsWith('/api/tab-crm/tasks/')) {
        const id = url.pathname.split('/')[4];
        value = state.tasks.find((t) => t.id === id);
        Object.assign(value, fields);
      } else if (url.pathname === '/api/tab-crm/settings') value = Object.assign(state.settings, fields);
      else if (url.pathname === '/api/tab-crm/scan') value = { started: true };
      else value = { ok: true };
    } else {
      try { value = fixture(url); }
      catch (error) { errors.push(error.message); res.writeHead(404); value = { detail: 'Missing fixture' }; }
    }
    res.end(JSON.stringify(value)); return;
  }
  const tabFile = url.pathname.match(/^\/tab-files\/crm\/(view\.js|view\.css)$/);
  const file = tabFile ? path.join(root, 'tabs', 'crm', tabFile[1])
    : path.resolve(root, url.pathname === '/' ? 'static/index.html' : '.' + decodeURIComponent(url.pathname));
  if (!tabFile && !file.startsWith(path.join(root, 'static') + path.sep)) { res.writeHead(404); res.end(); return; }
  try {
    res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.jpg': 'image/jpeg', '.png': 'image/png' })[path.extname(file)] || 'application/octet-stream');
    res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:");
    res.end(fs.readFileSync(file));
  } catch { res.writeHead(404); res.end(); }
});
app.setPath('userData', path.join(root, 'data', 'crm-smoke-profile'));
app.commandLine.appendSwitch('force-device-scale-factor', '1');
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
app.whenReady().then(async () => {
  fs.mkdirSync(output, { recursive: true });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = 'http://127.0.0.1:' + server.address().port;
  session.defaultSession.webRequest.onBeforeRequest((details, cb) => {
    if (details.url.startsWith(base + '/')) return cb({});
    outside.push(details.url); cb({ cancel: true });
  });
  const win = new BrowserWindow({ width: 1440, height: 1000, show: false, useContentSize: true,
    webPreferences: { offscreen: true, contextIsolation: true, nodeIntegration: false } });
  win.webContents.on('console-message', (details) => { if (details.level === 'error') errors.push(details.message); });
  const js = (code) => win.webContents.executeJavaScript(code);
  const wait = async (condition) => {
    for (let n = 0; n < 80; n++) { if (await js(condition)) return; await delay(50); }
    throw new Error('Timed out: ' + condition);
  };
  const click = async (text) => { await js(`[...document.querySelectorAll('.crm-view button')].find(b=>b.textContent===${JSON.stringify(text)}).click()`); await delay(120); };
  const capture = async (name) => { await delay(250); fs.writeFileSync(path.join(output, name + '.png'), (await win.webContents.capturePage()).toPNG()); };
  const overflow = () => js(`document.querySelector('.crm-view').scrollWidth > document.querySelector('.crm-view').clientWidth + 2`);
  try {
    await win.loadURL(base);
    await wait("document.querySelectorAll('.dashboard-stat').length===4");
    await js("document.getElementById('onboarding-overlay')?.classList.add('hidden')");
    await js("import('/static/js/app.js').then(m=>m.switchTab('crm'))");
    await wait("document.querySelectorAll('.crm-task').length===1");
    assert.equal(await js("document.querySelector('.crm-task strong').textContent"), original.title);
    await capture('desktop-tasks');
    await js("document.querySelector('.crm-task').click()");
    await wait("!!document.querySelector('.crm-detail')");
    await capture('desktop-detail');
    await click('Open source');
    await wait("!!document.querySelector('.crm-source-modal')");
    assert.equal(await js("document.querySelectorAll('.crm-source-modal img,.crm-source-modal script').length"), 0);
    assert.ok(await js("document.querySelector('.crm-source-text').textContent.includes('<script>steal()</script>')"));
    await capture('desktop-source');
    await js("document.querySelector('.crm-source-modal button').click()");
    await click('Mark done');
    assert.equal(state.tasks[0].status, 'done');
    await click('Reopen');
    assert.equal(state.tasks[0].status, 'active');
    await click('Add task');
    await wait("document.querySelector('.crm-detail h3').textContent==='New task'");
    await js("document.querySelector('.crm-detail input').value='Call the client'; document.querySelector('.crm-detail input').dispatchEvent(new Event('input',{bubbles:true}))");
    await click('Save task');
    assert.equal(state.tasks.length, 2);
    assert.equal(state.tasks[1].title, 'Call the client');
    await click('Contacts');
    await capture('desktop-contacts');
    await click('Sources');
    await wait("document.querySelector('.crm-settings')");
    await capture('desktop-sources');
    await click('Save scanning settings');
    assert.ok(writes.some((w) => w.path === '/api/tab-crm/settings'));
    win.setContentSize(390, 844); await delay(200);
    await capture('mobile-sources');
    assert.equal(await overflow(), false, 'Sources fit a phone');
    await click('Tasks');
    await capture('mobile-tasks');
    assert.equal(await overflow(), false, 'Tasks fit a phone');
    await js("document.querySelector('.crm-task').click()");
    await capture('mobile-detail');
    assert.equal(await overflow(), false, 'Task editor fits a phone');
    state.tasks = []; state.sources = [];
    await js("import('/static/js/app.js').then(m=>m.switchTab('crm'))");
    await wait("document.querySelector('.crm-view').textContent.includes('Your follow-ups start here')");
    await capture('mobile-empty');
    assert.equal(errors.length, 0, errors.join('\n'));
    assert.equal(outside.length, 0, 'No outside requests, including tracking pixels');
    console.log(JSON.stringify({ ok: true, output, writes: writes.length, checks: ['render', 'source text safety', 'complete/reopen', 'manual create', 'settings save', 'desktop/mobile', 'empty state', 'offline'] }));
  } catch (error) {
    await capture('failure').catch(() => {});
    console.error(error.stack); console.error(JSON.stringify({ errors, outside, output }));
    process.exitCode = 1;
  } finally { win.destroy(); server.close(); exitAfterFlush(process.exitCode || 0); }
});
