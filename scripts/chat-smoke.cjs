// Real Chromium, synthetic sessions and loopback-only traffic. No live backend.
const { app, BrowserWindow, session } = require('electron');
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
  '/generated-files/012345abcdef_report.docx': { filename: 'report.docx', extension: 'docx', kind: 'download', text: 'synthetic office file' },
  '/generated-files/012345abcdef_snippet.py': { filename: 'snippet.py', extension: 'py', kind: 'text', text: 'print("Hello")' },
  '/generated-files/012345abcdef_preview.pdf': { filename: 'preview.pdf', extension: 'pdf', kind: 'pdf', text: samplePDF() },
  '/generated-files/012345abcdef_image.png': { filename: 'image.png', extension: 'png', kind: 'image', text: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jX1sAAAAASUVORK5CYII=', 'base64') },
};
const rich = '# A clearer direction\n\nHere is a **focused plan**, with `useful details` and [a reference](https://example.test).\n\n## Next steps\n\n1. Keep the important work visible.\n2. Make room for deeper thinking.\n\n> Good tools stay out of your way.\n\n| Area | Direction |\n| --- | --- |\n| Chat | Clear, readable responses |\n| Files | Preview without leaving |\n\n```python\ndef greet(name):\n    return f"Hello, {name}"\n```\n\n[Project brief](/generated-files/012345abcdef_Project%20brief.md)\n\n[Static preview](/generated-files/012345abcdef_preview.html)\n\n[Word document](/generated-files/012345abcdef_report.docx)\n\n[Python source](/generated-files/012345abcdef_snippet.py)';
const models = [{ id: 'claude', name: 'Claude Code', kind: 'claude_cli', model: 'configured-model' }, { id: 'codex', name: 'Codex CLI', kind: 'codex_cli', model: '' }, { id: 'local', name: 'Local', kind: 'local', model: 'local-model' }];
const chats = {
  s1: { id: 's1', title: 'A focused workspace', model_endpoint_id: 'claude', messages: [{ role: 'user', content: 'Help me shape this into a clear plan.' }, { role: 'assistant', content: rich }] },
  s2: { id: 's2', title: 'Another conversation', model_endpoint_id: 'codex', messages: [] },
};
let pending = null;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    res.setHeader('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'");
    res.end('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/css/style.css"><link rel="stylesheet" href="/static/css/chat.css"></head><body><div id="view-content" class="view active" style="height:100vh"></div></body></html>'); return;
  }
  if (url.pathname.startsWith('/api/')) {
    let body = ''; for await (const chunk of req) body += chunk;
    const data = body ? JSON.parse(body) : {};
    requests.push({ path: url.pathname, method: req.method, data });
    const json = value => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)); };
    if (url.pathname === '/api/models') return json(models);
    if (url.pathname === '/api/projects') return json([]);
    if (url.pathname === '/api/sessions') return json(Object.values(chats));
    const match = url.pathname.match(/^\/api\/sessions\/([^/]+)(\/model)?$/);
    if (match) {
      const chat = chats[match[1]];
      if (match[2]) { Object.assign(chat, data); return json({ ok: true, ...data }); }
      return json(chat);
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
      res.write('data: ' + JSON.stringify({ chunk: text }) + '\r\n\r\n');
      pending = { res, sid: data.session_id, text };
      return;
    }
    res.writeHead(404); json({ detail: 'Missing fixture: ' + url.pathname }); return;
  }
  const target = path.resolve(root, '.' + url.pathname);
  if (!target.startsWith(path.join(root, 'static') + path.sep)) { res.writeHead(404); res.end(); return; }
  try {
    res.setHeader('Content-Type', { '.js': 'text/javascript', '.mjs': 'text/javascript', '.wasm': 'application/wasm', '.css': 'text/css', '.png': 'image/png' }[path.extname(target)] || 'application/octet-stream');
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
  win.webContents.on('console-message', (_e, level, message) => { if (level >= 3) errors.push(message); });
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
    assert.equal(await js("document.querySelectorAll('.artifact-card').length"), 4);
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
    await js("document.querySelector('[aria-label=\"Close file preview\"]').click(); document.querySelectorAll('.artifact-card')[2].click()");
    await waitFor("!!document.querySelector('.artifact-fallback')");
    assert.ok(await js("document.querySelector('.artifact-toolbar a').href.includes('download=true')"));
    await js("import('/static/js/chatContent.js').then(m=>m.openArtifact('s1','/generated-files/012345abcdef_preview.pdf','preview.pdf'))");
    await waitFor("document.querySelector('.artifact-pdf-text')?.textContent.includes('JARVIS preview test')");
    await delay(1000);
    await capture('desktop-pdf-preview');
    await js("import('/static/js/chatContent.js').then(m=>m.openArtifact('s1','/generated-files/012345abcdef_image.png','image.png'))");
    await waitFor("document.querySelector('.artifact-preview > img')?.naturalWidth===1");
    await open('s2');
    assert.equal(await js("document.querySelectorAll('.artifact-panel').length"), 0);
    await js("document.querySelector('#chat-input').value='Build a plan'; document.querySelector('#chat-send').click()");
    await waitFor("document.querySelector('.msg-body').textContent==='Build a plan' && document.querySelectorAll('.chat-prose p').length > 20");
    assert.equal(await js("document.querySelector('#model-version-btn').disabled"), true);
    await open('s1');
    assert.equal(await js("document.querySelector('#chat-send').disabled"), false);
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
    await waitFor("!document.querySelector('#chat-send').disabled");
    await open('s2');
    assert.equal(await js("document.querySelectorAll('.msg.assistant').length"), 1, 'Finished retention never duplicates a persisted reply');
    // Truncated SSE must fail, not masquerade as a successful reply.
    await js("document.querySelector('#chat-input').value='Interrupted turn'; document.querySelector('#chat-send').click()");
    await waitFor("document.querySelector('#chat-send').disabled");
    while (!pending) await delay(20);
    pending.res.end(); pending = null;
    await waitFor("!!document.querySelector('.msg-interrupted')");
    assert.equal(await js("document.querySelectorAll('.msg.assistant').length"), 2);
    // Inject dangerous content through the real renderer, not a stub parser.
    const attack = '<img src="https://example.test/track" onerror="window.__xss=1"><iframe src="/api/settings"></iframe><script>window.__xss=1</script><p class="artifact-panel">safe</p>[bad](javascript:alert(1))';
    await js(`import('/static/js/chatContent.js').then(m => { const node=document.createElement('div'); node.id='security-test'; document.body.append(node); m.renderMessageBody(node, ${JSON.stringify(attack)}, 's1'); })`);
    assert.equal(await js("document.querySelectorAll('#security-test img, #security-test iframe, #security-test script, #security-test .artifact-panel, #security-test a').length"), 0);
    await js("document.querySelector('#security-test').remove()");
    for (const width of [390, 320]) {
      win.setContentSize(width, 844); await open('s1'); await delay(100);
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
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: true, checks: ['rich Markdown and highlighting', 'model dispatch controls and reload persistence', 'artifact previews and Office fallback', 'isolated HTML and XSS filtering', 'session reattachment without duplicates', 'scroll position preservation', 'explicit stream completion', '320/390px mobile layouts'], requests, errors }, null, 2));
    console.log('PASS ' + output);
  } catch (error) {
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ passed: false, error: error.stack, errors }, null, 2));
    await capture('failure'); console.error(error); process.exitCode = 1;
  } finally { win.destroy(); server.close(); app.quit(); }
});
