// node scripts/swarm-smoke.cjs — isolated renderer, synthetic API, no live backend.
if (!process.versions.electron) {
  const { spawnSync } = require('node:child_process');
  const result = spawnSync(require('../electron/node_modules/electron'), [__filename],
    { stdio: 'inherit', windowsHide: true, timeout: 120000 });
  if (result.error) console.error(result.error.message);
  process.exit(result.status ?? 1);
}
const { app, BrowserWindow, session } = require('electron');
const { exitAfterFlush } = require('./electron-exit.cjs');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'data', 'swarm-review', 'run-' + Date.now());
const systems = [], streams = new Set(), errors = [], writes = [];
let nextId = 1, unavailable = false, snapshotDelay = 0, snapshotCalls = 0, streamOpens = 0;
const availability = { available: true, execution_available: false, blockers: [],
  detail: 'Agent execution is not connected yet. You can save systems, teams and ideas.' };
// What /api/models and /api/models/catalog really return in shape: masked
// connection records with no credentials, and public model/effort names.
const CONNECTIONS = [
  { id: 'claude-1', name: 'Claude Code', base_url: '', model: '', has_api_key: false, kind: 'claude_cli', num_ctx: null },
  { id: 'codex-1', name: 'Codex', base_url: '', model: '', has_api_key: false, kind: 'codex_cli', num_ctx: null },
];
const CATALOG = {
  claude_cli: [{ id: 'claude-opus-5', display_name: 'Opus 5', supported_efforts: [{ effort: 'low' }, { effort: 'high' }] }],
  codex_cli: [{ id: 'gpt-5.6-luna', display_name: 'Luna', supported_efforts: [{ effort: 'low' }, { effort: 'medium' }, { effort: 'high' }] },
    { id: 'gpt-5.6-sol', display_name: 'Sol', supported_efforts: [{ effort: 'low' }, { effort: 'medium' }, { effort: 'high' }] }],
};
const now = () => Date.now() / 1000;
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
function emit(company, kind = 'message.queued', duplicate = false) {
  if (!duplicate) {
    const event = { id: ++company.event_cursor, system_id: company.system.id, kind, data: {}, created_at: now(), version: 1 };
    company.pages.events.items.unshift(event); company.pages.events.total++;
  }
  const event = company.pages.events.items[0];
  for (const stream of streams) if (stream.company === company) stream.res.write(`id: ${event.id}\nevent: change\ndata: ${JSON.stringify(event)}\n\n`);
}
function createCompany(body) {
  const id = 'company-' + nextId++;
  const agents = [body.lead, ...body.specialists].map((a, i) => ({ ...a, id: id + '-agent-' + i,
    system_id: id, pool_id: id + '-pool', is_lead: i === 0 ? 1 : 0, enabled: 1 }));
  const company = { system: { id, name: body.name, mission: body.mission, owner: 'fixture', state: 'idle', revision: 0,
    reason: null, created_at: now(), configuration: { mode: body.mode, run_limit: body.run_limit,
      schedule: body.schedule, memory: body.memory } }, agents,
    budgets: [{ ...body.system_limit, scope: 'system', target: id, used: 0, held: 0 },
      { ...body.pool_limit, scope: 'pool', target: id + '-pool', used: 0, held: 0 },
      ...agents.map(a => ({ ...a.limit, scope: 'agent', target: a.id, used: 0, held: 0 }))],
    quotas: [], dependencies: [], allocation_ownership_unresolved: false, event_cursor: 0, availability,
    pages: Object.fromEntries(['tasks', 'messages', 'runs', 'attempts', 'events', 'checkpoints', 'shifts'].map(key => [key, { items: [], total: 0, offset: 0 }])) };
  emit(company, 'system.created'); systems.push(company); return company;
}
// Work fixture for the swimlane graph and the board: a dependency chain that
// crosses two lanes, one claimed attempt with recorded usage, and a cancelled
// task the board keeps behind its filter. Pages come back newest first, the
// same order the real store's rowid DESC paging returns.
function seedWork(company) {
  const [lead, backend] = company.agents;
  const task = (id, agent, objective, state, at) => ({ id, system_id: company.system.id, run_id: company.system.id + '-run',
    agent_id: agent.id, objective, state, revision: 1, checkpoint: '{}', result: null, created_at: at });
  const base = now();
  company.pages.tasks.items = [
    task('task-4', lead, 'Review the first release', 'review', base + 4),
    task('task-3', backend, 'Wire the form to the endpoint', 'ready', base + 3),
    task('task-2', backend, 'Build the registration endpoint', 'running', base + 2),
    task('task-5', backend, 'Abandoned spike', 'cancelled', base + 1),
    task('task-1', lead, 'Write the API contract', 'done', base),
  ];
  company.pages.tasks.total = 5;
  company.dependencies = [
    { task_id: 'task-2', depends_on: 'task-1' },
    { task_id: 'task-3', depends_on: 'task-2' },
    { task_id: 'task-4', depends_on: 'task-2' },
  ];
  company.pages.tasks.items.find(item => item.id === 'task-1').result = JSON.stringify({
    status: 'submitted', output: 'The contract is POST /users returning 201.', evidence: 'Checked against the spec.' });
  company.pages.attempts.items = [
    { id: 'attempt-2', system_id: company.system.id, task_id: 'task-4', agent_id: lead.id,
      state: 'unknown', used: 0, max_units: 25000, stopped: 0, error: 'company_pause' },
    { id: 'attempt-1', system_id: company.system.id, task_id: 'task-2', agent_id: backend.id,
      state: 'started', used: 1200, max_units: 25000, stopped: 0, error: null },
  ];
  company.pages.attempts.total = 2;
  company.unknown_attempts = company.pages.attempts.items.filter(item => item.state === 'unknown');
  company.pages.events.items.unshift({ id: ++company.event_cursor, system_id: company.system.id,
    kind: 'task.accepted', entity_id: 'task-1', data: { evidence: 'Matches the agreed contract.' },
    created_at: now(), version: 1 });
  company.pages.events.total++;
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'");
  const json = (value, code = 200) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value)); };
  if (url.pathname.startsWith('/api/') && !url.pathname.startsWith('/api/swarm')) {
    if (url.pathname === '/api/auth/status') return json({ auth_enabled: false, username: 'local', is_admin: true });
    if (url.pathname === '/api/settings') return json({ onboarding_complete: true, developer_mode_enabled: false });
    if (url.pathname === '/api/system/custom-tabs') return json([{ id: 'swarm', label: 'Custom collision', view_url: '/must-not-load.js' }]);
    if (url.pathname === '/api/system/status') return json({ vault_ok: true, scheduler_running: true, enabled_task_count: 0, discord_connected_bots: [], model_endpoint_count: 0 });
    if (url.pathname === '/api/models') return json(CONNECTIONS);
    if (url.pathname === '/api/models/catalog') return json(CATALOG);
    if (url.pathname === '/api/models/usage') return json({});
    if (url.pathname === '/api/projects') return json([{ id: 'project-1', name: 'Launch memory' }]);
    return json([]);
  }
  if (url.pathname.startsWith('/api/swarm')) {
    if (unavailable) return json({ detail: 'Fixture storage unavailable' }, 503);
    if (url.pathname === '/api/swarm/status') return json(availability);
    if (url.pathname === '/api/swarm/pools') return json(systems.map(c => ({ id: c.agents[0].pool_id, name: c.system.name + ' allocation' })));
    if (url.pathname === '/api/swarm/draft-team') {
      let raw = ''; for await (const chunk of req) raw += chunk;
      const asked = JSON.parse(raw || '{}');
      writes.push({ path: url.pathname, body: asked });
      if (!asked.description) return json({ detail: 'Describe the goal first.' }, 422);
      return json({ rationale: 'Research, then write.',
        lead: { name: 'Mo', role: 'Producer', instructions: 'Coordinates the work.' },
        specialists: [{ name: 'Rae', role: 'Researcher', instructions: 'Finds the angles.' },
                      { name: 'Kit', role: 'Writer', instructions: 'Drafts the script.' }] });
    }
    let body = {};
    if (req.method !== 'GET') { let data = ''; for await (const chunk of req) data += chunk; body = JSON.parse(data || '{}'); writes.push({ path: url.pathname, body, method: req.method }); }
    if (url.pathname === '/api/swarm/systems') {
      if (req.method === 'POST') return json({ id: createCompany(body).system.id }, 201);
      return json({ items: systems.map(c => ({ ...c.system, active_tasks: 0 })), total: systems.length, offset: 0 });
    }
    const segments = url.pathname.split('/').filter(Boolean);
    const company = systems.find(c => c.system.id === segments[3]);
    if (!company) return json({ detail: 'Not found' }, 404);
    const action = segments[4];
    if (!action) {
      if (req.method === 'DELETE') {
        if (company.system.state !== 'archived') return json({ detail: 'Archive the company before deleting it' }, 409);
        if (body.confirmation !== company.system.name) return json({ detail: 'Type the company name exactly to delete it' }, 409);
        systems.splice(systems.indexOf(company), 1);
        return json({ status: 'deleted', id: company.system.id });
      }
      if (req.method === 'PATCH') {
        assert.equal(body.expected_revision, company.system.revision);
        company.system.name = body.name; company.system.mission = body.mission;
        company.system.configuration = { mode: body.mode, run_limit: body.run_limit,
          schedule: body.schedule, memory: body.memory };
        Object.assign(company.budgets.find(item => item.scope === 'system' && item.target === company.system.id), body.system_limit);
        for (const change of body.pool_limits || []) {
          const budget = company.budgets.find(item => item.scope === 'pool' && item.target === change.id);
          assert.ok(budget, 'edited allocation belongs to this company');
          Object.assign(budget, change.limit);
        }
        company.system.revision++;
        return json({ id: company.system.id });
      }
      snapshotCalls++; if (snapshotDelay) await wait(snapshotDelay);
      return json({ ...company, availability: { ...availability, ...company.availability },
        unknown_attempts: company.pages.attempts.items.filter(item => item.state === 'unknown'),
        conclusion: company.conclusion || null, cycles: company.cycles || 0 });
    }
    if (action === 'events') {
      streamOpens++;
      res.writeHead(200, { 'Content-Type': 'text/event-stream' }); res.write(': connected\n\n');
      const stream = { res, company }; streams.add(stream); req.on('close', () => streams.delete(stream));
      const after = Math.max(Number(url.searchParams.get('after') || 0), Number(req.headers['last-event-id'] || 0));
      for (const event of [...company.pages.events.items].reverse()) if (event.id > after) res.write(`id: ${event.id}\nevent: change\ndata: ${JSON.stringify(event)}\n\n`);
      return;
    }
    if (req.method === 'POST' && action === 'messages') {
      const message = { id: 'message-' + nextId++, system_id: company.system.id, body: body.body, sender_id: null,
        recipient_id: company.agents[0].id, read_at: null, created_at: now() };
      company.pages.messages.items.unshift(message); company.pages.messages.total++;
      emit(company); return json({ id: message.id, status: 'queued' }, 202);
    }
    if (req.method === 'POST' && action === 'attempts' && segments[6] === 'reconcile') {
      if (!body.evidence || !body.evidence.trim()) return json({ detail: 'Describe what you checked before reconciling' }, 422);
      const attempt = company.pages.attempts.items.find(item => item.id === segments[5]);
      if (!attempt || attempt.state !== 'unknown') return json({ detail: 'That worker does not need reconciliation' }, 409);
      attempt.state = 'cancelled';
      const held = company.pages.tasks.items.find(item => item.id === attempt.task_id);
      if (held) held.state = 'ready';
      emit(company, 'attempt.reconciled');
      return json({ attempt_id: attempt.id, status: 'reconciled' });
    }
    if (req.method === 'POST' && action === 'checkpoints') {
      const checkpoint = { id: 'checkpoint-' + nextId++, cursor: company.event_cursor, created_at: now() };
      company.pages.checkpoints.items.unshift(checkpoint); company.pages.checkpoints.total++;
      return json(checkpoint, 201);
    }
    if (req.method === 'POST') {
      if (['start', 'resume'].includes(action) && !body.spend_confirmed) return json({ detail: 'Confirm provider spending' }, 409);
      company.system.state = { start: 'active', resume: 'active', pause: 'paused', stop: 'stopped', archive: 'archived', restore: 'stopped' }[action];
      company.system.reason = JSON.stringify(['manual_' + action]); company.system.revision++;
      emit(company, 'system.' + action); return json({ status: company.system.state });
    }
    if (company.pages[action === 'activity' ? 'events' : action]) return json(company.pages[action === 'activity' ? 'events' : action]);
    return json({ detail: 'Unknown fixture endpoint' }, 404);
  }
  if (url.pathname === '/full-app') {
    res.setHeader('Content-Type', 'text/html'); res.end(fs.readFileSync(path.join(root, 'static', 'index.html'))); return;
  }
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    res.end('<!doctype html><html data-theme="dark"><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/css/style.css"><link rel="stylesheet" href="/static/css/swarm.css"></head><body><main id="view-content"></main><script type="module" src="/fixture.js"></script></body></html>'); return;
  }
  if (url.pathname === '/fixture.js') {
    res.setHeader('Content-Type', 'text/javascript');
    res.end("import {render} from '/static/js/views/swarm.js'; window.mount=()=>{window.dispose?.();window.dispose=render(document.querySelector('main'));};window.mount();"); return;
  }
  if (url.pathname === '/favicon.ico') { res.writeHead(204); res.end(); return; }
  const file = path.resolve(root, '.' + decodeURIComponent(url.pathname));
  if (!file.startsWith(path.join(root, 'static') + path.sep)) { res.writeHead(404); res.end(); return; }
  try { res.setHeader('Content-Type', { '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png' }[path.extname(file)] || 'application/octet-stream'); res.end(fs.readFileSync(file)); }
  catch { res.writeHead(404); res.end(); }
});
app.setPath('userData', path.join(output, 'profile'));
app.disableHardwareAcceleration();
app.commandLine.appendSwitch('force-device-scale-factor', '1');
app.whenReady().then(async () => {
  fs.mkdirSync(output, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = 'http://127.0.0.1:' + server.address().port;
  session.defaultSession.webRequest.onBeforeRequest((details, done) => done({ cancel: !details.url.startsWith(base + '/') }));
  const win = new BrowserWindow({ width: 1280, height: 900, show: false, useContentSize: true,
    webPreferences: { offscreen: true, contextIsolation: true, nodeIntegration: false } });
  win.webContents.on('console-message', details => { if (details.level === 'error' && !details.message.includes('503')) errors.push(details.message); });
  const js = code => win.webContents.executeJavaScript(code);
  const until = async condition => { for (let i = 0; i < 160; i++) { if (await js(condition)) return; await wait(50); } throw new Error('Timed out: ' + condition); };
  const arrow = async (key, code) => {
    for (const type of ['keyDown', 'keyUp']) {
      await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent', { type, key, code: key, windowsVirtualKeyCode: code });
    }
    await wait(60);
  };
  const click = text => js(`{ const button=[...document.querySelectorAll('button')].find(b=>b.textContent===${JSON.stringify(text)}); button?.focus(); button?.click(); }`);
  const capture = async name => { await wait(150); fs.writeFileSync(path.join(output, name + '.png'), (await win.webContents.capturePage()).toPNG()); };
  const fits = async () => assert.ok(await js('document.documentElement.scrollWidth <= innerWidth + 1'), 'No horizontal page overflow');
  try {
    await win.loadURL(base);
    win.webContents.debugger.attach('1.3');
    await win.webContents.debugger.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true });
    await until("document.body.textContent.includes('Your first team starts here')");
    await capture('desktop-empty'); await click('Create system');
    await until("!!document.querySelector('dialog[open]')");
    // Not sure who you need: a drafted roster fills the form, and nothing is
    // created until the form is saved.
    await js("document.querySelector('.swarm-designer').open = true");
    await js("document.querySelector('.swarm-designer textarea').value = 'Launch a YouTube channel'");
    await click('Draft a team');
    await until("document.querySelectorAll('.swarm-member-form').length === 3");
    assert.equal(await js("document.querySelectorAll('.swarm-member-form input')[0].value"), 'Mo', 'The lead is filled in');
    assert.ok(await js("document.body.textContent.includes('Research, then write.')"), 'It says why that shape');
    assert.equal(systems.length, 0, 'Drafting creates nothing');
    const drafted = writes.find(w => w.path === '/api/swarm/draft-team');
    assert.equal(drafted.body.description, 'Launch a YouTube channel');
    assert.ok(drafted.body.endpoint_id, 'The owner names the connection that pays for it');
    await capture('desktop-designer');
    // Back to a hand-built team for the rest of the run.
    await js(`{ const rows=[...document.querySelectorAll('.swarm-member-form')];
      rows.slice(1).forEach(row => [...row.querySelectorAll('button')].find(b=>b.textContent==='Remove specialist')?.click()); }`);
    await until("document.querySelectorAll('.swarm-member-form').length === 1");

    await click('Add specialist');
    await js(`document.querySelector('[name=name]').value='App studio'; document.querySelector('[name=mission]').value='Build a thoughtful project tracker'; const row=document.querySelectorAll('.swarm-member-form')[1]; row.querySelectorAll('input')[0].value='Backend'; row.querySelectorAll('input')[1].value='Developer';`);
    assert.equal(await js("document.querySelectorAll('[aria-label=\"Model connection\"]').length"), 2, 'Every teammate picks its own connection');
    await js(`{ const picks=[...document.querySelectorAll('[aria-label="Model connection"]')];
      picks.forEach(p=>{p.value='claude-1';p.dispatchEvent(new Event('change'));}); }`);
    assert.ok(await js("[...document.querySelectorAll('[aria-label=\"Model\"]')][0].textContent.includes('Opus 5')"), 'Models come from the catalog for that kind');
    assert.ok(await js("[...document.querySelectorAll('[aria-label=\"Reasoning effort\"]')][0].textContent.includes('high')"), 'Efforts are offered for the chosen connection');
    await js(`{ const m=[...document.querySelectorAll('[aria-label="Model"]')][0];
      m.value='claude-opus-5'; m.dispatchEvent(new Event('change'));
      const e=[...document.querySelectorAll('[aria-label="Reasoning effort"]')][0];
      e.value='high'; }`);
    await js(`{ const codex=[...document.querySelectorAll('[aria-label="Model connection"]')][1];
      codex.value='codex-1'; codex.dispatchEvent(new Event('change')); }`);
    assert.ok(await js("[...document.querySelectorAll('[aria-label=\"Model\"]')][1].textContent.includes('Sol')"), 'Codex catalog offers Sol');
    await js(`{ const model=[...document.querySelectorAll('[aria-label="Model"]')][1];
      model.value='gpt-5.6-luna'; model.dispatchEvent(new Event('change'));
      [...document.querySelectorAll('[aria-label="Reasoning effort"]')][1].value='low'; }`);
    await js(`{ const mode=document.querySelector('[name=mode]'); mode.value='scheduled'; mode.dispatchEvent(new Event('change'));
      [...document.querySelectorAll('.swarm-schedule label')].find(n=>n.textContent.includes('automatic provider spending')).querySelector('input').checked=true;
      [...document.querySelectorAll('.swarm-memory-sources label')].filter(n=>['Vault notes','JARVIS Project'].includes(n.textContent.trim())).forEach(n=>n.querySelector('input').checked=true);
      document.querySelector('[aria-label="JARVIS Project"]').value='project-1';
      const run=[...document.querySelectorAll('.swarm-limits')].find(n=>n.querySelector('legend').textContent==='Per-run allocation');
      run.querySelector('input[type=checkbox]').click(); }`);
    assert.equal(await js("document.querySelector('.swarm-schedule').hidden"), false, 'Scheduled mode exposes its work window');
    await capture('desktop-setup');
    await js("document.querySelector('.swarm-setup').requestSubmit()");
    await until("document.querySelector('.swarm-header h1')?.textContent==='App studio'");
    assert.equal(systems.length, 1); assert.equal(systems[0].agents.length, 2);
    const setupWrite = writes.find(w => w.path === '/api/swarm/systems');
    assert.equal(setupWrite.body.lead.endpoint_id, 'claude-1', 'The chosen connection is saved');
    assert.equal(setupWrite.body.lead.model, 'claude-opus-5');
    assert.equal(setupWrite.body.lead.effort, 'high');
    assert.equal(setupWrite.body.specialists[0].model, 'gpt-5.6-luna');
    assert.equal(setupWrite.body.specialists[0].effort, 'low');
    assert.equal(setupWrite.body.specialists[0].endpoint_id, 'codex-1');
    assert.equal(setupWrite.body.mode, 'scheduled');
    assert.equal(setupWrite.body.schedule.auto_spend_confirmed, true, 'Scheduled provider spending is explicit');
    assert.equal(setupWrite.body.run_limit.ceiling, null, 'A cumulative allocation can be uncapped');
    assert.deepEqual(setupWrite.body.memory.sources, ['vault', 'project']);
    assert.equal(setupWrite.body.memory.project_id, 'project-1');
    assert.equal(setupWrite.body.lead.step_limit, 6250, 'Each worker step remains finite');
    assert.equal(await js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Start').disabled"), true);
    await until("document.body.textContent.includes('Live updates connected')");
    const company = systems[0];
    await js("const input=document.querySelector('.swarm-composer textarea');input.value='Build the first milestone <img src=x onerror=alert(1)>';input.dispatchEvent(new Event('input'));document.querySelector('.swarm-composer').requestSubmit()");
    await until("document.querySelectorAll('.swarm-message').length===1");
    assert.equal(await js("document.querySelectorAll('.swarm-message img').length"), 0);
    assert.equal(company.pages.messages.total, 1);
    await js("document.querySelector('.swarm-composer textarea').value='Unsaved draft';document.querySelector('.swarm-composer textarea').dispatchEvent(new Event('input'))");
    emit(company, 'task.updated'); await wait(300);
    assert.equal(await js("document.querySelector('.swarm-composer textarea').value"), 'Unsaved draft');
    const before = snapshotCalls; emit(company, 'task.updated', true); await wait(250);
    assert.equal(snapshotCalls, before, 'Duplicate events do not refresh twice');
    const opens = streamOpens; for (const stream of streams) stream.res.end();
    await until("document.body.textContent.includes('reconnecting')");
    await until("document.body.textContent.includes('Live updates connected')");
    assert.ok(streamOpens > opens, 'EventSource reconnects');
    company.availability = { execution_available: false, code: 'setup_blocked', blockers: [
      'Backend (Codex): Codex CLI executable not found. Install Codex CLI and sign in before using Swarm.'] };
    emit(company, 'system.updated'); await wait(300);
    await until("!!document.querySelector('.swarm-blockers li')");
    assert.ok(await js("document.querySelector('.swarm-blockers li').textContent.includes('executable not found')"), 'A Codex installation problem is named before Start');
    company.availability = { execution_available: true, code: 'ready', blockers: [],
      detail: 'Workers have no shell, file or vault access. They can plan, research, review and write.' };
    emit(company, 'system.updated'); await wait(300);
    const starts = writes.filter(w => w.path.endsWith('/start')).length;
    await click('Start'); await until("!!document.querySelector('dialog[open]')");
    assert.ok(await js("document.querySelector('.swarm-dialog-body').textContent.includes('not provider billing caps')"), 'Start names the provider-spend boundary');
    assert.ok(await js("document.querySelector('.swarm-dialog-body').textContent.includes('100,000 tokens')"), 'Start shows the company allocation');
    assert.ok(await js("document.querySelector('.swarm-dialog-body').textContent.includes('Claude Code') && document.querySelector('.swarm-dialog-body').textContent.includes('gpt-5.6-luna')"), 'Start shows enabled teammate connections and models');
    await capture('desktop-start-confirmation');
    await click('Cancel'); await until("!document.querySelector('dialog')");
    assert.equal(writes.filter(w => w.path.endsWith('/start')).length, starts, 'Cancelling Start sends no request');
    assert.equal(await js('document.activeElement.textContent'), 'Start', 'Start cancellation restores focus');
    await click('Start'); await click('Start and spend');
    await until("document.querySelector('.swarm-state')?.textContent==='active'");
    assert.equal(writes.findLast(w => w.path.endsWith('/start')).body.spend_confirmed, true);

    await click('Usage'); await until("document.body.textContent.includes('No provider allowance has been observed')");
    await capture('desktop-workspace');

    // Swimlane graph and board (David's ask 2026-09-16).
    seedWork(company); emit(company, 'task.created'); await wait(300);
    await click('Map');
    await until("document.querySelectorAll('.swarm-node').length===5");
    assert.equal(await js("document.querySelectorAll('.swarm-edge').length"), 3, 'One edge per loaded dependency');
    assert.equal(await js("document.querySelectorAll('.swarm-lane-label').length"), 2, 'One lane per agent');
    assert.equal(await js("document.querySelector('.swarm-node[data-task=task-2]').dataset.depth"), '1', 'Depth is longest path, not insertion order');
    assert.equal(await js("document.querySelector('.swarm-node[data-task=task-3]').dataset.depth"), '2');
    assert.ok(await js("document.querySelector('.swarm-node[data-task=task-3]').textContent.includes('Waiting on 1 task')"), 'Unmet dependencies are counted, not guessed');
    assert.ok(await js("document.querySelector('.swarm-node[data-task=task-2]').textContent.includes('1,200 of 25,000 tokens')"), 'Running card shows recorded usage');
    await fits(); await capture('desktop-map');
    await js("document.querySelector('.swarm-node[data-task=task-1]').focus()");
    await arrow('ArrowRight', 39);
    assert.equal(await js('document.activeElement.dataset.task'), 'task-4', 'Right travels the lane by depth');
    await arrow('ArrowDown', 40);
    assert.equal(await js('document.activeElement.dataset.task'), 'task-3', 'Down crosses to the next lane at the nearest depth');
    await js('document.activeElement.click()');
    await until("!!document.querySelector('dialog[open]')");
    assert.ok(await js("document.querySelector('.swarm-dialog-body').textContent.includes('Build the registration endpoint')"), 'Detail names the real dependency');
    await click('Close'); await until("!document.querySelector('dialog')");

    await click('Board');
    await until("document.querySelectorAll('.swarm-column').length===5");
    assert.equal(await js("document.querySelector('[data-column=running] .swarm-card').dataset.task"), 'task-2');
    assert.equal(await js("document.querySelector('[data-column=done] .swarm-card').dataset.task"), 'task-1');
    assert.ok(await js("document.querySelector('.swarm-board-status').textContent.includes('1 cancelled hidden')"));
    await click('Show cancelled');
    await until("document.querySelectorAll('.swarm-column').length===6");
    await fits(); await capture('desktop-board');
    company.pages.tasks.items.find(t => t.id === 'task-3').state = 'running';
    emit(company, 'task.claimed'); await wait(300);
    await until("document.querySelectorAll('[data-column=running] .swarm-card').length===2");
    assert.ok(await js("document.querySelector('.swarm-card[data-task=task-3]').classList.contains('swarm-card-arrived')"), 'A real state change moves the card and marks its arrival');
    assert.equal(await js("document.querySelectorAll('.swarm-card[data-task=task-3]').length"), 1, 'A moved card is never duplicated');
    await click('Chat');
    await until("!!document.querySelector('.swarm-composer')");
    assert.ok(await js("document.querySelector('.swarm-board-wrap').hidden && document.querySelector('.swarm-canvas-wrap').hidden"), 'Unselected work views are hidden');
    assert.equal(await js("getComputedStyle(document.querySelector('.swarm-board-wrap')).display"), 'none', 'And hidden means hidden, despite the flex rule');

    // C2: connections, live states, readable activity, results, reconcile.
    assert.ok(await js("document.body.textContent.includes('Needs reconciliation')"), 'An unconfirmed stop is visible on the team');
    assert.ok(await js("document.body.textContent.includes('Claude Code')"), 'A teammate shows the connection it runs on');
    assert.ok(await js("[...document.querySelectorAll('.swarm-agent-state')].some(n=>n.textContent==='Working')"), 'A claimed task reads as working');
    assert.ok(await js("document.body.textContent.includes('The contract is POST /users')"), 'Accepted work is shown where the owner looks');

    company.system.configuration = { ...company.system.configuration, mode: 'autonomous' };
    company.cycles = 3;
    company.conclusion = { reason: 'mission_complete', summary: 'Everything asked for is delivered.' };
    emit(company, 'mission.concluded'); await wait(300);
    assert.ok(await js("document.querySelector('.swarm-conclusion').textContent.includes('Mission complete')"), 'A finished mission says so');
    assert.ok(await js("document.body.textContent.includes('3 cycles so far')"), 'An autonomous company shows how many cycles it ran');
    company.conclusion = { reason: 'cycle_limit', summary: 'Reached the 5-cycle ceiling for one start.' };
    emit(company, 'mission.concluded'); await wait(300);
    assert.ok(await js("document.querySelector('.swarm-conclusion').textContent.includes('cycle ceiling')"), 'A ceiling stop is named, not silent');
    // A team waiting on its owner reads as a question, and the composer answers it.
    company.conclusion = { reason: 'needs_owner', summary: 'Paste the transcript text into a message.' };
    emit(company, 'mission.concluded'); await wait(300);
    assert.ok(await js("document.querySelector('.swarm-needs').textContent.includes('needs something from you')"), 'A waiting team asks visibly');
    assert.ok(await js("document.querySelector('.swarm-needs').textContent.includes('Paste the transcript')"), 'And says what it needs');
    assert.equal(await js("document.querySelector('.swarm-composer textarea').getAttribute('placeholder')"), 'Answer what the team asked for…');
    assert.ok(await js("[...document.querySelectorAll('button')].some(b=>b.textContent==='Answer and resume')"), 'The composer becomes the answer');
    await capture('desktop-needs-owner');
    delete company.conclusion;
    emit(company, 'system.updated'); await wait(300);

    await click('Activity');
    await until("!!document.querySelector('.swarm-activity')");
    assert.ok(await js("document.body.textContent.includes('Accepted: Matches the agreed contract.')"), 'Activity reads as sentences');
    await capture('desktop-activity');
    assert.equal(await js("document.querySelectorAll('.swarm-activity pre').length"), 0, 'No raw payloads in the feed');

    await click('Tasks');
    await until("!!document.querySelector('.swarm-unknown')");
    await click('Reconcile');
    await until("!!document.querySelector('dialog[open]')");
    await capture('desktop-reconcile');
    await js("document.querySelector('.swarm-dialog form').requestSubmit()");
    await wait(150);
    assert.ok(await js("!!document.querySelector('dialog[open]')"), 'Reconcile refuses an empty box without asking the server');
    assert.equal(writes.filter(w => w.path.includes('/reconcile')).length, 0);
    await js("const box=document.querySelector('.swarm-dialog textarea');box.value='Checked Task Manager, nothing is running.';document.querySelector('.swarm-dialog form').requestSubmit()");
    await until("!document.querySelector('dialog')");
    assert.equal(company.pages.attempts.items.find(a => a.id === 'attempt-2').state, 'cancelled');
    assert.equal(writes.filter(w => w.path.includes('/reconcile'))[0].body.evidence, 'Checked Task Manager, nothing is running.');
    await until("!document.querySelector('.swarm-unknown')");

    await click('Handoffs'); await click('Save handoff');
    await until("document.querySelectorAll('a[download]').length===2");
    assert.ok(await js("[...document.querySelectorAll('a[download]')].every(a=>a.pathname.startsWith('/api/swarm/systems/'))"));
    await click('Pause'); await until("document.querySelector('.swarm-state')?.textContent==='paused'");
    await capture('desktop-paused');
    const resumes = writes.filter(w => w.path.endsWith('/resume')).length;
    await click('Resume'); await until("!!document.querySelector('dialog[open]')");
    assert.ok(await js("document.querySelector('.swarm-dialog-body').textContent.includes('Resume this company?') || document.querySelector('.swarm-dialog-header').textContent.includes('Resume this company?')"));
    await click('Cancel'); await until("!document.querySelector('dialog')");
    assert.equal(writes.filter(w => w.path.endsWith('/resume')).length, resumes, 'Cancelling Resume sends no request');
    await click('Resume'); await click('Resume and spend');
    await until("document.querySelector('.swarm-state')?.textContent==='active'");
    assert.equal(writes.findLast(w => w.path.endsWith('/resume')).body.spend_confirmed, true);
    await click('Pause'); await until("document.querySelector('.swarm-state')?.textContent==='paused'");
    await click('Edit setup'); await until("!!document.querySelector('dialog[open]')");
    assert.ok(await js("document.querySelector('.swarm-setup').textContent.includes('affects every company that uses that provider account')"),
      'Edit setup explains the shared provider-account effect');
    assert.equal(await js("document.querySelector('[data-pool-id] input[type=number]').value"), '200000');
    await js("document.querySelector('[name=name]').value='Updated studio';document.querySelector('[data-pool-id] input[type=number]').value='450000';document.querySelector('.swarm-setup').requestSubmit()");
    await until("document.querySelector('.swarm-header h1')?.textContent==='Updated studio'");
    const setupPatch = writes.findLast(write => write.method === 'PATCH' && write.path === `/api/swarm/systems/${company.system.id}`);
    assert.deepEqual(setupPatch.body.pool_limits, [{ id: company.agents[0].pool_id,
      limit: { ceiling: 450000, pause_percent: 80, checkpoint_reserve: 0 } }]);
    assert.equal(company.budgets.find(item => item.scope === 'pool').ceiling, 450000);
    await click('Stop'); await until("!!document.querySelector('dialog[open]')");
    await click('Stop run'); await until("document.querySelector('.swarm-state')?.textContent==='stopped'");
    await click('Archive'); await until("!!document.querySelector('dialog[open]')");
    await js("document.querySelector('dialog .btn.danger').click()");
    await until("document.querySelector('.swarm-state')?.textContent==='archived'");
    assert.ok(await js("document.querySelector('.swarm-composer textarea').disabled"));
    await click('Restore'); await until("document.querySelector('.swarm-state')?.textContent==='stopped'");
    win.setContentSize(390, 844); await wait(150); await fits();
    assert.equal(await js("getComputedStyle(document.querySelector('.swarm-team')).display"), 'none');
    await capture('mobile-inbox'); await click('Team'); await fits(); await capture('mobile-team');
    await click('Work'); await click('Usage'); await fits(); await capture('mobile-usage');
    await click('Edit setup'); await until("!!document.querySelector('dialog[open]')"); await fits(); await capture('mobile-setup');
    emit(company, 'task.updated'); await wait(250); // Rebuilt controls must still recover keyboard focus.
    await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await win.webContents.debugger.sendCommand('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 });
    await until("!document.querySelector('dialog')");
    assert.equal(await js('document.activeElement.textContent'), 'Edit setup', 'Dialog restores focus');
    await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
    assert.equal(await js("getComputedStyle(document.querySelector('.swarm-state')).transitionDuration"), '0s');
    await click('Board'); await until("!!document.querySelector('.swarm-board')");
    await fits(); await capture('mobile-board');
    company.pages.tasks.items.find(t => t.id === 'task-4').state = 'done';
    emit(company, 'task.accepted'); await wait(300);
    await until("document.querySelectorAll('[data-column=done] .swarm-card').length===2");
    assert.ok(await js("document.querySelector('.swarm-card[data-task=task-4]').classList.contains('swarm-card-changed')"), 'Reduced motion highlights in place');
    assert.ok(await js("!document.querySelector('.swarm-card[data-task=task-4]').classList.contains('swarm-card-arrived')"), 'Reduced motion never animates travel');
    await click('Map'); await until("!!document.querySelector('.swarm-canvas')");
    await fits(); await capture('mobile-map');
    await js('window.dispose()'); await wait(150); assert.equal(streams.size, 0, 'Unmount closes observation streams');
    snapshotDelay = 250;
    await js('window.mount()'); await until("!!document.querySelector('.swarm-system-card')");
    await js("document.querySelector('.swarm-system-card').click();window.dispose();document.querySelector('main').textContent='Different view'");
    await wait(400); assert.equal(await js("document.querySelector('main').textContent"), 'Different view', 'Late response cannot overwrite another view');
    snapshotDelay = 0; unavailable = true;
    await js('window.mount()'); await until("!!document.querySelector('[role=alert]')");
    await capture('mobile-unavailable');
    unavailable = false;
    win.setContentSize(1440, 900);
    await win.loadURL(base + '/full-app');
    await until("!!document.querySelector('[data-tab=swarm]')");
    assert.equal(await js("document.querySelectorAll('[data-tab=swarm]').length"), 1, 'Built-in tab wins custom name collision');
    assert.ok(await js("document.querySelector('[data-tab=chat]').nextElementSibling.dataset.tab === 'swarm'"));
    await js("document.querySelector('[data-tab=swarm]').click()");
    await until("!!document.querySelector('.swarm-system-card')");
    await js("document.querySelector('.swarm-system-card').click()");
    await until("document.querySelector('.swarm-header h1')?.textContent==='Updated studio'");
    await fits(); await capture('full-app-desktop');
    await click('Map'); await until("document.querySelectorAll('.swarm-node').length===5");
    await fits(); await capture('full-app-map');
    await click('Board'); await until("!!document.querySelector('.swarm-board')");
    await fits(); await capture('full-app-board');
    await click('Chat'); await until("!!document.querySelector('.swarm-composer')");
    win.setContentSize(390, 844); await wait(150);
    await fits(); await capture('full-app-mobile');
    await js("import('/static/js/app.js').then(m=>m.switchTab('home'))");
    await wait(150); assert.equal(streams.size, 0, 'App navigation cleans up Swarm');
    await js("import('/static/js/app.js').then(m=>m.switchTab('swarm'))");
    await until("!!document.querySelector('.swarm-system-card')");
    await js("document.querySelector('.swarm-system-card').click()");
    await until("document.querySelector('.swarm-header h1')?.textContent==='Updated studio'");
    await click('Archive'); await until("!!document.querySelector('dialog[open]')");
    await js("document.querySelector('dialog .btn.danger').click()");
    await until("document.querySelector('.swarm-state')?.textContent==='archived'");
    const deletes = writes.filter(w => w.method === 'DELETE').length;
    await click('Delete'); await until("!!document.querySelector('dialog[open]')");
    assert.equal(await js("document.activeElement.getAttribute('aria-label')"), 'Type company name', 'Delete starts in the confirmation field');
    await click('Cancel'); await until("!document.querySelector('dialog')");
    assert.equal(writes.filter(w => w.method === 'DELETE').length, deletes, 'Cancelling Delete sends no request');
    await until("document.activeElement.textContent==='Delete'");
    assert.equal(await js('document.activeElement.textContent'), 'Delete', 'Delete cancellation restores focus');
    await click('Delete');
    await js("{ const input=document.querySelector('[aria-label=\"Type company name\"]'); input.value='Updated'; input.dispatchEvent(new Event('input')); }");
    assert.equal(await js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Delete permanently').disabled"), true, 'Partial company name cannot delete');
    await js("{ const input=document.querySelector('[aria-label=\"Type company name\"]'); input.value='Updated studio'; input.dispatchEvent(new Event('input')); }");
    assert.equal(await js("[...document.querySelectorAll('button')].find(b=>b.textContent==='Delete permanently').disabled"), false, 'Exact company name enables delete');
    await fits(); await capture('mobile-delete');
    await click('Delete permanently');
    await until("document.body.textContent.includes('Your first team starts here')");
    assert.equal(systems.length, 0);
    assert.equal(writes.findLast(w => w.method === 'DELETE').body.confirmation, 'Updated studio');
    assert.equal(streams.size, 0, 'Deletion closes the company event stream');
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: true, checks: ['empty/setup/edit', 'existing shared allocation edit and warning', 'queued message and literal HTML', 'draft retained', 'SSE reconnect/dedup/unmount', 'usage unavailable', 'handoff links', 'spend confirmation for start/resume', 'pause/stop/archive/restore', 'typed permanent deletion', 'desktop/mobile overflow', 'Escape/focus', 'reduced motion', 'late responses', 'failure state', 'swimlane graph layering/edges/lanes', 'graph keyboard travel and detail', 'board columns from real states', 'card movement on a real event', 'reduced-motion highlight without travel', 'per-teammate connection/model/effort pickers', 'named capability blockers', 'live agent states', 'readable activity', 'accepted results', 'reconcile requires evidence'], writes }, null, 2));
    console.log('PASS: Swarm UI, spend confirmation, deletion, lifecycle, handoffs, SSE, graph, board, focus, reduced motion and error handling.');
    console.log('Screenshots: ' + output);
  } catch (error) {
    await capture('failure').catch(() => {});
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: false, error: error.stack, errors }, null, 2));
    console.error(error.stack); process.exitCode = 1;
  } finally {
    for (const stream of streams) stream.res.end();
    win.destroy(); server.close(); exitAfterFlush(process.exitCode);
  }
}).catch(error => { console.error(error.stack); exitAfterFlush(1); });
