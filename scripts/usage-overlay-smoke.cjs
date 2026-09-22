// Isolated Electron smoke for the desktop usage notch. No real backend, CLI or
// account call: the page is served from a local stub with synthetic readings,
// and the main-process side of its bridge is stubbed here to record what the
// page asks for. Run: node scripts/run-electron-test.cjs usage-overlay-smoke
const { app, BrowserWindow, ipcMain } = require('electron');
const { exitAfterFlush } = require('./electron-exit.cjs');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const output = path.join(root, 'data', 'usage-overlay-review');
const now = Date.now() / 1000;
let claudeStatus = 'ok';
const refreshes = [];
function quota() {
  const claude = claudeStatus === 'needs_sign_in'
    ? { provider: 'claude', status: 'needs_sign_in', stale: true, windows: [], note: 'Sign-in has expired or is missing. Open the CLI once to renew it.' }
    : { provider: 'claude', status: 'ok', stale: false, note: '', updated_at: now, windows: [
      { name: '5-hour', used_percent: 24, resets_at: now + 3600 * 2 },
      { name: 'Weekly', used_percent: 61, resets_at: now + 86400 * 3 },
    ] };
  return { providers: [claude, { provider: 'codex', status: 'ok', stale: false, note: '', updated_at: now, windows: [
    { name: '5-hour', used_percent: 86, resets_at: now + 1500 },
    { name: 'Weekly', used_percent: 12, resets_at: now + 86400 * 5 },
  ] }], recorded: [] };
}
const files = {
  '/usage-overlay': ['static/usage-overlay.html', 'text/html'],
  '/static/css/usage-overlay.css': ['static/css/usage-overlay.css', 'text/css'],
  '/static/js/usage-overlay.js': ['static/js/usage-overlay.js', 'text/javascript'],
};
const server = http.createServer((req, res) => {
  if (req.url === '/api/models/quotas' || req.url === '/api/models/quotas/refresh') {
    if (req.method === 'POST') {
      let body = ''; req.on('data', c => { body += c; });
      req.on('end', () => { refreshes.push(JSON.parse(body).provider); res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(quota())); });
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(quota()));
    return;
  }
  const markMatch = /^\/static\/img\/model-marks\/([a-z0-9-]+\.svg)$/.exec(req.url);
  if (markMatch) {
    servedMarks.push(markMatch[1]);
    res.writeHead(200, { 'Content-Type': 'image/svg+xml' });
    res.end(fs.readFileSync(path.join(root, 'static/img/model-marks', markMatch[1])));
    return;
  }
  const file = files[req.url];
  if (!file) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': file[1] });
  res.end(fs.readFileSync(path.join(root, file[0])));
});
const servedMarks = [];
const interactive = [];
let openedApp = 0;
ipcMain.on('usage-overlay:interactive', (_e, on) => interactive.push(on));
ipcMain.on('usage-overlay:open-app', () => { openedApp += 1; });
ipcMain.handle('usage-overlay:config', () => ({ edge: 'right', foldOnHover: true }));
let win;
const delay = ms => new Promise(r => setTimeout(r, ms));
async function run() {
  await app.whenReady();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  fs.mkdirSync(output, { recursive: true });
  // Shown, but placed off every screen: the pill's clip-path transition runs on the
  // compositor, which a hidden window never advances, so a hidden window's capture
  // showed the pill frozen half-unfolded while the page itself was correct.
  win = new BrowserWindow({ x: -5000, y: -5000, width: 360, height: 460, show: true, skipTaskbar: true, focusable: false,
    backgroundColor: '#3a4a5c', // a dark stand-in for the desktop, so the black notch is visible in screenshots
    webPreferences: { contextIsolation: true, nodeIntegration: false, backgroundThrottling: false,
      preload: path.join(root, 'electron/usage-overlay-preload.js') } });
  await win.loadURL(`http://127.0.0.1:${server.address().port}/usage-overlay`);
  const js = code => win.webContents.executeJavaScript(code);
  const waitFor = async (cond) => { for (let i = 0; i < 60; i++) { if (await js(cond)) return; await delay(50); } throw new Error('Timed out: ' + cond); };
  const move = (x, y) => js(`document.dispatchEvent(new MouseEvent('mousemove',{clientX:${x},clientY:${y},bubbles:true}))`);

  // At rest: folded to the sliver, both rings drawn behind the clip, headline percents shown.
  await waitFor("document.querySelectorAll('#pill .cell').length === 2");
  assert.equal(await js("document.body.classList.contains('folded')"), true, 'starts folded');
  assert.deepEqual(await js("[...document.querySelectorAll('.pct')].map(n => n.textContent)"), ['24%', '86%']);
  await delay(600);  // capturing before the first frame has painted fails with UnknownVizError
  fs.writeFileSync(path.join(output, 'folded.png'), (await win.webContents.capturePage()).toPNG());

  // Reaching the sliver unfolds it and takes clicks; the Codex ring is past 80%, so it is red.
  const rest = await js("(r => [r.left + r.width / 2, r.top + r.height / 2])(document.getElementById('rest').getBoundingClientRect())");
  await move(rest[0], rest[1]);
  await waitFor("!document.body.classList.contains('folded')");
  assert.equal(interactive[interactive.length - 1], true, 'pointer over the notch takes clicks');
  assert.match(await js("document.querySelector('.cell[data-p=codex] svg.reading').innerHTML"), /#FF3F00/i);

  // Hovering a ring opens its card, CodeNotch's layout: title, windows, reset copy, used and left.
  const ring = await js("(r => [r.left + r.width / 2, r.top + r.height / 2])(document.querySelector('.cell[data-p=claude] .ringwrap').getBoundingClientRect())");
  await delay(400);
  await move(ring[0], ring[1]);
  await waitFor("document.getElementById('card').classList.contains('show')");
  const cardText = await js("document.getElementById('card').innerText");
  assert.match(cardText, /Claude Usage/);
  assert.match(cardText, /5-hour limit/);
  assert.match(cardText, /Weekly limit/);
  assert.match(cardText, /24% Used · 76% left/);
  assert.match(cardText, /Resets/);
  await delay(250);
  fs.writeFileSync(path.join(output, 'card.png'), (await win.webContents.capturePage()).toPNG());

  // Clicking a ring refreshes that provider.
  await js(`(() => { const r = document.querySelector('.cell[data-p=claude] .ringwrap').getBoundingClientRect();
    const o = { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0, bubbles: true };
    document.getElementById('pill').dispatchEvent(new MouseEvent('mousedown', o)); document.dispatchEvent(new MouseEvent('mouseup', o)); })()`);
  await waitFor(`${JSON.stringify(refreshes)}.length >= 0`);
  for (let i = 0; i < 40 && !refreshes.includes('claude'); i++) await delay(50);
  assert.deepEqual(refreshes, ['claude'], 'a ring click refreshes only its own provider');

  // Moving away gives clicks back to the desktop and folds again.
  await move(20, 20);
  await waitFor("document.body.classList.contains('folded')");
  assert.equal(interactive[interactive.length - 1], false, 'pointer off the notch passes clicks through');

  // Signed out: a dash on the ring and the sign-in note on the card.
  claudeStatus = 'needs_sign_in';
  await js('refresh()');
  await waitFor("document.querySelector('.cell[data-p=claude] .pct').textContent === '—'");
  await move(rest[0], rest[1]);
  await waitFor("!document.body.classList.contains('folded')");
  await delay(400);
  await move(ring[0], ring[1]);
  await waitFor("document.getElementById('card').innerText.includes('Sign in to Claude Code')");

  // Real logos, not letters: Claude's mark and the OpenAI mark for Codex, loaded and in use.
  assert.deepEqual([...new Set(servedMarks)].sort(), ['claude.svg', 'openai.svg']);
  assert.match(await js("getComputedStyle(document.querySelector('.cell[data-p=codex] .mark')).maskImage"), /openai\.svg/);
  assert.match(await js("document.querySelector('#card .c-head .mark').style.getPropertyValue('--mark')"), /claude\.svg/);

  // Every edge: the pill against that edge and the card fully inside the window, never clipped.
  for (const [edge, width, height] of [['left', 360, 650], ['top', 650, 650], ['bottom', 650, 650], ['right', 360, 650]]) {
    win.setContentSize(width, height);
    win.webContents.send('usage-overlay:config', { edge, foldOnHover: false });
    await waitFor(`document.body.dataset.edge === '${edge}' && !document.body.classList.contains('folded')`);
    await delay(450);
    const ringAt = await js("(r => [r.left + r.width / 2, r.top + r.height / 2])(document.querySelector('.cell[data-p=codex] .ringwrap').getBoundingClientRect())");
    await move(ringAt[0], ringAt[1]);
    await waitFor("document.getElementById('card').classList.contains('show') && document.getElementById('card').innerText.includes('Codex Usage')");
    await delay(300);
    const geometry = await js(`(() => { const box = el => { const r = document.getElementById(el).getBoundingClientRect(); return { l: r.left, t: r.top, r: r.right, b: r.bottom }; };
      return { W: innerWidth, H: innerHeight, pill: box('pill'), card: box('card') }; })()`);
    const { W, H, pill, card } = geometry;
    const touching = { left: Math.round(pill.l) === 0, right: Math.round(pill.r) === W, top: Math.round(pill.t) === 0, bottom: Math.round(pill.b) === H }[edge];
    assert.ok(touching, `${edge}: pill is flush against its edge ${JSON.stringify(geometry)}`);
    assert.ok(card.l >= 0 && card.t >= 0 && card.r <= W && card.b <= H, `${edge}: card inside the window ${JSON.stringify(geometry)}`);
    fs.writeFileSync(path.join(output, `edge-${edge}.png`), (await win.webContents.capturePage()).toPNG());
    await move(-1, -1);
  }

  // Nothing the page renders may come from a script-capable source.
  assert.equal(await js("document.querySelectorAll('#card script, #card img, #card iframe').length"), 0);
  console.log('PASS: usage notch folded/unfold, card, click-through, ring refresh, signed-out state, logos, all four edges. Screenshots: ' + output);
}
run().then(() => exitAfterFlush(0), (error) => { console.error(error); exitAfterFlush(1); }).finally(() => {
  if (win && !win.isDestroyed()) win.destroy();
  server.close();
});
