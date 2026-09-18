// Real Chromium, synthetic sessions and loopback-only traffic. No live backend.
const { app, BrowserWindow, session } = require('electron');
const { exitAfterFlush } = require('./electron-exit.cjs');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'data', 'chat-review', 'run-' + Date.now());
const errors = [], requests = [];
function samplePDF() {
  const stream = 'BT /F1 24 Tf 50 740 Td (JARVIS preview test) Tj ET';
  const objects = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>', '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>', `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`];
  let out = '%PDF-1.4\n'; const offsets = [0];
  objects.forEach((obj, i) => { offsets.push(out.length); out += `${i+1} 0 obj\n${obj}\nendobj\n`; });
  const xref = out.length;
  out += 'xref\n0 6\n0000000000 65535 f \n' + offsets.slice(1).map(offset => String(offset).padStart(10,'0') + ' 00000 n \n').join('');
  return out + `trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
}
const files = {
  '/generated-files/012345abcdef_Project%20brief.md': { filename: 'Project brief.md', extension: 'md', kind: 'markdown', text: '# Project brief\n\nA **focused** workspace.\n\n- Clear next steps\n- Room to think' },
  '/generated-files/012345abcdef_preview.html': { filename: 'preview.html', extension: 'html', kind: 'html', text: '<html><style>h1{color:teal}</style><h1>A focused workspace</h1><script>parent.__xss=1</script><img src="https://example.test/tracker"><a href="https://example.test">Escape</a></html>' },
  // 'office' rather than 'download' as of the Office preview work
  // (2026-09-15) — see core/chat_artifacts.py. A macro-enabled sibling is
  // kept below so the download-only fallback still has a real subject.
  '/generated-files/012345abcdef_report.docx': { filename: 'report.docx', extension: 'docx', kind: 'office', text: 'synthetic office file' },
  '/generated-files/012345abcdef_budget.xlsx': { filename: 'budget.xlsx', extension: 'xlsx', kind: 'office', text: 'synthetic workbook' },
  '/generated-files/012345abcdef_deck.pptx': { filename: 'deck.pptx', extension: 'pptx', kind: 'office', text: 'synthetic deck' },
  '/generated-files/012345abcdef_rows.csv': { filename: 'rows.csv', extension: 'csv', kind: 'office', text: 'name,note\r\n"Smith, John",two\r\n' },
  '/generated-files/012345abcdef_macros.docm': { filename: 'macros.docm', extension: 'docm', kind: 'download', text: 'macro-enabled file' },
  '/generated-files/012345abcdef_snippet.py': { filename: 'snippet.py', extension: 'py', kind: 'text', text: 'print("Hello")' },
  '/generated-files/012345abcdef_preview.pdf': { filename: 'preview.pdf', extension: 'pdf', kind: 'pdf', text: samplePDF() },
  '/generated-files/012345abcdef_image.png': { filename: 'image.png', extension: 'png', kind: 'image', text: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1sAAAAASUVORK5CYII=', 'base64') },
};
const rich = '# A clearer direction\n\nHere is a **focused plan**, with `useful details` and [a reference](https://example.test).\n\n## Next steps\n\n1. Keep the important work visible.\n2. Make room for deeper thinking.\n\n> Good tools stay out of your way.\n\n| Area | Direction |\n| --- | --- |\n| Chat | Clear, readable responses |\n| Files | Preview without leaving |\n\n```python\ndef greet(name):\n    return f"Hello, {name}"\n```\n\n[Project brief](/generated-files/012345abcdef_Project%20brief.md)\n\n[Static preview](/generated-files/012345abcdef_preview.html)\n\n[Word document](/generated-files/012345abcdef_report.docx)\n\n[Python source](/generated-files/012345abcdef_snippet.py)\n\n[Workbook](/generated-files/012345abcdef_budget.xlsx)\n\n[Slide deck](/generated-files/012345abcdef_deck.pptx)\n\n[Rows](/generated-files/012345abcdef_rows.csv)\n\n[Macro document](/generated-files/012345abcdef_macros.docm)';
// Engine available, no model downloaded: what a fresh install looks like.
let speechStatus = { engine_available: true, active_model: null, models: [
  { name: 'tiny.en', label: 'Tiny', size_mb: 75, downloaded: false },
  { name: 'base.en', label: 'Base', size_mb: 142, downloaded: false },
] };
const models = [{ id: 'claude', name: 'Claude Code', kind: 'claude_cli', model: 'configured-model' }, { id: 'codex', name: 'Codex CLI', kind: 'codex_cli', model: '' }, { id: 'local', name: 'Local', kind: 'local', model: 'local-model' }];
// Stands in for core/model_catalog.py's response. "ultra" belongs to one
// codex model and not the other on purpose — same asymmetry the backend
// suite relies on, so the effort row is proven to be per-model here too.
const effortList = names => names.map(effort => ({ effort, description: effort + ' reasoning' }));
const catalog = {
  codex_cli: [
    { id: 'catalog-astra', display_name: 'Catalog Astra', description: 'Most capable catalog model.', alias: null, default_effort: 'low', supported_efforts: effortList(['low', 'medium', 'high', 'ultra']), context_window: 100000, effective_context_percent: 90, source: 'cli_cache', estimated: false },
    { id: 'catalog-lite', display_name: 'Catalog Lite', description: 'Smaller and faster.', alias: null, default_effort: 'medium', supported_efforts: effortList(['low', 'medium']), context_window: 50000, effective_context_percent: null, source: 'cli_cache', estimated: false },
  ],
  claude_cli: [
    { id: 'configured-model', display_name: 'Configured Model', description: 'Curated entry.', alias: null, default_effort: null, supported_efforts: effortList(['low', 'high']), context_window: 200000, effective_context_percent: null, source: 'curated', estimated: true },
  ],
};
const chats = {
  s1: { id: 's1', title: 'A focused workspace', model_endpoint_id: 'claude', messages: [{ role: 'user', content: 'Help me shape this into a clear plan.' }, { role: 'assistant', content: rich }] },
  s2: { id: 's2', title: 'Another conversation', model_endpoint_id: 'codex', messages: [] },
};
let pending = null;
const answers = [];
let deferFirstChunk = false;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    // Mirrors core/middleware.py's real header, frame-src included — the
    // whole point of reproducing it here is that a CSP difference between
    // fixture and product hides exactly this class of bug.
    res.setHeader('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'self' https:");
    res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/css/style.css"><link rel="stylesheet" href="/static/css/chat.css"></head><body><div id="view-content" class="view active" style="height:100vh"></div></body></html>'); return;
  }
  if (url.pathname.startsWith('/api/')) {
    let body = ''; for await (const chunk of req) body += chunk;
    const data = body && req.headers['content-type']?.includes('application/json') ? JSON.parse(body) : {};
    requests.push({ path: url.pathname, method: req.method, data });
    const json = value => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)); };
    if (url.pathname === '/api/speech/status') return json(speechStatus);
    if (url.pathname === '/api/speech/transcribe') return json({ text: 'what is the weather' });
    if (url.pathname === '/api/models/catalog') return json(catalog);
    if (url.pathname === '/api/models') return json(models);
    if (url.pathname === '/api/projects') return json([]);
    if (url.pathname === '/api/chat/attachments') return json({ id: 'staged-test', filename: 'draft.txt' });
    if (url.pathname === '/api/sessions') {
      if (req.method === 'POST') {
        const id = 'new-' + Object.keys(chats).length;
        chats[id] = { id, title: 'New chat', messages: [], model_endpoint_id: 'claude' };
        return json(chats[id]);
      }
      return json(Object.values(chats));
    }
    const match = url.pathname.match(/^\/api\/sessions\/([^/]+)(\/model|\/context|\/open-mic)?$/);
    if (match) {
      const chat = chats[match[1]];
      if (match[2] === '/model') {
        Object.assign(chat, { model_effort: data.effort ?? null, ...data });
        // Mirrors core/session_manager.py: a model change invalidates the
        // stored occupancy, because the capacity it was measured against
        // no longer applies.
        if ('model_override' in data) chat.context_state = null;
        return json({ ok: true, ...data });
      }
      if (match[2] === '/open-mic') {
        chat.open_mic = !!data.active;
        return json(data.active ? { open_mic: true } : { open_mic: false, summarised: false, reason: 'too short to summarise' });
      }
      if (match[2] === '/context') return json(chat.context_state ? { available: true, ...chat.context_state } : { available: false });
      return json(chat);
    }
    // Structured Office contents, shaped exactly like core/office_preview.py's
    // output so the renderers are exercised against the real payload.
    if (url.pathname === '/api/chat/artifacts/office') {
      const file = files[url.searchParams.get('url')];
      if (!file || file.kind !== 'office') { res.writeHead(400); return json({ detail: 'This file has no structured preview' }); }
      if (file.extension === 'xlsx') return json({ kind: 'xlsx', truncated_sheets: false, sheets: [
        { name: 'Budget', rows: [['Item', 'Qty'], ['Widget', '3']], total_rows: 900, total_cols: 2, truncated_rows: true, truncated_cols: false },
        { name: 'Notes', rows: [['second sheet']], total_rows: 1, total_cols: 1, truncated_rows: false, truncated_cols: false },
      ] });
      if (file.extension === 'docx') return json({ kind: 'docx', truncated: false, blocks: [
        { type: 'heading', level: 1, text: 'Project brief' },
        { type: 'paragraph', level: 0, text: 'An opening paragraph.' },
        { type: 'table', rows: [['Area', 'Direction'], ['Chat', 'Clear']] },
        { type: 'paragraph', level: 0, text: 'Closing paragraph after the table.' },
      ] });
      if (file.extension === 'pptx') return json({ kind: 'pptx', truncated: false, layout_fidelity: false, slides: [
        { index: 1, title: 'Quarterly review', body: ['First point', 'Second point'], tables: [], notes: 'Speaker notes here.' },
        { index: 2, title: 'Second slide', body: [], tables: [[['a', 'b']]], notes: '' },
      ] });
      return json({ kind: 'csv', truncated: false, rows: [['name', 'note'], ['Smith, John', 'two']] });
    }
    if (url.pathname.startsWith('/api/chat/artifacts')) {
      const file = files[url.searchParams.get('url')];
      if (!file) { res.writeHead(404); return json({ detail: 'The generated file is no longer available' }); }
      if (url.pathname.endsWith('/content')) {
        res.setHeader('Content-Type', file.kind === 'pdf' ? 'application/pdf' : file.kind === 'image' ? 'image/png' : 'text/plain');
        res.setHeader('X-Frame-Options', 'SAMEORIGIN');
        res.setHeader('Content-Security-Policy', "default-src 'none'; frame-ancestors 'self'; sandbox");
        res.end(file.text); return;
      }
      return json({ ...file, size: file.text.length });
    }
    if (url.pathname === '/api/chat/stream') {
      chats[data.session_id].messages.push({ role: 'user', content: data.message });
      res.setHeader('Content-Type', 'text/event-stream');
      const text = '## Working on your request\n\n' + 'A paragraph with enough detail to exercise the reading position.\n\n'.repeat(25);
      res.flushHeaders();
      if (!deferFirstChunk) res.write('data: ' + JSON.stringify({ chunk: text }) + '\r\n\r\n');
      pending = { res, sid: data.session_id, text };
      return;
    }
    if (url.pathname.startsWith('/api/permissions')) {
      if (url.pathname.endsWith('/answer')) { answers.push({ path: url.pathname, choice: data.choice }); return json({ status: 'answered' }); }
      return json({ rules: [], audit: [] });
    }
    res.writeHead(404); json({ detail: 'Missing fixture: ' + url.pathname }); return;
  }
  const target = path.resolve(root, '.' + url.pathname);
  if (!target.startsWith(path.join(root, 'static') + path.sep)) { res.writeHead(404); res.end(); return; }
  try {
    res.setHeader('Content-Type', { '.js': 'text/javascript', '.mjs': 'text/javascript', '.wasm': 'application/wasm', '.css': 'text/css', '.png': 'image/png', '.svg': 'image/svg+xml' }[path.extname(target)] || 'application/octet-stream');
    res.end(fs.readFileSync(target));
  } catch { res.writeHead(404); res.end(); }
});
const delay = ms => new Promise(r => setTimeout(r, ms));
app.setPath('userData', path.join(root, 'data', 'chat-smoke-profile'));
app.whenReady().then(async () => {
  fs.mkdirSync(output, { recursive: true });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  const base = 'http://127.0.0.1:' + server.address().port;
  session.defaultSession.webRequest.onBeforeRequest((details, cb) => cb({ cancel: !details.url.startsWith(base + '/') && !details.url.startsWith('about:') && !details.url.startsWith('data:') }));
  const win = new BrowserWindow({ width: 1440, height: 900, show: false, useContentSize: true, webPreferences: { offscreen: true, contextIsolation: true, nodeIntegration: false } });
  win.webContents.on('console-message', (details) => { if (details.level === 'error') errors.push(details.message); });
  const js = async code => {
    let timer;
    try { return await Promise.race([win.webContents.executeJavaScript(code), new Promise((_, reject) => { timer = setTimeout(() => reject(Error('Renderer timeout: ' + code)), 10000); })]); }
    finally { clearTimeout(timer); }
  };
  const waitFor = async condition => { for (let i = 0; i < 100; i++) { if (await js(condition)) return; await delay(40); } throw Error('Timed out: ' + condition); };
  const capture = async name => { await delay(300); fs.writeFileSync(path.join(output, name + '.png'), (await win.webContents.capturePage()).toPNG()); };
  const open = async sid => { await js(`import('/static/js/views/chat.js').then(m => m.openSessionById('${sid}'))`); };
  const mount = async () => {
    await js(`import('/static/js/views/chat.js').then(async m => { window.cleanup?.(); window.cleanup = await m.render(document.querySelector('#view-content'), 'chat', {sessionId: 's1'}); })`);
  };
  try {
    await win.loadURL(base); await mount();
    win.webContents.debugger.attach('1.3');
    await win.webContents.debugger.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true });
    assert.equal(await js("document.querySelectorAll('.chat-prose h1').length"), 1);
    assert.equal(await js("document.querySelectorAll('.chat-prose table').length"), 1);
    assert.equal(await js("document.querySelectorAll('.chat-code-header button').length"), 1);
    assert.ok(await js("!!document.querySelector('code .hljs-keyword')"));
    assert.equal(await js("document.querySelectorAll('.artifact-card').length"), 8);
    await js("document.querySelector('#chat-messages').scrollTop=0");
    await capture('desktop-chat');
    await js("document.querySelector('#model-version-btn').click(); document.querySelector('#chat-model-id').value='exact-test-model'; document.querySelector('.model-version-form').requestSubmit()");
    await waitFor("document.querySelector('#model-version-btn').textContent==='exact-test-model'");
    assert.equal(chats.s1.model_override, 'exact-test-model');
    assert.equal(models[0].model, 'configured-model');
    await win.loadURL(base); await mount();
    assert.equal(await js("document.querySelector('#model-version-btn').textContent"), 'exact-test-model');
    await js("document.querySelector('#model-picker-btn').click(); [...document.querySelectorAll('#model-picker-menu button')].find(b=>b.textContent.startsWith('Local')).click()");
    await waitFor("document.querySelector('#model-version-btn').hidden");
    await js("document.querySelector('#model-picker-btn').click(); [...document.querySelectorAll('#model-picker-menu button')].find(b=>b.textContent.startsWith('Codex')).click()");
    await waitFor("!document.querySelector('#model-version-btn').hidden");

    // -- model catalog + reasoning level (David's ask 2026-09-15) --------
    await js("document.querySelector('#model-version-btn').click()");
    await waitFor("document.querySelectorAll('.model-catalog-item').length===2");
    assert.ok(await js("[...document.querySelectorAll('.model-catalog-name')].map(n=>n.textContent).includes('Catalog Astra')"));
    // Search filters the list rather than re-querying the server.
    // Braces, not a bare `const`: each js() call is evaluated in the same
    // global scope, so a repeated top-level declaration is a redeclaration
    // error that surfaces only as "script failed to execute".
    await js("{ const s=document.querySelector('#chat-model-search'); s.value='lite'; s.dispatchEvent(new Event('input')); }");
    await waitFor("document.querySelectorAll('.model-catalog-item').length===1");
    assert.equal(await js("document.querySelector('.model-catalog-name').textContent"), 'Catalog Lite');
    await js("{ const s=document.querySelector('#chat-model-search'); s.value=''; s.dispatchEvent(new Event('input')); }");
    await waitFor("document.querySelectorAll('.model-catalog-item').length===2");
    await js("[...document.querySelectorAll('.model-catalog-item')].find(b=>b.textContent.includes('Catalog Astra')).click()");
    await waitFor("document.querySelector('#model-version-btn').textContent==='Catalog Astra'");
    assert.equal(chats.s1.model_override, 'catalog-astra');
    // Picking a model must not carry an effort over with it.
    assert.equal(chats.s1.model_effort, null);
    // Astra advertises four levels; the row adds a "Default" option.
    await js("document.querySelector('#model-version-btn').click()");
    await waitFor("document.querySelectorAll('.model-effort-btn').length===5");
    assert.deepEqual(await js("[...document.querySelectorAll('.model-effort-btn')].map(b=>b.textContent)"), ['Default', 'low', 'medium', 'high', 'ultra']);
    await capture('desktop-model-catalog');
    await js("[...document.querySelectorAll('.model-effort-btn')].find(b=>b.textContent==='ultra').click()");
    await waitFor("document.querySelector('#model-version-btn').textContent==='Catalog Astra · ultra'");
    assert.equal(chats.s1.model_effort, 'ultra');
    assert.equal(requests.filter(r => r.path === '/api/sessions/s1/model' && r.data.effort === 'ultra').length, 1);
    // The smaller model genuinely advertises fewer levels — proving the row
    // is driven per-model, not by one shared provider-wide list.
    await js("document.querySelector('#model-version-btn').click(); [...document.querySelectorAll('.model-catalog-item')].find(b=>b.textContent.includes('Catalog Lite')).click()");
    await waitFor("document.querySelector('#model-version-btn').textContent==='Catalog Lite'");
    await js("document.querySelector('#model-version-btn').click()");
    await waitFor("document.querySelectorAll('.model-effort-btn').length===3");

    // -- context meter --------------------------------------------------
    // Hidden entirely until the server reports a real measurement.
    assert.ok(await js("document.querySelector('#context-pill').hidden"));
    chats.s1.context_state = { used_tokens: 45000, capacity_tokens: 90000, percent: 50, estimated_capacity: false, capacity_source: 'cli_cache', model: 'catalog-astra' };
    await win.loadURL(base); await mount();
    await waitFor("!document.querySelector('#context-pill').hidden");
    assert.equal(await js("document.querySelector('.context-text').textContent"), '50%');
    assert.equal(await js("document.querySelector('#context-pill').dataset.level"), 'ok');
    assert.equal(await js("document.querySelector('.context-bar-fill').style.width"), '50%');
    assert.ok(await js("document.querySelector('#context-pill').title.includes('reported by the provider')"));
    // A real reading with no published capacity shows the token count and
    // deliberately no percentage.
    chats.s1.context_state = { used_tokens: 4200, capacity_tokens: null, percent: null, estimated_capacity: false, capacity_source: null, model: 'unlisted' };
    await win.loadURL(base); await mount();
    await waitFor("document.querySelector('.context-text').textContent==='4.2k ctx'");
    assert.equal(await js("document.querySelector('.context-bar')"), null);
    assert.ok(await js("document.querySelector('#context-pill').title.includes('No context capacity is published')"));
    // A high reading is flagged visually without changing the number.
    chats.s1.context_state = { used_tokens: 87000, capacity_tokens: 90000, percent: 96.7, estimated_capacity: true, capacity_source: 'curated', model: 'catalog-astra' };
    await win.loadURL(base); await mount();
    await waitFor("document.querySelector('#context-pill').dataset.level==='high'");
    assert.equal(await js("document.querySelector('.context-text').textContent"), '96.7%');
    assert.ok(await js("document.querySelector('#context-pill').title.includes('estimated capacity')"));
    chats.s1.context_state = null;
    await win.loadURL(base); await mount();
    await waitFor("document.querySelector('#context-pill').hidden");

    // -- side browser, web fallback (David's ask 2026-09-15). This suite has
    // no preload bridge, so window.jarvis is undefined and browserPane.js
    // takes its iframe path — which is exactly the path a phone or any
    // non-Electron client gets. The native path is covered separately by
    // scripts/browser-smoke.cjs, where the guarantees actually live.
    await js("document.querySelector('#overflow-plus-btn').click(); [...document.querySelectorAll('.overflow-menu-item')].find(b=>b.textContent.includes('Browse the web')).click()");
    await waitFor("!!document.querySelector('.browser-panel')");
    assert.ok(await js("!!document.querySelector('.browser-frame')"), 'web client falls back to an iframe');
    // Always offered, because whether a site refuses framing cannot be
    // detected from JavaScript — the control must not depend on guessing.
    assert.ok(await js("[...document.querySelectorAll('.browser-toolbar .btn')].some(b=>b.textContent==='Open in new tab')"));
    // A bare host becomes https; only http/https are ever accepted.
    await js("{ const f=document.querySelector('.browser-address-form'); f.querySelector('input').value='example.test/docs'; f.requestSubmit(); }");
    await waitFor("document.querySelector('.browser-frame').getAttribute('src')==='https://example.test/docs'");
    assert.equal(await js("document.querySelector('.browser-address').value"), 'https://example.test/docs');
    await js("{ const f=document.querySelector('.browser-address-form'); f.querySelector('input').value='file:///etc/passwd'; f.requestSubmit(); }");
    await waitFor("document.querySelector('.browser-status').textContent.includes('Only web addresses')");
    assert.equal(await js("document.querySelector('.browser-frame').getAttribute('src')"), 'https://example.test/docs', 'a refused address must not navigate the frame');
    // One right-hand pane at a time: in the desktop app the browser's page is
    // a native layer that would paint straight over a file preview.
    await js("document.querySelector('.artifact-card').click()");
    await waitFor("!!document.querySelector('.artifact-panel:not(.browser-panel)')");
    assert.equal(await js("document.querySelectorAll('.browser-panel').length"), 0, 'opening a file preview closes the browser');
    await js("document.querySelector('[aria-label=\"Close file preview\"]').click()");
    await js("document.querySelector('#overflow-plus-btn').click(); [...document.querySelectorAll('.overflow-menu-item')].find(b=>b.textContent.includes('Browse the web')).click()");
    await waitFor("!!document.querySelector('.browser-panel')");
    await js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))");
    await waitFor("document.querySelectorAll('.browser-panel').length===0");

    await js("document.querySelector('#model-version-btn').click(); [...document.querySelectorAll('#model-version-menu button')].find(b=>b.textContent==='Use CLI default').click()");
    await waitFor("document.querySelector('#model-version-btn').textContent==='CLI default'");
    await waitFor("fetch('/api/sessions/s1').then(r=>r.json()).then(s=>s.model_override==='')");
    assert.equal(chats.s1.model_override, '');
    await js("document.querySelector('.artifact-card').click()");
    await waitFor("!!document.querySelector('.artifact-document h1')");
    await capture('desktop-artifact');
    await js("[...document.querySelectorAll('.artifact-toolbar button')].find(b=>b.textContent==='Source').click()");
    assert.ok(await js("document.querySelector('.artifact-source').textContent.startsWith('# Project brief')"));
    await js("document.querySelector('[aria-label=\"Close file preview\"]').click(); document.querySelectorAll('.artifact-card')[1].click()");
    await waitFor("!!document.querySelector('.artifact-frame')");
    assert.equal(await js("document.querySelector('.artifact-frame').getAttribute('sandbox')"), '');
    assert.ok(await js("!document.querySelector('.artifact-frame').srcdoc.includes('<script')"));
    assert.equal(await js("window.__xss || 0"), 0);
    await capture('desktop-html-preview');
    // Card 7 is the macro-enabled .docm — deliberately never previewed, so
    // the download-only fallback still has a real subject now that .docx is
    // a structured preview.
    await js("document.querySelector('[aria-label=\"Close file preview\"]').click(); document.querySelectorAll('.artifact-card')[7].click()");
    await waitFor("!!document.querySelector('.artifact-fallback')");
    assert.ok(await js("document.querySelector('.artifact-toolbar a').href.includes('download=true')"));

    // -- Office previews (David's ask 2026-09-15). Every string below comes
    // from the document, so these also confirm content is inserted as text.
    await js("document.querySelector('[aria-label=\"Close file preview\"]').click(); document.querySelectorAll('.artifact-card')[2].click()");
    await waitFor("!!document.querySelector('.office-doc')");
    assert.equal(await js("document.querySelector('.office-doc h1').textContent"), 'Project brief');
    // Reading order: the table sits between the two paragraphs, not after
    // both of them.
    assert.deepEqual(await js("[...document.querySelector('.office-doc').children].map(n=>n.tagName)"), ['H1', 'P', 'DIV', 'P']);
    assert.equal(await js("document.querySelector('.office-grid th').textContent"), 'Area');
    await capture('desktop-office-docx');

    await js("document.querySelector('[aria-label=\"Close file preview\"]').click(); document.querySelectorAll('.artifact-card')[4].click()");
    await waitFor("document.querySelectorAll('.office-tab').length===2");
    assert.equal(await js("document.querySelector('.office-tab.active').textContent"), 'Budget');
    assert.equal(await js("document.querySelector('.office-grid th').textContent"), 'Item');
    // A partial view says so rather than implying the file ends here.
    assert.ok(await js("document.querySelector('.office-note').textContent.includes('900 rows')"));
    await js("[...document.querySelectorAll('.office-tab')].find(t=>t.textContent==='Notes').click()");
    await waitFor("document.querySelector('.office-grid th').textContent==='second sheet'");
    assert.equal(await js("document.querySelector('.office-tab.active').textContent"), 'Notes');
    await capture('desktop-office-xlsx');

    await js("document.querySelector('[aria-label=\"Close file preview\"]').click(); document.querySelectorAll('.artifact-card')[5].click()");
    await waitFor("!!document.querySelector('.office-slide')");
    assert.equal(await js("document.querySelector('.office-slide h2').textContent"), 'Quarterly review');
    assert.equal(await js("document.querySelectorAll('.office-slide p').length"), 2);
    assert.ok(await js("!!document.querySelector('.office-notes')"), 'speaker notes are available');
    // The fidelity limit is stated in the UI, not left to be discovered.
    assert.ok(await js("[...document.querySelectorAll('.office-note')].some(n=>n.textContent.includes('layout, theming, and images are not shown'))"));
    await js("[...document.querySelectorAll('.artifact-toolbar button')].find(b=>b.getAttribute('aria-label')==='Next slide').click()");
    await waitFor("document.querySelector('.office-slide h2').textContent==='Second slide'");
    assert.ok(await js("!!document.querySelector('.office-slide .office-grid')"), 'slide tables render');
    await capture('desktop-office-pptx');

    await js("document.querySelector('[aria-label=\"Close file preview\"]').click(); document.querySelectorAll('.artifact-card')[6].click()");
    await waitFor("!!document.querySelector('.office-grid')");
    // Parsed server-side precisely so a quoted separator survives.
    assert.equal(await js("document.querySelectorAll('.office-grid td')[0].textContent"), 'Smith, John');
    await js("[...document.querySelectorAll('.artifact-toolbar button')].find(b=>b.textContent==='Source').click()");
    await waitFor("!!document.querySelector('.artifact-source')");
    assert.ok(await js("document.querySelector('.artifact-source').textContent.includes('\"Smith, John\"')"));
    await js("[...document.querySelectorAll('.artifact-toolbar button')].find(b=>b.textContent==='Table').click()");
    await waitFor("!!document.querySelector('.office-grid')");
    await js("import('/static/js/chatContent.js').then(m=>m.openArtifact('s1','/generated-files/012345abcdef_preview.pdf','preview.pdf'))");
    await waitFor("document.querySelector('.artifact-pdf-text')?.textContent.includes('JARVIS preview test')");
    await delay(1000);
    await capture('desktop-pdf-preview');
    await js("import('/static/js/chatContent.js').then(m=>m.openArtifact('s1','/generated-files/012345abcdef_image.png','image.png'))");
    await waitFor("document.querySelector('.artifact-preview > img')?.naturalWidth===1");
    await open('s2');
    assert.equal(await js("document.querySelectorAll('.artifact-panel').length"), 0);
    await delay(400);
    assert.ok(await js("document.querySelector('#chat-main').classList.contains('is-empty')"));
    await js("window.originalComposer=document.querySelector('#chat-input')");
    await capture('desktop-new-chat');
    deferFirstChunk = true;
    await js("document.querySelector('#chat-input').value='Build a plan'; document.querySelector('#chat-send').click()");
    await waitFor("document.querySelector('.chat-activity')?.dataset.state==='waiting'");
    await js("document.querySelector('.chat-activity summary').click()");
    assert.equal(await js("document.querySelectorAll('.chat-activity li[data-state=done]').length"), 1);
    await capture('chat-activity-waiting');
    await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [{name:'prefers-reduced-motion',value:'reduce'}] });
    assert.equal(await js("getComputedStyle(document.querySelector('.chat-activity-marker')).animationName"), 'none');
    await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [] });
    pending.res.write('data: ' + JSON.stringify({ chunk: pending.text }) + '\n\n');
    deferFirstChunk = false;
    await waitFor("document.querySelector('.msg-body').textContent==='Build a plan' && document.querySelectorAll('.chat-prose p').length > 20");
    assert.equal(await js("document.querySelector('#model-version-btn').disabled"), true);
    assert.ok(await js("!document.querySelector('#chat-main').classList.contains('is-empty') && document.querySelector('#chat-input')===window.originalComposer"), 'First turn moves the same composer');
    await delay(400);
    assert.ok(await js("document.querySelector('.chat-input-bar').getBoundingClientRect().bottom > innerHeight-70"), 'Conversation composer docks at bottom');
    await open('s1');
    assert.equal(await js("document.querySelector('#chat-send').dataset.mode!=='stop'"), true);
    await open('s2');
    assert.equal(await js("document.querySelectorAll('.msg.assistant').length"), 1);
    await js("document.querySelector('#chat-messages').scrollTop=0");
    pending.res.write('data: ' + JSON.stringify({ chunk: '\nFinal detail.' }) + '\n\n');
    await delay(150);
    assert.equal(await js("document.querySelector('#chat-messages').scrollTop"), 0);
    assert.equal(await js("document.querySelector('.chat-jump').hidden"), false);
    const finalText = pending.text + '\nFinal detail.';
    chats.s2.messages.push({ role: 'assistant', content: finalText });
    pending.res.end('data: {"done":true}\n\n'); pending = null;
    await waitFor("document.querySelector('#chat-send').dataset.mode!=='stop'");
    assert.equal(await js("document.querySelector('.chat-activity')?.dataset.state"), 'done');
    await open('s2');
    assert.equal(await js("document.querySelectorAll('.msg.assistant').length"), 1, 'Finished retention never duplicates a persisted reply');
    // Truncated SSE must fail, not masquerade as a successful reply.
    await js("document.querySelector('#chat-input').value='Interrupted turn'; document.querySelector('#chat-send').click()");
    await waitFor("document.querySelector('#chat-send').dataset.mode==='stop'");
    while (!pending) await delay(20);
    pending.res.end(); pending = null;
    await waitFor("!!document.querySelector('.msg-interrupted')");
    assert.equal(await js("[...document.querySelectorAll('.chat-activity')].at(-1).dataset.state"), 'failed');
    assert.equal(await js("document.querySelectorAll('.msg.assistant').length"), 2);

    // -- stopping a running turn (David's ask 2026-09-15, the foundation the
    // voice work needs for barge-in). Before this there was no cancellation
    // at all: chatStream.js notes the request "was never actually cancelled",
    // so a long or wrong reply had to be waited out.
    await js("document.querySelector('#chat-input').value='Stop this one'; document.querySelector('#chat-send').click()");
    await waitFor("document.querySelector('#chat-send').dataset.mode==='stop'");
    while (!pending) await delay(20);
    pending.res.write('data: ' + JSON.stringify({ chunk: 'Partial answer before the stop.' }) + '\n\n');
    await waitFor("document.querySelector('#chat-messages').textContent.includes('Partial answer before the stop')");
    await js("document.querySelector('#chat-send').click()");
    await waitFor("!!document.querySelector('.msg-stopped')");
    // A stop is not a failure: no error styling and no error toast.
    // Scoped to the message that was stopped: an earlier check in this suite
    // deliberately produces a genuinely failed message, so a document-wide
    // count would fail for the wrong reason.
    assert.equal(await js("[...document.querySelectorAll('.msg.assistant')].at(-1).classList.contains('msg-failed')"), false, 'Stopping must not render as a failure');
    assert.equal(await js("[...document.querySelectorAll('.chat-activity')].at(-1).dataset.state"), 'stopped');
    // Whatever had streamed stays on screen, because the backend saved it.
    assert.ok(await js("document.querySelector('#chat-messages').textContent.includes('Partial answer before the stop')"), 'Partial reply survives the stop');
    // The button returns to send, so the chat is immediately usable again.
    await waitFor("document.querySelector('#chat-send').dataset.mode==='send'");
    try { pending.res.end(); } catch (e) {}
    pending = null;


    // -- dictation (David's ask 2026-09-15). The no-model path is checked
    // because it needs no microphone: the button verifies a model exists
    // BEFORE opening the mic, so the failure arrives before recording rather
    // than after a user has already spoken into it.
    assert.ok(await js("!document.querySelector('#chat-mic').hidden"), 'Mic button shows where recording is supported');
    await js("document.querySelector('#chat-mic').click()");
    await waitFor("[...document.querySelectorAll('.toast')].some(t=>t.textContent.includes('No speech model'))");
    // Nothing should be recording after that refusal.
    assert.ok(await js("!document.querySelector('#chat-mic').classList.contains('recording')"), 'A refused start leaves the mic idle');
    await js("document.querySelectorAll('.toast').forEach(t=>t.remove())");
    // With the engine missing entirely the message is different, because a
    // broken build and a missing download are not the same problem.
    speechStatus = { engine_available: false, active_model: null, models: [] };
    await js("import('/static/js/voiceInput.js').then(m=>m.refreshSpeechStatus())");
    await js("document.querySelector('#chat-mic').click()");
    await waitFor("[...document.querySelectorAll('.toast')].some(t=>t.textContent.includes('not available in this build'))");
    await js("document.querySelectorAll('.toast').forEach(t=>t.remove())");
    speechStatus = { engine_available: true, active_model: null, models: [] };
    await js("import('/static/js/voiceInput.js').then(m=>m.refreshSpeechStatus())");


    // -- Open Mic (David's ask 2026-09-15). Driven through the no-model
    // refusal, which needs no microphone: the toggle checks a model exists
    // before opening the device, same as the dictation button.
    assert.ok(await js("!document.querySelector('#chat-openmic').hidden"), 'Open Mic button shows where recording is supported');
    speechStatus = { engine_available: true, active_model: null, models: [] };
    await js("import('/static/js/voiceInput.js').then(m=>m.refreshSpeechStatus())");
    await js("document.querySelector('#chat-openmic').click()");
    await waitFor("[...document.querySelectorAll('.toast')].some(t=>t.textContent.includes('No speech model'))");
    assert.ok(await js("!document.querySelector('#chat-openmic').classList.contains('active')"), 'A refused start does not enter Open Mic');
    // Starting Open Mic on an empty chat creates the session first, which is
    // how a chat begins as an Open Mic session. Asserted by the refusal
    // reaching the model check at all: with no session it would have stopped
    // earlier with a different message.
    assert.ok(await js("!!document.querySelector('#chat-openmic')"), 'Open Mic is reachable from a new chat');
    assert.ok(await js("!document.querySelector('#chat-main').classList.contains('open-mic')"));
    await js("document.querySelectorAll('.toast').forEach(t=>t.remove())");
    // Sentence chunking is the part with real behaviour, and driving it
    // through live speech synthesis would be slow and machine-dependent.
    assert.deepEqual(
      await js("import('/static/js/voiceOutput.js').then(m=>m._chunk('First one. Second one. Third one. Fourth one.'))"),
      ['First one.', 'Second one. Third one.', 'Fourth one.'],
      'First sentence ships alone, then two-sentence breaths',
    );
    // A trailing sentence with no terminal punctuation must still be spoken;
    // that is the case voice-line's chunker exists for.
    assert.deepEqual(
      await js("import('/static/js/voiceOutput.js').then(m=>m._chunk('Hello there. And then this trails off'))"),
      ['Hello there.', 'And then this trails off'],
    );
    // Markdown is written to be read, not heard.
    assert.equal(
      await js("import('/static/js/voiceOutput.js').then(m=>m._forSpeech(['## Title','','**bold** and `code` and [a link](http://x.test)','- item'].join(String.fromCharCode(10))))"),
      'Title bold and code and a link item',
    );

    // Open Mic's loop, driven end to end with a fake microphone and a fake
    // voice. This exists because barge-in shipped completely dead: the
    // microphone was closed while the assistant spoke, so no level ever
    // reached the handler and the interrupt branch could not run. Nothing
    // caught it, because openMic.js hard-imported the recorder and no suite
    // could drive the machine without real hardware. It is injectable now, so
    // this test drives the actual state machine rather than a copy of it.
    await js(`(async () => {
      window.__bargeLog = [];
      const m = await import('/static/js/openMic.js');
      // A real, decodable WAV: the transcription path runs WebAudio for real
      // rather than being stubbed out, so a regression there fails here too.
      const wav = (seconds) => {
        const rate = 16000, n = Math.floor(rate * seconds);
        const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
        const w = (o, t) => { for (let i = 0; i < t.length; i++) v.setUint8(o + i, t.charCodeAt(i)); };
        w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
        v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
        v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true);
        v.setUint16(32, 2, true); v.setUint16(34, 16, true);
        w(36, 'data'); v.setUint32(40, n * 2, true);
        for (let i = 0; i < n; i++) v.setInt16(44 + i * 2, Math.sin(i / 8) * 8000, true);
        return new Blob([buf], { type: 'audio/wav' });
      };
      window.__counts = { captured: 0, stopped: 0, closed: 0, cancelledSpeech: 0, speakerResets: 0 };
      const fakeMic = () => ({
        async start() {},
        capture() { window.__counts.captured++; },
        async stop() { window.__counts.stopped++; return wav(0.5); },
        close() { window.__counts.closed++; },
        cancel() { window.__counts.closed++; },
      });
      window.__spoken = [];
      // Mirrors the real speaker's latch: once cancelled it stays silent until
      // reset. That is the behaviour that made the app go quiet after the
      // first interruption, so the fake has to reproduce it or the test would
      // pass against the bug.
      let speakerCancelled = false;
      const fakeSpeaker = () => ({
        feed(text) { if (!speakerCancelled) window.__spoken.push(text); },
        finish() {},
        reset() { speakerCancelled = false; window.__counts.speakerResets++; },
        cancel() { speakerCancelled = true; window.__counts.cancelledSpeech++; },
      });
      window.__mic = m.createOpenMic({
        sessionId: 's1',
        // Never resolves, so the turn is still in flight when the interrupt
        // arrives - which is the only state barge-in is about.
        sendMessage: (text, handlers) => {
          window.__bargeLog.push('sent:' + text);
          // Streams one chunk so the speaker is exercised, then never settles:
          // the turn has to still be in flight when the interruption arrives.
          handlers.onChunk('a reply sentence.');
          return new Promise(() => {});
        },
        stopTurn: () => { window.__bargeLog.push('stopTurn'); },
        onState: (s) => window.__bargeLog.push('state:' + s),
        createMic: fakeMic,
        makeSpeaker: fakeSpeaker,
      });
      await window.__mic.start();
      return true;
    })()`);
    assert.equal(await js("window.__mic.state"), 'listening', 'Open Mic starts listening');
    assert.equal(await js("window.__counts.captured"), 0, 'An open microphone is not recording yet');

    await js("window.__mic._level(0.5)");
    assert.equal(await js("window.__counts.captured"), 1, 'Recording starts when speech does, not when the device opens');
    await delay(420);
    await js("window.__mic._level(0.5)");
    await delay(950);
    await js("window.__mic._level(0.01)");
    await waitFor("window.__mic.state==='thinking'");
    assert.ok(await js("window.__bargeLog.includes('sent:what is the weather')"), 'The transcript is sent as an ordinary turn');
    assert.equal(await js("window.__counts.stopped"), 1, 'The utterance ended without closing the device');
    assert.equal(await js("window.__counts.closed"), 0, 'The microphone stays open through the reply');

    assert.deepEqual(await js("window.__spoken"), ['a reply sentence.'], 'The reply is spoken');

    // An isolated peak must not interrupt: the microphone hears the assistant
    // through the speakers, and one syllable of its own voice coming back
    // would make it talk over itself.
    await js("window.__mic._level(0.5)");
    await js("window.__mic._level(0.01)");
    await js("window.__mic._level(0.01)");
    await js("window.__mic._level(0.01)");
    assert.ok(!(await js("window.__bargeLog.includes('stopTurn')")), 'An isolated peak does not interrupt');

    // Real speech read through a level meter is a BROKEN run of loud readings,
    // not an unbroken one - it dips between syllables. Requiring consecutive
    // frames is what made interrupting do nothing while David talked over it
    // for seconds, so the pattern below is deliberately gappy.
    await js("window.__mic._level(0.5)");
    await js("window.__mic._level(0.05)");
    await js("window.__mic._level(0.5)");
    assert.ok(!(await js("window.__bargeLog.includes('stopTurn')")), 'Two loud readings are still not enough');
    await js("window.__mic._level(0.5)");
    assert.ok(await js("window.__bargeLog.includes('stopTurn')"), 'Speech with normal gaps cuts the assistant off');
    assert.equal(await js("window.__counts.cancelledSpeech"), 1, 'Barge-in silences the voice as well as the turn');
    assert.equal(await js("window.__counts.captured"), 2, 'The interruption is recorded as the next utterance');

    // The turn AFTER an interruption must still be audible. The speaker's
    // cancel latch was never cleared, so the first barge-in silenced the rest
    // of the session: text kept appearing, nothing was ever spoken again.
    await delay(420);
    await js("window.__mic._level(0.5)");
    await delay(950);
    await js("window.__mic._level(0.01)");
    await waitFor("window.__counts.speakerResets>0");
    await waitFor("window.__spoken.length>1");
    assert.deepEqual(await js("window.__spoken"), ['a reply sentence.', 'a reply sentence.'],
      'A reply after an interruption is still spoken aloud');
    await js("window.__mic.stop(); window.__mic=null;");

    // Inject dangerous content through the real renderer, not a stub parser.
    const attack = '<img src="https://example.test/track" onerror="window.__xss=1"><iframe src="/api/settings"></iframe><script>window.__xss=1</script><p class="artifact-panel">safe</p>[bad](javascript:alert(1))';
    await js(`import('/static/js/chatContent.js').then(m => { const node=document.createElement('div'); node.id='security-test'; document.body.append(node); m.renderMessageBody(node, ${JSON.stringify(attack)}, 's1'); })`);
    assert.equal(await js("document.querySelectorAll('#security-test img, #security-test iframe, #security-test script, #security-test .artifact-panel, #security-test a').length"), 0);
    await js("document.querySelector('#security-test').remove()");
    // Set here rather than earlier because the "Use CLI default" step above
    // legitimately clears it (a model change invalidates the reading). The
    // composer-overflow assertion below is only meaningful with the meter
    // actually present in the row.
    chats.s1.context_state = { used_tokens: 45000, capacity_tokens: 90000, percent: 50, estimated_capacity: false, capacity_source: 'cli_cache', model: 'catalog-astra' };
    for (const width of [390, 320]) {
      win.setContentSize(width, 844); await open('s1'); await delay(100);
      await waitFor("!document.querySelector('#context-pill').hidden");
      assert.ok(await js("document.querySelector('.chat-input-bar').scrollWidth <= document.querySelector('.chat-input-bar').clientWidth+2"), 'Composer fits mobile ' + width);
      await js("document.querySelector('#chat-messages').scrollTop=0");
      await capture('mobile-chat-' + width);
      await js("document.querySelector('.artifact-card').click()");
      await waitFor("!!document.querySelector('.artifact-document')");
      await delay(250); // let the deliberate slide-in animation settle
      assert.ok(await js("document.querySelector('.artifact-panel').getBoundingClientRect().right <= innerWidth+1"));
      await capture('mobile-artifact-' + width);
      await js("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape'}))");
      assert.equal(await js("document.querySelectorAll('.artifact-panel').length"), 0);
    }
    // A fresh landing retains its draft/files through model selection and
    // first-send creation. No live uploads or providers are contacted.
    win.setContentSize(1440, 900);
    await js("document.querySelector('.chat-header-new').click()");
    await waitFor("document.querySelector('#model-picker-label').textContent==='Choose model'");
    const stageDraft = async () => {
      await js("{ document.querySelector('#chat-input').value='Keep this draft'; const transfer=new DataTransfer(); transfer.items.add(new File(['draft content'],'draft.txt',{type:'text/plain'})); const fileInput=document.querySelector('input[type=file]'); fileInput.files=transfer.files; fileInput.dispatchEvent(new Event('change')); }");
      await waitFor("document.querySelector('.attach-chip')?.textContent.includes('draft.txt')");
    };
    await stageDraft();
    await js("document.querySelector('#model-picker-btn').click(); document.querySelector('#model-picker-menu button').click()");
    await waitFor("document.querySelector('#model-picker-label').textContent==='Claude Code'");
    assert.ok(await js("document.querySelector('.attach-chip')?.textContent.includes('draft.txt') && document.querySelector('#chat-input').value==='Keep this draft'"));
    win.setContentSize(320, 420);
    await delay(400);
    await js("document.querySelector('#model-version-btn').click()");
    assert.ok(await js("(() => {const r=document.querySelector('#model-version-menu').getBoundingClientRect();return r.top>=0&&r.left>=0&&r.right<=innerWidth&&r.bottom<=innerHeight})()"), 'Centered model-version menu fits short mobile viewport');
    win.setContentSize(1440, 900);
    await js("document.body.click()");
    await js("document.querySelector('.chat-header-new').click()");
    await waitFor("document.querySelector('#model-picker-label').textContent==='Choose model'");
    await stageDraft();
    await js("document.querySelector('#chat-send').click()");
    await waitFor("document.querySelectorAll('.chat-prose p').length > 20");
    assert.deepEqual(requests.filter(r=>r.path==='/api/chat/stream').at(-1).data.attachment_ids, ['staged-test']);
    assert.equal(requests.filter(r=>r.path==='/api/chat/stream').at(-1).data.message, 'Keep this draft');
    // A tool asks for something it has not been granted, mid-reply. The prompt
    // must appear in the chat that asked, show the request as text, and refuse
    // by default if it is dismissed.
    pending.res.write('data: ' + JSON.stringify({ permission: {
      id: 'req-1', tool: 'Bash', target: 'npm test',
      title: 'Run a command', description: 'npm test -- --watch',
      arguments: '{"command": "npm test -- --watch <img src=x onerror=alert(1)>"}',
      choices: [
        { id: 'once', label: 'Allow once', behavior: 'allow', scope: 'once' },
        { id: 'session', label: 'Always in this chat (npm test)', behavior: 'allow', scope: 'session' },
        { id: 'forever', label: 'Always allow npm test', behavior: 'allow', scope: 'forever' },
        { id: 'reject', label: 'Reject', behavior: 'deny', scope: 'once' },
      ] } }) + '\r\n\r\n');
    await waitFor("!!document.querySelector('.permission-dialog[open]')");
    assert.equal(await js("document.querySelector('.permission-target').textContent"), 'npm test', 'The prompt states what a grant would cover');
    assert.equal(await js("document.querySelectorAll('.permission-dialog img').length"), 0, 'A request is shown as text, never as markup');
    assert.equal(await js("document.querySelectorAll('.permission-choices button').length"), 4);
    await js("document.querySelector('.permission-choices [data-choice=once]').click()");
    await waitFor("!document.querySelector('.permission-dialog')");
    assert.deepEqual(answers.map(a => a.choice), ['once'], 'The answer reaches the server');

    // Dismissing without choosing refuses rather than hanging.
    pending.res.write('data: ' + JSON.stringify({ permission: {
      id: 'req-2', tool: 'Bash', target: 'rm -rf', title: 'Run a command', description: '',
      arguments: '{"command": "rm -rf /"}',
      choices: [
        { id: 'once', label: 'Allow once', behavior: 'allow', scope: 'once' },
        { id: 'reject', label: 'Reject', behavior: 'deny', scope: 'once' },
      ] } }) + '\r\n\r\n');
    await waitFor("!!document.querySelector('.permission-dialog[open]')");
    await js("document.querySelector('.permission-dialog').close()");
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.deepEqual(answers.map(a => a.choice), ['once', 'reject'], 'Closing the prompt refuses');

    pending.res.end('data: {"done":true}\n\n'); pending = null;
    await waitFor("document.querySelector('#chat-send').dataset.mode!=='stop'");
    await win.webContents.debugger.sendCommand('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
    await waitFor("matchMedia('(prefers-reduced-motion: reduce)').matches");
    await js("document.querySelector('.chat-header-new').click()");
    // Chromium can report an inherited scrollbar-color transition here;
    // only a transform animation represents composer movement.
    const runningAnimations = await js("document.querySelector('.chat-composer-dock').getAnimations().filter(a=>a.playState==='running' && a.effect.getKeyframes().some(frame=>'transform' in frame)).map(a=>a.effect.getKeyframes())");
    assert.deepEqual(runningAnimations, [], 'Reduced motion skips composer animation: ' + JSON.stringify(runningAnimations));
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: true, checks: ['centered landing and same-composer transition', 'first-message attachments and model-selection draft retention', 'reduced motion', 'rich Markdown and highlighting', 'model dispatch controls and reload persistence', 'artifact previews and Office fallback', 'isolated HTML and XSS filtering', 'session reattachment without duplicates', 'scroll position preservation', 'explicit stream completion', '320/390px mobile layouts'], requests, errors }, null, 2));
    console.log('PASS ' + output);
  } catch (error) {
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: false, error: error.stack, errors }, null, 2));
    await capture('failure'); console.error(error); process.exitCode = 1;
  } finally { win.destroy(); server.close(); exitAfterFlush(process.exitCode); }
});
