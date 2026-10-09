// Run: electron/node_modules/electron/dist/electron.exe scripts/ui-smoke.cjs
// Isolated renderer checks. All API responses are synthetic, all writes stay
// in this process, and no request can reach the running JARVIS backend.
const { app, BrowserWindow, session } = require("electron");
const { exitAfterFlush } = require('./electron-exit.cjs');
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const output = path.join(root, "data", "ui-review", "run-" + Date.now());
const updateDocImages = process.argv.includes("--update-doc-images");
const updateChatImage = process.argv.includes("--update-chat-image");
let unavailable = false;
// The synthetic backend, shared with the website's demo (demo/fixtures.js).
const demoState = { empty: false };
let sessionDelay = 0;
const writes = [];
const reads = [];
const now = Date.now() / 1000;
const { fixture, mutate, media } = require("../demo/fixtures.js")({ now, state: demoState });
const computerImage = fs.readFileSync(path.join(root, 'static', 'img', 'computer-fixture.jpg')).toString('base64');
demoState.computerImage = computerImage;
const errors = [];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  // The site (docs/) may read GitHub's public release data; nothing else leaves.
  const site = url.pathname.startsWith("/docs/") || url.pathname.startsWith("/kairos/");
  res.setHeader('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'self' https: http://127.0.0.1:* http://localhost:*; img-src 'self' data: blob:" + (site ? "; connect-src 'self' https://api.github.com" : ""));
  if (url.pathname === "/__github-latest") {
    // Stands in for api.github.com/repos/david-darr/kairos/releases/latest.
    res.setHeader("Content-Type", "application/json"); res.setHeader("Access-Control-Allow-Origin", "*");
    res.end(JSON.stringify({ tag_name: "v2.0.0", assets: [
      { name: "Kairos-Setup-2.0.0.exe", size: 405922807, browser_download_url: "https://github.com/david-darr/kairos/releases/download/v2.0.0/Kairos-Setup-2.0.0.exe" },
      { name: "Kairos-Setup-2.0.0.exe.blockmap", size: 431000, browser_download_url: "https://github.com/david-darr/kairos/releases/download/v2.0.0/Kairos-Setup-2.0.0.exe.blockmap" },
      { name: "Kairos-2.0.0-arm64.dmg", size: 398000000, browser_download_url: "https://github.com/david-darr/kairos/releases/download/v2.0.0/Kairos-2.0.0-arm64.dmg" }] }));
    return;
  }
  if (url.pathname.startsWith("/api/")) {
    if (req.method === 'GET') reads.push({ path: url.pathname, query: Object.fromEntries(url.searchParams) });
    if (req.method === 'GET' && /\/terminals\/[^/]+\/output$/.test(url.pathname)) {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
      let cursor = Number(url.searchParams.get('after') || 0);
      const send = () => {
        url.searchParams.set('after', String(cursor));
        const result = fixture(url);
        for (const event of result.events) { cursor = event.id; res.write(`id: ${event.id}\ndata: ${JSON.stringify({ output: event.data })}\n\n`); }
        if (result.ended) { clearInterval(timer); res.end('event: closed\ndata: {}\n\n'); }
      };
      const timer = setInterval(send, 50); send(); res.on('close', () => clearInterval(timer)); return;
    }
    if (url.pathname === '/api/chat/stream' && req.method === 'POST' && demoState.computerTurn) {
      let body = ''; for await (const chunk of req) body += chunk;
      writes.push({ path: url.pathname, query: Object.fromEntries(url.searchParams), method: req.method, body });
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
      const send = (kind, detail) => res.write(`data: ${JSON.stringify({ run_id: 'r-computer', tool_event: {
        at: now, kind, name: 'computer', ok: true, detail: JSON.stringify(detail) } })}\n\n`);
      send('tool_started', { action: 'open', url: 'https://example.com/' });
      send('tool_finished', { action: 'open', url: 'https://example.com/', image: computerImage });
      let extra = false;
      const timer = setInterval(() => {
        if (demoState.moreComputerActions && !extra) { extra = true; send('tool_started', { action: 'read', url: 'https://example.com/' }); }
        if ((demoState.stoppedOwners || []).includes('chat:s1')) {
          send('tool_finished', { action: 'done', url: 'https://example.com/' });
          res.write('data: {"done":true}\n\n'); res.end(); clearInterval(timer);
        }
      }, 100);
      res.on('close', () => clearInterval(timer));
      return;
    }
    if (/^\/api\/computer\/[^/]+\/frames$/.test(url.pathname) && req.method === 'GET') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store' });
      const send = () => {
        const frame = fixture(url);
        if (!frame.owner) { res.write('event: closed\ndata: {}\n\n'); res.end(); clearInterval(timer); return; }
        res.write(`event: frame\ndata: ${JSON.stringify({ ...frame, image: computerImage })}\n\n`);
      };
      const timer = setInterval(send, 500);
      req.on('close', () => clearInterval(timer));
      send();
      return;
    }
    if (url.pathname === "/api/sessions" && sessionDelay) await delay(sessionDelay);
    if (url.pathname === '/api/chat/artifacts' && (demoState.staleArtifacts || []).includes(url.searchParams.get('url'))) {
      res.writeHead(404, { 'Content-Type': 'application/json' }); res.end('{"detail":"File no longer available"}'); return;
    }
    const file = req.method === 'GET' && media(url);
    if (file) { res.setHeader('Content-Type', file.type); res.end(file.base64 ? Buffer.from(file.base64, 'base64') : file.body); return; }
    res.setHeader("Content-Type", "application/json");
    if (unavailable && url.pathname === "/api/system/status") { res.writeHead(503); res.end('{"detail":"Unavailable"}'); return; }
    if (req.method !== "GET") {
      let body = ""; for await (const chunk of req) body += chunk;
      writes.push({ path: url.pathname, query: Object.fromEntries(url.searchParams), method: req.method, body });
      if (url.pathname === '/api/settings/computer-use') demoState.computerUse = JSON.parse(body);
      let parsed = {}; try { parsed = JSON.parse(body); } catch {}
      const updated = mutate(url.pathname, req.method, parsed, Object.fromEntries(url.searchParams));
      if (updated?._appStart) {
        res.setHeader('Content-Type', 'text/event-stream');
        res.write(`data: ${JSON.stringify(updated._appStart)}\n\n`);
        if (updated._appStart.permission) {
          const request = updated._appStart.permission;
          const timer = setInterval(() => {
            const answer = demoState.forgeAppAnswers[request.id];
            if (!answer) return;
            clearInterval(timer);
            const result = answer === 'once' ? mutate(updated._approvedPath || url.pathname.replace(/\/(start|restart)$/, '/approved'), 'POST', {})._appStart : { error: 'The action was not approved.' };
            res.end(`data: ${JSON.stringify(result)}\n\n`);
          }, 50);
          res.on('close', () => clearInterval(timer));
        } else res.end();
        return;
      }
      if (updated?._status) res.statusCode = updated._status;
      if (url.pathname === "/api/chat/stream") {
        res.setHeader("Content-Type", "text/event-stream");
        for (const packet of updated?._forgePackets || []) {
          res.write(`data: ${JSON.stringify(packet)}\n\n`);
          await delay(150);
        }
        if (demoState.forgeHeldSession === parsed.session_id) {
          res.write('data: {"chunk":"Checking the shell entry point."}\n\n');
          const timer = setInterval(() => {
            if (demoState.forgeRelease) { clearInterval(timer); res.end('data: {"done":true}\n\n'); }
          }, 50);
          res.on('close', () => clearInterval(timer)); return;
        }
        if (updated?.handoffs) { res.end(`data: ${JSON.stringify(updated)}\n\ndata: {"done":true}\n\n`); return; }
        res.end('data: {"chunk":"Tab build request received."}\n\ndata: {"done":true}\n\n'); return;
      }
      res.end(JSON.stringify(updated || (url.pathname === "/api/sessions" ? { id: "s1" } : { ok: true }))); return;
    }
    try { res.end(JSON.stringify(fixture(url))); }
    catch (error) { errors.push(error.message); res.writeHead(404); res.end('{"detail":"Missing fixture"}'); }
    return;
  }
  // /kairos/ is the site's address on GitHub Pages (404.html uses it).
  const pathname = url.pathname.startsWith("/kairos/") ? "/docs/" + url.pathname.slice("/kairos/".length) : url.pathname;
  // A folder serves its index.html, as GitHub Pages does (docs/demo/).
  const tabFile = pathname.match(/^\/tab-files\/([a-z][a-z0-9_]*)\/(view\.(?:js|css))$/);
  const file = path.resolve(root, tabFile ? `tabs/${tabFile[1]}/${tabFile[2]}` :
    pathname === "/" ? "static/index.html" : "." + decodeURIComponent(pathname) + (pathname.endsWith("/") ? "index.html" : ""));
  if (!tabFile && !["static", "docs"].some(dir => file.startsWith(path.join(root, dir) + path.sep))) { res.writeHead(404); res.end(); return; }
  try {
    // .mjs and .wasm for the PDF viewer (pdf.js): a module script served as
    // octet-stream is refused, so the viewer would never fetch its file.
    const mime = { ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html", ".png": "image/png",
      ".svg": "image/svg+xml", ".webp": "image/webp", ".jpg": "image/jpeg", ".woff2": "font/woff2", ".wasm": "application/wasm" };
    res.setHeader("Content-Type", mime[path.extname(file)] || "application/octet-stream");
    res.setHeader("Cache-Control", "no-store");
    res.end(fs.readFileSync(file));
  } catch { res.writeHead(404); res.end(); }
});
app.commandLine.appendSwitch("force-device-scale-factor", "1");
app.setPath("userData", path.join(root, "data", "ui-smoke-profile"));
const delay = (ms) => new Promise(r => setTimeout(r, ms));
app.whenReady().then(async () => {
  fs.mkdirSync(output, { recursive: true });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  const base = "http://127.0.0.1:" + server.address().port;
  const outside = [];
  const docsOnDisk = require("node:url").pathToFileURL(path.join(root, "docs")).href + "/";  // the demo opened from disk
  session.defaultSession.webRequest.onBeforeRequest((details, cb) => {
    if (details.url.startsWith(base + "/") || details.url.startsWith(docsOnDisk)) return cb({});
    outside.push(details.url);
    if (details.url === "https://api.github.com/repos/david-darr/kairos/releases/latest") return cb({ redirectURL: base + "/__github-latest" });
    cb({ cancel: true });
  });
  const win = new BrowserWindow({ width: 1440, height: 900, show: false, useContentSize: true, webPreferences: { offscreen: true, contextIsolation: true, nodeIntegration: false } });
  win.webContents.on("console-message", (details) => { if (details.level === 'error') errors.push(details.message); });
  const js = async (code) => {
    try { return await win.webContents.executeJavaScript(code); }
    catch (error) { throw new Error(error.message + '\nRenderer expression: ' + code); }
  };
  const waitFor = async (condition) => {
    for (let i = 0; i < 80; i++) { if (await js(condition)) return; await delay(50); }
    throw new Error("Timed out: " + condition);
  };
  const capture = async (name) => {
    await delay(250);
    const picture = await win.webContents.capturePage();
    fs.writeFileSync(path.join(output, name + ".png"), picture.toPNG());
  };
  const navigate = async (tab, options = {}) => {
    await js("import('/static/js/app.js').then(m => m.switchTab(" + JSON.stringify(tab) + "," + JSON.stringify(options) + "))");
    await delay(100);
    // Home to Chat animates (a view transition); captures wait for it to end.
    await waitFor("!document.documentElement.classList.contains('tab-transition')");
  };
  const overflow = async () => js(`Array.from(document.querySelectorAll('#view-content, #view-content .view-constrained, .chat-input-bar, .settings-content, .cal-left, .panel-dialog, .panel-dialog .modal-body, .forge-project-sidebar, .forge-workspace, .forge-pane, .forge-composer')).filter(e => e.clientWidth > 0 && e.scrollWidth > e.clientWidth + 2).map(e => ({class: e.className, width:e.clientWidth, scroll:e.scrollWidth}))`);
  const openPopup = async (opener, ready, label) => {
    await js(`window.__popupOpener = ${opener}; window.__popupOpener.focus(); window.__popupOpener.click()`);
    await waitFor(`!!document.querySelector('.modal-panel[role="dialog"][aria-modal="true"]') && (${ready})`);
    await delay(220);
    assert.equal(await js("window.__popupOpener.getAttribute('aria-haspopup')"), 'dialog', label + ' opener announces a dialog');
    assert.equal(await js("window.__popupOpener.hasAttribute('aria-expanded')"), false, label + ' has no inline expanded state');
    assert.equal(await js("document.querySelectorAll('.panel-dialog').length"), 1, label + ' opens one panel');
    assert.ok(await js("document.querySelector('.modal-panel').contains(document.activeElement)"), label + ' moves focus inside');
    assert.ok(await js("!!document.querySelector('.modal-panel button[aria-label=Close]')"), label + ' has a close button');
    assert.ok(await js("getComputedStyle(document.getElementById('view-content')).overflowY === 'hidden'"), label + ' locks page scrolling');
    assert.ok(await js("getComputedStyle(document.querySelector('.modal-panel .modal-body')).overflowY === 'auto'"), label + ' scrolls the panel body');
    await js(`(() => {
      const panel = document.querySelector('.modal-panel');
      const controls = [...panel.querySelectorAll('button, input, textarea, select, a[href], summary, [tabindex]')].filter(n => !n.disabled && n.tabIndex >= 0 && n.getClientRects().length);
      controls.at(-1).focus();
      document.dispatchEvent(new KeyboardEvent('keydown', {key:'Tab', bubbles:true, cancelable:true}));
      window.__trapForward = document.activeElement === controls[0];
      document.dispatchEvent(new KeyboardEvent('keydown', {key:'Tab', shiftKey:true, bubbles:true, cancelable:true}));
      window.__trapBackward = document.activeElement === controls.at(-1);
    })()`);
    assert.ok(await js("window.__trapForward && window.__trapBackward"), label + ' traps focus in both directions');
    if (await js('innerWidth <= 768')) assert.ok(await js("(() => { const r = document.querySelector('.modal-panel').getBoundingClientRect(); return Math.abs(r.left)<1 && Math.abs(r.top)<1 && Math.abs(r.width-innerWidth)<1 && Math.abs(r.height-innerHeight)<1; })()"), label + ' fills the phone viewport');
    assert.deepEqual(await overflow(), [], label + ' popup overflow');
  };
  const closePopup = async (mode, label) => {
    await js(mode === 'backdrop' ? "document.querySelector('.panel-dialog').parentElement.click()" : mode === 'button'
      ? "document.querySelector('.modal-panel button[aria-label=Close]').click()"
      : "document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true, cancelable:true}))");
    await waitFor("!document.querySelector('.panel-dialog')");
    assert.ok(await js("document.activeElement === window.__popupOpener"), label + ' returns focus after ' + mode);
    assert.ok(await js("!document.documentElement.classList.contains('panel-dialog-open')"), label + ' unlocks scrolling');
  };
  const popupDismissals = async (opener, ready, label) => {
    await openPopup(opener, ready, label);
    await closePopup('escape', label);
    await openPopup(opener, ready, label);
    await closePopup('backdrop', label);
  };
  try {
    await win.loadURL(base);
    // Offscreen windows do not receive native focus. Once a page exists,
    // emulate it for real DOM focus/keyboard events.
    win.webContents.debugger.attach("1.3");
    await win.webContents.debugger.sendCommand("Emulation.setFocusEmulationEnabled", { enabled: true });
    await waitFor("document.querySelectorAll('.dashboard-stat').length === 4");
    assert.ok(await js("document.querySelector('.dashboard-hero').contains(document.querySelector('.dashboard-header')) && !document.querySelector('.dashboard-hero').contains(document.querySelector('.dashboard-stats'))"), "Home keeps the header and introduction in one card above the summary strip");
    // Home's banner is the Kairos figure as halftone (static/js/dither.js): still, no status dot, no motion control.
    await waitFor("document.querySelector('.dashboard-core').classList.contains('is-dithered') && !!document.querySelector('.dashboard-core canvas')");
    assert.ok(await js("!document.querySelector('.kairos-sky-image, .kairos-sky-point, .core-motion-toggle, .core-caption, .kairos-mark')"), "Home has no sky scene, dot or motion control");
    assert.ok(await js("Number(document.querySelector('.dashboard-core').dataset.drawMs) < 400"), "Home banner draws quickly");
    await require('./appearance-checks.cjs')({ js, win, waitFor, capture, base, delay });
    await require('./artifact-pane-checks.cjs')({ js, win, waitFor, capture, base, delay, navigate });
    await require('./artifact-viewer-checks.cjs')({ js, win, waitFor, capture, delay, navigate });
    await require('./artifact-review-checks.cjs')({ js, win, waitFor, capture, delay, navigate, writes });
    await require('./artifact-continuity-checks.cjs')({ js, win, waitFor, capture, base, delay, navigate, writes, demoState, reads });
    await js("(async () => { const overlay=document.getElementById('onboarding-overlay'); overlay.classList.remove('hidden'); await import('/static/js/onboarding.js').then(m => m.run(overlay, () => {})); })()");
    assert.equal(await js("document.querySelector('.onboarding-card h2').textContent"), 'Not more time. The right time.');
    await capture('desktop-onboarding');
    win.setContentSize(390, 844); await delay(100);
    await capture('mobile-onboarding');
    win.setContentSize(1440, 900); await delay(350);
    await js("document.getElementById('onboarding-overlay').classList.add('hidden')");
    await require('./model-setup-checks.cjs')({ js, win, waitFor, capture, delay, navigate, demoState, writes });
    await require('./slash-help-checks.cjs')({ js, win, waitFor, capture, delay, navigate, demoState, writes });
    console.log('PASS: model setup desktop/phone onboarding, install approval/progress, sign-ins, API test, local background handoff, skip and re-entry.');
    // Forge Preview uses the same fixture backend, navigation and popup checks.
    const forgeWrites = writes.length;
    for (const [label, width, height] of [['desktop', 1440, 900], ['mobile', 390, 844]]) {
      win.setContentSize(width, height); await delay(100);
      await navigate('home');
      if (label === 'mobile') await js("document.querySelector('#mobile-menu-btn').click()");
      await js("document.querySelector('#forge-mode-switch [data-mode=forge]').click()");
      await waitFor("document.querySelectorAll('.forge-stats .forge-stat').length === 5 && document.querySelectorAll('.forge-widget-grid > section').length === 5");
      assert.deepEqual(await js("[...document.querySelectorAll('#nav [data-tab]')].map(n => n.dataset.tab)"), ['forgeHome'], label + ' Forge navigation');
      assert.deepEqual(await js("[...document.querySelectorAll('.forge-widget-grid > section > header h2')].map(n => n.textContent)"), ['Working now', 'Running services', 'Git activity', 'Recent projects', 'Repo lifespan']);
      await waitFor("!!document.querySelector('.forge-banner canvas') && document.querySelectorAll('.forge-ranked tbody tr').length === 4");
      assert.ok(await js("document.querySelector('.forge-donut svg').getAttribute('role') === 'img' && !!document.querySelector('.forge-heatmap svg[aria-label]')"), label + ' accessible charts');
      await js("document.querySelector('.forge-bar').focus()");
      assert.ok(await js("!document.querySelector('.forge-chart-tooltip').hidden"), label + ' keyboard bar tooltip');
      await js("document.activeElement.blur(); document.getElementById('view-content').scrollTop = 0");
      assert.deepEqual(await overflow(), [], label + ' Forge Home overflow');
      await capture(label + '-forge-home');
      await win.loadURL(base);
      await waitFor("document.querySelectorAll('.forge-stats .forge-stat').length === 5");
      assert.equal(await js("localStorage.getItem('kairos:app-mode')"), 'forge', label + ' mode survives reload');
      const customizeOpener = "document.querySelector('.forge-toolbar button')";
      const customizeReady = "!!document.querySelector('[data-widget-toggle=working]')";
      await openPopup(customizeOpener, customizeReady, label + ' Customize');
      await js("document.querySelector('[data-widget-toggle=working]').click()");
      await closePopup('button', label + ' Customize');
      await win.loadURL(base);
      await waitFor("document.querySelectorAll('.forge-widget-grid > section').length === 4 && document.querySelectorAll('.forge-stats .forge-stat').length === 5");
      assert.ok(await js("![...document.querySelectorAll('.forge-widget-grid > section > header h2')].some(n => n.textContent === 'Working now')"), label + ' hidden widget survives reload');
      await openPopup(customizeOpener, customizeReady, label + ' Customize restore');
      await js("document.querySelector('[data-widget-toggle=working]').click()");
      await closePopup('escape', label + ' Customize restore');
      await js("document.querySelector('[aria-label=\"Repository for lifespan\"]').parentElement.value = 'fp2'; document.querySelector('[aria-label=\"Repository for lifespan\"]').parentElement.dispatchEvent(new Event('change'))");
      await waitFor("document.querySelector('.forge-lifespan .forge-widget-body').textContent === 'Not a git repository'");
      await waitFor("document.querySelectorAll('#nav [data-forge-project]').length === 2");
      assert.ok(await js("!document.querySelector('[data-tab=forgeProjects], [data-tab=forgeSessions]')"), label + ' old views are absent');
      if (width <= 768) await js("document.querySelector('#mobile-menu-btn').click()");
      await js("document.querySelector('[aria-label=\"Add project\"]').click()");
      assert.deepEqual(await js("[...document.querySelectorAll('.forge-project-menu button')].map(b => b.textContent)"), ['Open folder', 'Clone repo', 'New repo'], label + ' project menu');
      await js("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))");
      for (const [kind, text] of [['existing', 'Open folder'], ['clone', 'Clone repo'], ['new', 'New repo']]) {
        await js(`window.__popupOpener = document.querySelector('[aria-label="Add project"]'); window.__popupOpener.click(); [...document.querySelectorAll('.forge-project-menu button')].find(b => b.textContent === ${JSON.stringify(text)}).click()`);
        await waitFor(`!!document.querySelector('[data-forge-form=${kind}] input')`);
        assert.deepEqual(await overflow(), [], label + ' project form fits');
        await closePopup('escape', label + ' ' + kind);
      }
      await js("document.querySelector('[data-forge-project=fp1]').dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true}))");
      assert.deepEqual(await js("[...document.querySelectorAll('.custom-select-menu button')].map(b => b.textContent)"), ['Open on Home', 'Copy path', 'Remove from list'], label + ' project context menu');
      const removeStart = writes.length;
      await js("[...document.querySelectorAll('.custom-select-menu button')].find(b => b.textContent === 'Remove from list').click()");
      await waitFor("!!document.querySelector('.confirm-panel')");
      assert.ok(!writes.slice(removeStart).some(w => w.method === 'DELETE'), label + ' removal waits for confirmation');
      await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Cancel').click()");
      await js("document.querySelector('[data-forge-project=fp1]').dispatchEvent(new MouseEvent('contextmenu', {bubbles:true, cancelable:true})); [...document.querySelectorAll('.custom-select-menu button')].find(b => b.textContent === 'Open on Home').click()");
      await waitFor("document.querySelectorAll('.forge-stats .forge-stat').length === 5");
      assert.equal(await js("document.querySelector('[aria-label=\"Project for new session\"]').parentElement.value"), 'fp1', label + ' Open on Home selects project');
      assert.equal(await js("document.querySelector('[aria-label=\"Where it runs\"]').parentElement.value"), 'new_worktree', label + ' isolated worktree is the default');
      await js("document.querySelector('[aria-label=\"Agent for new session\"]').click()");
      assert.ok(await js("document.querySelector('.custom-select-menu').textContent.includes('Claude') && document.querySelector('.custom-select-menu').textContent.includes('Codex')"), label + ' both coding agents are available');
      await js("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))");
      await js(`(() => { const picker = document.querySelector('[aria-label="Where it runs"]').parentElement; picker.value = 'existing_branch'; picker.dispatchEvent(new Event('change')); })()`);
      await waitFor("!!document.querySelector('[aria-label=\"Branch for new session\"]')");
      await js("document.querySelector('[aria-label=\"Branch for new session\"]').click()");
      assert.ok(await js("[...document.querySelectorAll('.custom-select-menu button')].some(b => b.textContent.includes('main') && b.disabled)"), label + ' checked-out branches are unavailable');
      await js("document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))");
      assert.ok(reads.some(r => r.path === '/api/forge/projects/fp1/branches'), label + ' loads local branches');
      await js(`(() => { const picker = document.querySelector('[aria-label="Branch for new session"]').parentElement; picker.value = 'feature/garden'; picker.dispatchEvent(new Event('change')); })()`);
      assert.ok(await js("!document.querySelector('.forge-composer button[type=submit]').disabled"), label + ' branch selection enables start');
      await js(`(() => { const picker = document.querySelector('[aria-label="Where it runs"]').parentElement; picker.value = 'in_place'; picker.dispatchEvent(new Event('change')); })()`);
      assert.ok(await js("document.querySelector('.forge-location-note.forge-warning').textContent.includes('directly')"), label + ' in-place warning is visible');
      await js(`(() => { const picker = document.querySelector('[aria-label="Where it runs"]').parentElement; picker.value = 'new_worktree'; picker.dispatchEvent(new Event('change')); const agent = document.querySelector('[aria-label="Agent for new session"]').parentElement; agent.value = 'm2'; agent.dispatchEvent(new Event('change')); document.querySelector('#forge-message').value = 'Build the Forge fixture'; })()`);
      // The exact model for that agent, from the same catalog as Chat's picker.
      // customSelect keeps its options in memory, so check the value it accepted and its label.
      await waitFor("!!document.querySelector('[aria-label=\"Model for new session\"]')");
      await js("document.querySelector('[aria-label=\"Model for new session\"]').parentElement.value = 'workspace-fast'");
      assert.equal(await js("document.querySelector('[aria-label=\"Model for new session\"]').parentElement.value"), 'workspace-fast', label + ' exact model is selectable');
      assert.equal(await js("document.querySelector('[aria-label=\"Model for new session\"]').textContent.trim()"), 'Workspace Fast', label + ' exact model shows its name');
      assert.deepEqual(await overflow(), [], label + ' Forge composer overflow');
      await capture(label + '-forge-home-composer');
      // This index belongs to this viewport pass, even when both send the same task.
      const handoffStart = writes.length;
      await require('./forge-first-commit-checks.cjs')({ js, waitFor, capture, writes, demoState, label });
      await waitFor("document.getElementById('view-content').dataset.view === 'forgeShell' && !!document.querySelector('.forge-transcript-layout')");
      await waitFor("document.querySelector('.forge-work-label')?.textContent.includes('Working for')");
      assert.ok(await js("document.querySelector('[aria-label=\"Session mode\"]').disabled"), label + ' mode chip disabled while running');
      for (let i = 0; i < 80 && !writes.slice(handoffStart).some(w => w.path === '/api/chat/stream' && JSON.parse(w.body).message === 'Build the Forge fixture'); i++) await delay(50);
      const handoffWrites = writes.slice(handoffStart);
      const createWrite = handoffWrites.find(w => w.path === '/api/forge/sessions' && w.method === 'POST');
      const streamWrite = handoffWrites.findLast(w => w.path === '/api/chat/stream');
      assert.deepEqual(JSON.parse(createWrite.body), { project_id: 'fp1', task: 'Build the Forge fixture', model_endpoint_id: 'm2', model_override: 'workspace-fast', mode: 'build', isolation: 'new_worktree', branch: null }, label + ' creates the selected Forge session');
      assert.equal(JSON.parse(streamWrite.body).message, 'Build the Forge fixture', label + ' sends the first message');
      assert.ok(handoffWrites.indexOf(createWrite) < handoffWrites.indexOf(streamWrite), label + ' creates Forge before sending');
      assert.ok(!handoffWrites.some(w => w.path === '/api/sessions' || /\/(workspace|model)$/.test(w.path)), label + ' has no Phase 1 chat bridge');
      const forgeSessionId = JSON.parse(streamWrite.body).session_id;
      const forgeBase = '/api/forge/sessions/' + forgeSessionId;
      await waitFor(`document.querySelector('.side-chat-send')?.textContent === 'Send' && document.querySelector('[data-session-id="${forgeSessionId}"]')`);
      assert.equal(await js("document.documentElement.dataset.appMode"), 'forge', label + ' stays in Forge');
      assert.ok(await js("document.querySelector('.forge-session-header').textContent.includes('Kairos garden') && document.querySelector('.forge-session-header').textContent.includes('Codex') && document.querySelector('.forge-session-header').textContent.includes('workspace-fast')"), label + ' session header shows the agent and its exact model');
      assert.ok(await js("!document.querySelector('.forge-project-sidebar').hidden"), label + ' starting opens project sidebar');
      if (width <= 768) await js("document.querySelector('[aria-label=\"Close project sidebar\"]').click()");
      await capture(label + '-forge-shell');
      // App checks have their own write index for each viewport pass.
      const appWriteStart = writes.length;
      if (width > 768) {
        assert.equal(await js("Math.round(document.querySelector('.forge-project-sidebar').getBoundingClientRect().width)"), 228, 'sidebar default width');
        assert.equal(await js("document.querySelector('.forge-project-resizer').getAttribute('aria-valuemin')"), '208');
        assert.equal(await js("document.querySelector('.forge-project-resizer').getAttribute('aria-valuemax')"), '520');
        await js("document.querySelector('.forge-project-resizer').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowLeft', bubbles:true})); document.querySelector('.forge-project-resizer').dispatchEvent(new KeyboardEvent('keydown', {key:'ArrowLeft', bubbles:true}))");
        assert.equal(await js("Math.round(document.querySelector('.forge-project-sidebar').getBoundingClientRect().width)"), 208, 'sidebar minimum width');
        await js("document.querySelector('.forge-project-resizer').dispatchEvent(new KeyboardEvent('keydown', {key:'Home', bubbles:true}))");
      }
      await waitFor("document.querySelector('.forge-transcript-layout .chat-backdrop')?.dataset.scene === 'sky'");
      assert.equal(await js("getComputedStyle(document.querySelector('.forge-transcript-layout .chat-backdrop')).display !== 'none'"), true, label + ' Forge halftone on');
      await js("import('/static/js/appearance.js').then(m => m.updateAppearance({halftone:false}))");
      assert.equal(await js("getComputedStyle(document.querySelector('.forge-transcript-layout .chat-backdrop')).display"), 'none', 'Forge halftone off');
      await js("import('/static/js/appearance.js').then(m => m.updateAppearance({halftone:true}))");
      await js("document.querySelector('.forge-run-app').click()");
      await waitFor("!!document.querySelector('.forge-app-command-input')");
      // A different command per pass must ask again for this project.
      await js(`{ const input=document.querySelector('.forge-app-command-input'); input.value='npm run dev -- --host 127.0.0.1 --port {port} --pass=${label}'; input.dispatchEvent(new Event('input')); [...document.querySelectorAll('.modal-panel button')].find(b => b.textContent === 'Save and run').click(); }`);
      await waitFor("!!document.querySelector('.forge-app-approval')");
      assert.ok(!demoState.forgeApps[forgeSessionId]?.running, label + ' Run app waits for approval');
      assert.ok(await js("document.querySelector('.forge-app-approval pre').textContent.includes('5173') && document.querySelector('.forge-app-approval pre').textContent.includes('Folder:')"), label + ' exact command and folder');
      await js("[...document.querySelectorAll('.forge-app-approval button')].find(b => b.textContent === 'Approve command').click()");
      await waitFor("!!document.querySelector('.forge-app-preview-surface iframe')");
      if (width > 768) assert.ok(await js("document.querySelector('.forge-shell').dataset.split === 'true' && !!document.querySelector('[aria-label=\"Left pane\"] .forge-transcript-layout')"), 'preview opens beside session');
      assert.ok(writes.slice(appWriteStart).some(w => w.path === forgeBase + '/app/start'), label + ' own app start');
      assert.ok(await js("document.querySelector('.forge-preview-fallback').textContent.includes('iframe')"), label + ' labelled fallback');
      await waitFor("!!document.querySelector('.forge-running-strip button') && !document.querySelector('.forge-running-strip').hidden && !!document.querySelector('[data-forge-project=fp1] .forge-app-dot')");
      for (const preset of ['tablet', 'phone', 'desktop']) {
        await js(`document.querySelector('[data-preview-width=${preset}]').click()`);
        assert.equal(await js(`document.querySelector('[data-preview-width=${preset}]').getAttribute('aria-pressed')`), 'true');
      }
      await js("document.querySelector('.forge-preview-logs').open=true");
      await waitFor("document.querySelector('.forge-app-logs').textContent.includes('Ready')");
      if (width <= 768) await js("document.querySelector('[data-preview-width=phone]').click()");
      await capture(label + '-forge-preview');
      const restartStart = writes.length;
      await js("[...document.querySelectorAll('.forge-preview-log-controls button')].find(b => b.textContent === 'Restart').click()");
      await waitFor("!document.querySelector('.forge-preview-log-controls button').disabled");
      assert.ok(writes.slice(restartStart).some(w => w.path === forgeBase + '/app/restart'), label + ' logs restart');
      assert.ok(!await js("!!document.querySelector('.forge-app-approval')"), 'same command already approved');
      await navigate('forgeHome');
      await waitFor(`!!document.querySelector('.forge-running-service[data-app-session="${forgeSessionId}"]')`);
      assert.ok(await js("document.querySelector('.forge-running-service').textContent.includes(':5173')"), label + ' Running services port');
      await js(`document.querySelector('.forge-running-service[data-app-session="${forgeSessionId}"]').click()`);
      await waitFor("!!document.querySelector('.forge-preview-log-controls')");
      await js("document.querySelector('.forge-preview-logs').open=true; [...document.querySelectorAll('.forge-preview-log-controls button')].find(b => b.textContent === 'Stop').click()");
      await waitFor("document.querySelector('.forge-preview-status').textContent.includes('stopped')");
      assert.ok(writes.slice(appWriteStart).some(w => w.path === forgeBase + '/app/stop'), label + ' logs stop');
      await js("document.querySelector('.forge-surface-tab-group[data-surface-type=app-preview] .forge-tab-close').click(); document.querySelector('.forge-surface-tab-group[data-surface-type=session] .forge-surface-tab').click()");
      await waitFor("!!document.querySelector('.forge-pane .forge-transcript-layout')");

      await waitFor("document.querySelector('.forge-work-label')?.textContent.includes('Worked for') && !!document.querySelector('.forge-turn-review:not([hidden])')");
      assert.ok(await js("!document.querySelector('.forge-work').open"), label + ' completed work folds closed');
      assert.ok(await js("!document.querySelector('.forge-transcript .msg, .forge-transcript .msg-body')"), label + ' transcript has no chat bubbles');
      assert.ok(await js("document.querySelector('.forge-composer-context').textContent.includes('~/Documents')"), label + ' abbreviated worktree and branch');
      await capture(label + '-forge-transcript');
      await js("document.querySelector('.forge-work').open = true");
      await waitFor("document.querySelector('.forge-command-output')?.textContent.includes('Syntax check passed') && !!document.querySelector('.forge-inline-diff')");
      assert.ok(await js("document.querySelectorAll('.forge-inline-diff .forge-diff-line').length <= 6"), label + ' inline diff has at most six numbered lines');
      if (width > 768) await capture(label + '-forge-transcript-expanded');
      await js("document.querySelector('.forge-inline-diff').click()");
      await waitFor("!!document.querySelector('.forge-pane .forge-diff-viewer')");
      await js("document.querySelector('.forge-surface-tab-group[data-surface-type=diff] .forge-tab-close').click()");
      await waitFor("!!document.querySelector('.forge-pane .forge-transcript-layout')");
      const undoStart = writes.length;
      await js("document.querySelector('.forge-turn-review button[data-undo]').click()");
      await waitFor("!!document.querySelector('.confirm-panel')");
      assert.ok(!writes.slice(undoStart).some(w => w.path.endsWith('/undo')), label + ' Undo waits for confirmation');
      await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Cancel').click()");
      assert.ok(!writes.slice(undoStart).some(w => w.path.endsWith('/undo')), label + ' cancelled Undo keeps files');
      const checkpointId = await js("document.querySelector('.forge-turn-review button[data-undo]').dataset.undo");
      await js("document.querySelector('.forge-turn-review button[data-undo]').click()");
      await waitFor("!!document.querySelector('.confirm-panel')");
      await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Undo turn').click()");
      await waitFor("!document.querySelector('.confirm-panel') && !document.querySelector('.forge-turn-review:not([hidden])')");
      assert.deepEqual(JSON.parse(writes.slice(undoStart).find(w => w.path === forgeBase + '/checkpoints/' + checkpointId + '/undo').body), { confirmed: true }, label + ' Undo calls this turn checkpoint');
      // Restore changes for this viewport pass's Unit C review checks.
      const resendStart = writes.length;
      await js("{const input=document.querySelector('.forge-transcript-input'); input.value='Restore composer changes'; input.dispatchEvent(new Event('input')); document.querySelector('.forge-send').click();}");
      await waitFor("document.querySelector('.forge-send')?.textContent === 'Stop'");
      await waitFor("document.querySelector('.forge-send')?.textContent === 'Send' && !!document.querySelector('.forge-turn-review:not([hidden])')");
      assert.ok(writes.slice(resendStart).some(w => w.path === '/api/chat/stream' && JSON.parse(w.body).session_id === forgeSessionId), label + ' resend shares chatStream');
      for (const mode of ['plan', 'build']) {
        const modeStart = writes.length;
        await js(`(() => { const picker = document.querySelector('[aria-label="Session mode"]').parentElement; picker.value = ${JSON.stringify(mode)}; picker.dispatchEvent(new Event('change')); })()`);
        await waitFor(`document.querySelector('.forge-plan-note').textContent.includes(${JSON.stringify(mode === 'plan' ? 'agent read only' : 'make changes')})`);
        assert.equal(JSON.parse(writes.slice(modeStart).find(w => w.path === forgeBase + '/mode').body).mode, mode, label + ' mode route');
      }
      await js("document.querySelector('[data-forge-project=fp1]').click()");
      await waitFor("!document.querySelector('.forge-project-sidebar').hidden && document.querySelectorAll('.forge-session-card').length >= 2");
      assert.ok(await js("document.querySelector('.forge-project-heading strong').textContent === 'Kairos garden'"), label + ' project opens its sidebar');
      if (width <= 768) {
        assert.ok(await js("(() => { const r=document.querySelector('.forge-project-sidebar').getBoundingClientRect(); return r.left === 0 && r.top === 0 && r.width === innerWidth && r.height === innerHeight; })()"), label + ' sheet fills the phone');
        assert.ok(await js("document.querySelector('.forge-workspace').inert"), label + ' sheet traps interaction');
        await capture(label + '-forge-shell-sheet');
      }
      await js(`document.querySelector('.forge-session-card[data-session-id="${forgeSessionId}"]').click()`);
      await waitFor("!!document.querySelector('.forge-pane .forge-transcript-layout')");
      assert.ok(await js("!!document.querySelector('.forge-surface-tab-group[data-surface-type=session]')"), label + ' card opens a session tab');
      // End-session retains Phase 2's separate dirty-worktree confirmation.
      if (width <= 768) await waitFor("document.querySelector('.forge-project-sidebar').hidden");
      const endStart = writes.length;
      await js("[...document.querySelectorAll('.forge-session-header button')].find(b => b.textContent === 'End session').click()");
      await waitFor("!!document.querySelector('.modal-panel .forge-warning')");
      await js("[...document.querySelectorAll('.modal-panel button')].find(b => b.textContent === 'Discard').click()");
      await waitFor("!!document.querySelector('.confirm-panel')");
      assert.ok(!writes.slice(endStart).some(w => w.method === 'DELETE'), label + ' dirty removal asks');
      await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Cancel').click(); document.querySelector('.modal-panel [aria-label=Close]').click()");
      await js("document.querySelector('[aria-label=\"Open project sidebar\"]').click(); document.querySelector('[data-sidebar-tab=explorer]').click()");
      await waitFor("!!document.querySelector('.forge-tree-directory')");
      await js("document.querySelector('.forge-tree-directory').open = true");
      await waitFor("!!document.querySelector('[data-file-path=\"src/garden.js\"]')");
      await js("document.querySelector('[data-file-path=\"src/garden.js\"]').click()");
      await waitFor("!!document.querySelector('.forge-pane .cm-editor')");
      assert.ok(await js("document.querySelector('.forge-surface-tab-group[data-surface-type=file]').classList.contains('preview')"), label + ' Explorer opens an italic preview');
      // CodeMirror's first gutter element is a hidden width spacer holding the widest number.
      assert.ok(await js("[...document.querySelectorAll('.cm-lineNumbers .cm-gutterElement')].find(e => e.style.visibility !== 'hidden')?.textContent === '1' && !!document.querySelector('.cm-content[contenteditable=true]')"), label + ' numbered editable file');
      await js("document.querySelector('[data-file-path=\"src/garden.js\"]').dispatchEvent(new MouseEvent('dblclick', {bubbles:true}))");
      assert.ok(await js("!document.querySelector('.forge-surface-tab-group[data-surface-type=file]').classList.contains('preview')"), label + ' double-click pins the preview');
      if (width > 768) await capture(label + '-forge-shell-explorer');
      if (width <= 768) await js("document.querySelector('[aria-label=\"Open project sidebar\"]').click()");
      await js("document.querySelector('[data-sidebar-tab=changes]').click()");
      await waitFor("!!document.querySelector('.forge-change-open')");
      await js("document.querySelector('.forge-change-open').click()");
      await waitFor("!!document.querySelector('.forge-pane .forge-diff-line.diff-add') && !!document.querySelector('.forge-pane .forge-diff-line.diff-remove')");
      assert.ok(await js("document.querySelector('.diff-add .forge-diff-number:nth-child(2)').textContent === '1'"), label + ' unified diff line numbers');
      if (width > 768) await capture(label + '-forge-shell-changes');
      if (width <= 768) await js("document.querySelector('[aria-label=\"Open project sidebar\"]').click()");
      const revertStart = writes.length;
      await js("document.querySelector('.forge-revert').click()");
      await waitFor("!!document.querySelector('.confirm-panel')");
      assert.ok(!writes.slice(revertStart).some(w => w.path.endsWith('/revert-file')), label + ' Revert waits for confirmation');
      await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Cancel').click()");
      await waitFor("!document.querySelector('.confirm-panel') && !document.querySelector('.forge-revert').disabled");
      assert.ok(!writes.slice(revertStart).some(w => w.path.endsWith('/revert-file')), label + ' Cancel keeps changes');
      await js("document.querySelector('.forge-revert').click()");
      await waitFor("!!document.querySelector('.confirm-panel')");
      await js("[...document.querySelectorAll('.confirm-panel button')].find(b => b.textContent === 'Revert file').click()");
      await waitFor("!document.querySelector('.forge-change-open')");
      assert.deepEqual(JSON.parse(writes.slice(revertStart).find(w => w.path === forgeBase + '/revert-file').body), { path: 'src/garden.js', confirmed: true }, label + ' confirmed file route');
      await js("document.querySelector('[data-sidebar-tab=sessions]').click(); document.querySelector('[data-sidebar-tab=explorer]').click()");
      assert.equal(await js("localStorage.getItem('kairos:forge-panel:fp1')"), '"explorer"', label + ' panel preference remembered');
      if (width > 768) {
        // Native pointer input exercises pointer capture, width clamping and persistence.
        const dragHandle = async (selector, dx) => {
          const point = await js(`(() => { const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return {x:r.left+r.width/2, y:r.top+100}; })()`);
          await win.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {type:'mousePressed', x:point.x, y:point.y, button:'left', clickCount:1});
          await win.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {type:'mouseMoved', x:point.x+dx, y:point.y, button:'left', buttons:1});
          await delay(50);
          await win.webContents.debugger.sendCommand('Input.dispatchMouseEvent', {type:'mouseReleased', x:point.x+dx, y:point.y, button:'left', clickCount:1});
        };
        await dragHandle('.forge-project-resizer', 100);
        assert.equal(await js("Math.round(document.querySelector('.forge-project-sidebar').getBoundingClientRect().width)"), 328, 'sidebar resizes');
        assert.equal(await js("JSON.parse(localStorage.getItem('kairos:forge-sidebar-width'))"), 328, 'sidebar width saved');
        await js("document.querySelector('.forge-project-resizer').dispatchEvent(new MouseEvent('dblclick', {bubbles:true}))");
        assert.equal(await js("Math.round(document.querySelector('.forge-project-sidebar').getBoundingClientRect().width)"), 228, 'sidebar double-click resets');
        await js("document.querySelector('.forge-surface-tab-group[data-surface-type=file] .forge-surface-tab').click(); document.dispatchEvent(new KeyboardEvent('keydown', {key:'d', ctrlKey:true, bubbles:true, cancelable:true}))");
        await waitFor("document.querySelector('.forge-shell').dataset.split === 'true'");
        assert.ok(await js("!!document.querySelector('[aria-label=\"Left pane\"] .forge-transcript-layout') && !!document.querySelector('[aria-label=\"Right pane\"] .cm-editor')"), 'split shows file beside session');
        const beforeSplit = await js("document.querySelector('[aria-label=\"Left pane\"]').clientWidth");
        await dragHandle('.forge-pane-divider', 40);
        assert.ok(await js("document.querySelector('[aria-label=\"Left pane\"]').clientWidth") > beforeSplit, 'split divider resizes');
        await capture(label + '-forge-shell-split');
        await js("{const t=document.querySelector('.forge-surface-tab-group[data-surface-type=diff]'); const dt=new DataTransfer(); t.dispatchEvent(new DragEvent('dragstart', {bubbles:true, dataTransfer:dt})); document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true})); document.querySelector('.forge-workspace').dispatchEvent(new DragEvent('drop', {bubbles:true, clientX:innerWidth-2, dataTransfer:dt}));}");
        assert.ok(await js("!!document.querySelector('[aria-label=\"Right pane\"] .cm-editor') && !document.querySelector('[aria-label=\"Right pane\"] .forge-diff-viewer')"), 'Escape cancels tab drag');
        await js("{const t=document.querySelector('.forge-surface-tab-group[data-surface-type=diff]'); const dt=new DataTransfer(), w=document.querySelector('.forge-workspace'); t.dispatchEvent(new DragEvent('dragstart', {bubbles:true, dataTransfer:dt})); w.dispatchEvent(new DragEvent('dragover', {bubbles:true, cancelable:true, clientX:innerWidth-2, dataTransfer:dt})); w.dispatchEvent(new DragEvent('drop', {bubbles:true, clientX:innerWidth-2, dataTransfer:dt}));}");
        assert.ok(await js("!!document.querySelector('[aria-label=\"Right pane\"] .forge-diff-viewer')"), 'right-edge tab drop');
      } else {
        await js("document.querySelector('[aria-label=\"Close project sidebar\"]').click(); document.dispatchEvent(new KeyboardEvent('keydown', {key:'d', ctrlKey:true, bubbles:true, cancelable:true}))");
        assert.equal(await js("document.querySelector('.forge-shell').dataset.split"), 'false', 'phone never splits');
      }
      await js("document.querySelector('.forge-surface-tab-group[data-surface-type=diff]').dispatchEvent(new MouseEvent('auxclick', {button:1, bubbles:true, cancelable:true}))");
      assert.ok(await js("!document.querySelector('.forge-surface-tab-group[data-surface-type=diff]')"), label + ' middle-click closes');
      assert.deepEqual(await overflow(), [], label + ' shell overflow');
      await require('./forge3-checks.cjs')({ js, waitFor, capture, writes, demoState, label, forgeSessionId, width });
      // A project with no session uses the registered folder's read-only Explorer.
      await js("document.querySelector('[data-forge-project=fp2]').click()");
      await waitFor("document.querySelector('.forge-project-heading strong').textContent === 'Field notes'");
      await js("document.querySelector('[data-sidebar-tab=explorer]').click()");
      await waitFor("!!document.querySelector('[data-file-path=\"README.md\"]')");
      await js("document.querySelector('[data-file-path=\"README.md\"]').click()");
      await waitFor("document.querySelector('.forge-pane .forge-file-viewer')?.textContent.includes('Read only')");
      assert.ok(reads.some(r => r.path === '/api/forge/projects/fp2/files'), label + ' project-folder fallback');
      // Chats opens the shell and selects the correct project.
      await navigate('chat');
      await waitFor(`!!document.querySelector('.session-item[data-session-id="${forgeSessionId}"] .forge-chat-tag')`);
      await js(`document.querySelector('.session-item[data-session-id="${forgeSessionId}"]').click()`);
      await waitFor("document.getElementById('view-content').dataset.view === 'forgeShell' && !!document.querySelector('.forge-transcript-layout')");
      assert.equal(await js("document.querySelector('.forge-project-heading strong').textContent"), 'Kairos garden', label + ' Chats selects the project');
      await js("document.querySelector('[aria-label=\"New session\"]').click()");
      await waitFor("!!document.querySelector('[aria-label=\"Project for new session\"]') && document.activeElement.id === 'forge-message'");
      assert.equal(await js("document.querySelector('[aria-label=\"Project for new session\"]').parentElement.value"), 'fp1', label + ' sidebar plus preselects the composer');
      // Keep this pass's response live so Working now and busy shimmer can be exercised.
      const workingStart = writes.length;
      demoState.forgeHeldSession = forgeSessionId; demoState.forgeRelease = false;
      await js(`import('/static/js/chatStream.js').then(stream => stream.startTurn(${JSON.stringify(forgeSessionId)}, 'Shell entry check', 'Working now shell check ${label}', []))`);
      await waitFor(`!!document.querySelector('.forge-home .forge-session-row[data-session-id="${forgeSessionId}"]') && !!document.querySelector('[data-forge-project=fp1].busy')`);
      await js(`document.querySelector('.forge-home .forge-session-row[data-session-id="${forgeSessionId}"]').click()`);
      await waitFor("document.getElementById('view-content').dataset.view === 'forgeShell' && !!document.querySelector('.forge-transcript-layout') && !document.querySelector('.forge-project-sidebar').hidden");
      assert.ok(writes.slice(workingStart).some(w => w.path === '/api/chat/stream' && JSON.parse(w.body).session_id === forgeSessionId), label + ' Working now uses this pass session');
      demoState.forgeRelease = true;
      await waitFor("document.querySelector('.side-chat-send')?.textContent === 'Send'");
      delete demoState.forgeHeldSession; delete demoState.forgeRelease;
      await navigate('home');
      if (label === 'desktop') {
        await js("document.querySelector('#sidebar-toggle').click()"); await delay(350);
        await js("document.querySelector('.forge-mode-rail').click()");
        await waitFor("!!document.querySelector('.forge-home') && document.querySelectorAll('#nav [data-tab]').length === 1");
        assert.equal(await js("document.querySelector('.forge-mode-rail').getAttribute('aria-label')"), 'Switch to Kairos');
        assert.ok(await js("!!document.querySelector('.forge-mode-rail svg .brand-point')"), 'Collapsed mode toggle is the Kairos circle mark');
        await capture('desktop-forge-switch-collapsed');
        await js("document.querySelector('.forge-mode-rail').click()"); await waitFor("!!document.querySelector('.dashboard-hero')");
        await js("document.querySelector('#sidebar-toggle').click()"); await delay(350);
      } else {
        // A phone retains the full switch even with the desktop rail preference.
        await js("document.documentElement.classList.add('sidebar-collapsed'); document.querySelector('#mobile-menu-btn').click()"); await delay(300);
        assert.equal(await js("getComputedStyle(document.querySelector('#forge-mode-switch [data-mode=forge]')).display"), 'block');
        assert.equal(await js("getComputedStyle(document.querySelector('.forge-mode-rail')).display"), 'none');
        await capture('mobile-forge-switch-collapsed');
        await js("document.querySelector('#forge-mode-switch [data-mode=forge]').click()"); await waitFor("!!document.querySelector('.forge-home') && !document.querySelector('#sidebar').classList.contains('open')");
        await js("document.querySelector('#mobile-menu-btn').click(); document.querySelector('#forge-mode-switch [data-mode=kairos]').click()");
        await waitFor("!!document.querySelector('.dashboard-hero')");
        await js("document.documentElement.classList.remove('sidebar-collapsed')");
      }
      demoState.isAdmin = false;
      await js("localStorage.setItem('kairos:app-mode', 'forge')"); await win.loadURL(base);
      await waitFor("document.querySelectorAll('.dashboard-stat').length === 4");
      assert.ok(await js("!document.querySelector('#forge-mode-switch, #nav [data-tab=forgeHome], #nav [data-tab=forgeProjects], #nav [data-tab=forgeSessions]')"), label + ' non-admin sees no Forge switch');
      await navigate('forgeHome');
      assert.equal(await js("document.getElementById('view-content').dataset.view"), 'home', label + ' non-admin cannot route to Forge');
      demoState.isAdmin = true; delete demoState.sessionFields;
      await win.loadURL(base); await waitFor("!!document.querySelector('#forge-mode-switch') && document.querySelectorAll('.dashboard-stat').length === 4");
    }
    writes.splice(forgeWrites);
    win.setContentSize(1440, 900); await delay(350);
    const railWidth = () => js("document.querySelector('#sidebar').getBoundingClientRect().width");
    assert.equal(await railWidth(), 204);
    await js("document.querySelector('#sidebar-toggle').click()");
    await delay(80);
    const movingWidth = await railWidth();
    assert.ok(movingWidth > 52 && movingWidth < 204, "Sidebar animates between widths");
    await delay(300);
    assert.equal(await railWidth(), 52);
    assert.ok(await js("[...document.querySelectorAll('#sidebar .nav-item svg')].every(e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=52})"), 'Icons fit the slim rail');
    assert.ok(await js("[...document.querySelectorAll('#sidebar .nav-item > span, #sidebar .sidebar-user-name')].every(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return (r.width===0||s.opacity==='0'||s.display==='none')&&r.right<=52})"), 'Labels are hidden in the slim rail');
    assert.equal(await js("document.querySelector('#sidebar-toggle').getAttribute('aria-expanded')"), "false");
    assert.equal(await js("localStorage.getItem('jarvis:sidebar-collapsed')"), "true");
    assert.ok(await js("[...document.querySelectorAll('#nav button')].every(b => b.getAttribute('aria-label') && b.querySelector('svg'))"), "Every icon-only tab has an accessible name and an icon");
    await capture("desktop-sidebar-collapsed");
    await win.loadURL(base);
    await waitFor("document.querySelectorAll('.dashboard-stat').length === 4");
    await delay(350);
    assert.equal(await railWidth(), 52, "Collapsed state survives reload");
    for (const tab of ["chat", "notes", "library", "calendar", "tasks", "email", "tool-store", "cookbook", "school"]) {
      await navigate(tab);
      assert.deepEqual(await overflow(), [], "collapsed " + tab);
      if (tab === 'chat') await capture('desktop-chat-minimal');
    }
    await js("document.querySelector('[data-tab=chat]').focus()");
    assert.equal(await js("document.querySelector('.sidebar-tooltip').textContent"), "Chats");
    assert.ok(await js("document.querySelector('.sidebar-tooltip').classList.contains('show')"));
    win.setContentSize(390, 844);
    await delay(100);
    await js("document.querySelector('#mobile-menu-btn').click()");
    await delay(300);
    assert.ok(await railWidth() > 200, "Mobile retains full-width drawer despite desktop preference");
    assert.equal(await js("getComputedStyle(document.querySelector('#nav .nav-item > span')).opacity"), "1");
    await capture("mobile-sidebar");
    await js("document.querySelector('[data-tab=home]').click()");
    win.setContentSize(1440, 900);
    await delay(100);
    await js("document.querySelector('#sidebar-toggle').focus()");
    assert.equal(await js("document.activeElement.id"), "sidebar-toggle");
    await win.webContents.debugger.sendCommand("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13, text: "\r" });
    await win.webContents.debugger.sendCommand("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
    // Wait for the 260 ms width transition to settle rather than a fixed delay.
    await waitFor("Math.round(document.querySelector('#sidebar').getBoundingClientRect().width) === 204");
    assert.equal(Math.round(await railWidth()), 204, "Keyboard expands sidebar");
    await js("document.activeElement.blur()");
    // Page widths on a wide desktop.
    const previousSize = win.getContentSize();
    try {
      win.setContentSize(1920, 1080);
      await delay(100);
      for (const section of ["documents", "vault", "google"]) {
        await navigate("library", { section });
        await waitFor("!!document.querySelector('#view-content .library-view, #view-content .library-vault-page')");
        assert.equal(await js("document.querySelector('#view-content .library-view, #view-content .library-vault-page').getBoundingClientRect().width"), 1440, "Library " + section + " is 1440px wide");
      }
      await navigate("library", { section: "documents" });
      await waitFor("!!document.querySelector('.library-document-card')");
      await js("document.querySelector('.library-document-card').click()");
      await waitFor("!!document.querySelector('#view-content .library-editor')");
      assert.equal(await js("document.querySelector('#view-content .library-editor').getBoundingClientRect().width"), 1440, "Library document editor is 1440px wide");
      await navigate("library", { section: "documents" });
      for (const tab of ["agents", "tasks", "notes", "email", "cookbook", "tool-store"]) {
        await navigate(tab);
        await waitFor("!!document.querySelector('#view-content .view-constrained')");
        assert.equal(await js("document.querySelector('#view-content .view-constrained').getBoundingClientRect().width"), 1200, tab + " is 1200px wide");
      }
    } finally {
      win.setContentSize(...previousSize);
      await delay(100);
    }
    for (const [label, width, height] of [["desktop", 1440, 900], ["mobile", 390, 844]]) {
      demoState.stoppedOwners = [];
      demoState.takenOwners = [];
      win.setContentSize(width, height);
      await delay(100);
      for (const tab of ["home", "chat", "notes", "library", "calendar", "tasks", "email", "tool-store", "agents", "cookbook", "school"]) {
        await navigate(tab);
        if (tab === "chat") {
          // A new chat lands on the halftone figure (static/js/chatBackdrop.js).
          await waitFor("document.querySelector('.chat-backdrop')?.dataset.scene === 'figure' && document.querySelector('.chat-backdrop').dataset.drawMs !== undefined");
          assert.ok(await js("document.querySelector('.chat-backdrop').clientWidth > 0"), label + " default Chat draws the halftone background");
          await waitFor("!!document.querySelector('.session-item[data-session-id=s1]')");
          await js("document.querySelector('.session-item[data-session-id=s1]').click()");
          await waitFor("!!document.querySelector('#overflow-menu .chat-computer-toggle:not([hidden])')");
          const chatComputerStart = writes.length;
          await waitFor("document.querySelector('.chat-computer-history .computer-thumbnail')?.naturalWidth > 0");
          await capture(label + '-chat-computer-history');
          await js("document.querySelector('.chat-computer-history .computer-shot summary').click()");
          assert.ok(await js("document.querySelector('.chat-computer-history .computer-shot').open"), label + ' screenshot expands');
          await capture(label + '-chat-computer-history-expanded');
          await js("document.querySelector('.chat-computer-history .computer-shot summary').click()");
          demoState.computerTurn = true; demoState.moreComputerActions = false;
          await js("import('/static/js/chatStream.js').then(stream => { stream.startTurn('s1', 'Computer demo', 'Open example.com', []); })");
          if (width <= 768) {
            await waitFor("document.querySelector('.computer-watch:not([hidden])')?.textContent === 'Computer is open - watch'");
            assert.ok(await js("!document.querySelector('.chat-computer-pane')"), 'Phone does not force the drawer open');
            await capture(label + '-chat-computer-watch');
            await js("document.querySelector('.computer-watch').click()");
          } else {
            await waitFor("!!document.querySelector('.chat-computer-pane')");
            await capture(label + '-chat-computer-auto');
            await js("document.querySelector('[aria-label=\"Close computer pane\"]').click()");
            demoState.moreComputerActions = true;
            await delay(300);
            assert.ok(await js("!document.querySelector('.chat-computer-pane')"), 'A dismissed pane does not reopen in the same turn');
          }
          await waitFor("!!document.querySelector('#overflow-menu .chat-computer-toggle:not([hidden])')");
          await js("document.body.click(); document.querySelector('#overflow-plus-btn').click()");
          await js("document.querySelector('#overflow-menu .chat-computer-toggle').click()");
          assert.ok(await js("document.querySelector('#overflow-menu').classList.contains('hidden')"), label + ' Computer closes the + menu');
          await waitFor("document.querySelector('.computer-frame')?.complete && document.querySelector('.computer-frame')?.naturalWidth > 0");
          await capture(label + '-chat-computer');
          await js("document.querySelector('.computer-actions button:nth-child(1)').click()");
          await waitFor("document.querySelector('.computer-state')?.textContent.includes('You have control')");
          await capture(label + '-chat-computer-takeover');
          await js("document.querySelector('.computer-record').click()");
          await waitFor("!!document.querySelector('.computer-panel.is-recording .computer-recording-indicator:not([hidden])')");
          assert.equal(await js("document.querySelector('.computer-recording-indicator').textContent"), 'Recording');
          await capture(label + '-computer-recording');
          await js("{ const f=document.querySelector('.computer-frame'), r=f.getBoundingClientRect(); f.dispatchEvent(new MouseEvent('click', { bubbles:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2 })); }");
          await waitFor("document.querySelector('.computer-state')?.textContent.includes('model is waiting')");
          // The click's request is asynchronous; wait for it. Chromium truncates
          // mouse positions to whole CSS pixels, and one CSS pixel of this
          // ~516 px wide panel spans ~2.5 page pixels, so the centre is 640,400 ± 3.
          const centreClick = w => w.path === '/api/computer/chat%3As1/input' && JSON.parse(w.body).kind === 'click';
          for (let i = 0; i < 40 && !writes.some(centreClick); i++) await delay(50);
          const sent = JSON.parse((writes.find(centreClick) || { body: '{}' }).body);
          assert.ok(Math.abs(sent.x - 640) <= 3 && Math.abs(sent.y - 400) <= 3,
            `${label} computer click maps to page coordinates (sent ${sent.x},${sent.y})`);
          await js("for (const key of 'kairos') document.querySelector('.computer-stage').dispatchEvent(new KeyboardEvent('keydown', {key, bubbles:true, cancelable:true}))");
          for (let i = 0; i < 40 && !writes.some(w => w.path.endsWith('/input') && JSON.parse(w.body).text === 's'); i++) await delay(50);
          await js("document.querySelector('.computer-record').click()");
          await waitFor("document.querySelector('.recording-review [aria-label=\"SKILL.md preview\"]')?.value.includes(\"kairos\")");
          await js("document.querySelector('.recording-review .recording-check input').click()");
          await waitFor("document.querySelector('.recording-review textarea')?.value.includes('(ask each time)')");
          await capture(label + '-recording-review');
          await js("{ const preview=document.querySelector('.recording-review textarea'); preview.value += '\\nOpen an ngrok link in the computer.\\n'; preview.dispatchEvent(new Event('input')); }");
          await js("[...document.querySelectorAll('.recording-review button')].find(b => b.textContent === 'Save').click()");
          await waitFor("[...document.querySelectorAll('.recording-review button')].some(b => b.textContent === 'Save anyway')");
          await capture(label + '-recording-caution');
          await js("[...document.querySelectorAll('.recording-review button')].find(b => b.textContent === 'Save anyway').click()");
          await waitFor("!document.querySelector('.recording-review')");
          const recordingSave = writes.find(w => w.path === '/api/skills/from-recording');
          assert.ok(recordingSave && JSON.parse(recordingSave.body).body.includes('(ask each time)'), label + ' recorded skill saves through import');
          assert.ok(writes.some(w => w.path === '/api/skills/from-recording' && JSON.parse(w.body).confirmed === true), label + ' caution requires explicit confirmation');
          await js("document.querySelector('.computer-actions button:nth-child(2)').click()");
          await waitFor("document.querySelector('.computer-state')?.textContent === 'Computer closed'");
          assert.ok(await js("document.querySelector('.computer-frame')?.naturalWidth > 0"), label + ' last frame remains after close');
          await capture(label + '-chat-computer-closed');
          await js("document.querySelector('[aria-label=\"Close computer pane\"]').click()");
          await waitFor("!document.querySelector('.chat-computer-pane')");
          assert.ok(writes.some(w => w.path === '/api/computer/chat%3As1/stop'), label + ' Stop calls the computer route');
          // Checked: drop these requests and reset the fixture, so the later
          // "nothing was written" checks and the next pass start clean.
          writes.splice(chatComputerStart);
          demoState.stoppedOwners = []; demoState.takenOwners = [];
          demoState.recordingOwners = []; demoState.recordedSteps = {}; demoState.recordedSkills = [];
          demoState.computerTurn = false; demoState.moreComputerActions = false;
        }
        if (tab === "home") {
          await waitFor("document.querySelectorAll('.dashboard-stat').length === 4");
          assert.ok(await js("document.querySelector('.dashboard-content').classList.contains('is-filled') && getComputedStyle(document.querySelector('.dashboard-section-body')).animationName === 'home-fill'"), label + " Home's first fill animates in");
          await waitFor("document.querySelectorAll('.dashboard-model-usage').length > 0");
          // Only the local model carries a usage label, and it is tokens
          // spent, never a "% used" figure that reads like a quota.
          const labels = await js("[...document.querySelectorAll('.dashboard-model-usage')].map(n => n.textContent)");
          assert.deepEqual(labels, ["842K tokens via Kairos"], label + " home model usage labels");
          // Provider logos where known (core/model_marks.py); the generic icon where not.
          const marks = await js("[...document.querySelectorAll('.dashboard-row > .model-mark')].map(n => n.getAttribute('aria-label'))");
          assert.deepEqual(marks, ["Claude", "Codex"], label + " home model logos");
        }
        if (tab === "school" && !demoState.empty && label === "desktop") {
          // An assignment's course chat is the shared embedded chat
          // (sessionChat.js): model choice, composer, Open in Chats.
          // Opening an assignment syncs your work into the course chat on purpose.
          const schoolWrites = writes.length;
          await waitFor("[...document.querySelectorAll('.card-row .title')].some(t=>t.textContent==='Review the project brief')");
          await js("[...document.querySelectorAll('.card-row .title')].find(t=>t.textContent==='Review the project brief').closest('.card-row').click()");
          await waitFor("!!document.querySelector('.school-course-chat .session-chat .side-chat-input')");
          await waitFor("document.querySelectorAll('.school-course-chat .msg').length > 0");
          assert.ok(await js("!!document.querySelector('.school-course-chat .session-chat-models .custom-select') && [...document.querySelectorAll('.school-course-chat button')].some(b=>b.textContent==='Open in Chats')"), label + " School course chat has model choice and Open in Chats");
          assert.deepEqual(await overflow(), [], label + " School assignment overflow");
          await capture(label + "-school-assignment");
          assert.ok(writes.slice(schoolWrites).every(w => w.path.startsWith('/api/tab-school/assignments/a1/')), label + ' School writes only its own sync');
          writes.splice(schoolWrites);
        }
        if (tab === "agents" && !demoState.empty) {
          // Agents: the list, the cross-agent inbox with its answers, the badge.
          await waitFor("document.querySelectorAll('.agent-tile:not(.team-tile)').length === 2");
          assert.ok(await js("document.querySelector('.agent-teams .team-tile')?.textContent.includes('Launch team')"), label + " teams listed under agents");
          assert.ok(await js("document.querySelector('.team-tile .agent-status').textContent === 'Working'"), label + " team state shown");
          assert.ok(await js("[...document.querySelectorAll('.agent-inbox-question button')].some(b => b.textContent === 'Answer')"), label + " a question can be answered");
          assert.equal(await js("document.querySelectorAll('.agent-inbox-approval').length"), 0, label + " agents run in Auto: nothing to approve");
          assert.ok(await js("document.querySelector('.agent-inbox-report').textContent.includes('Backend Engineer')"), label + " report body shown");
          assert.equal(await js("document.querySelector('.agent-status-working').textContent"), "Working");
          await waitFor("document.querySelector('#nav .nav-item[data-tab=agents] .nav-badge')?.textContent === '2'");
          assert.deepEqual(await overflow(), [], label + " overflow in agents list");
          await navigate("agents", { agentId: "a1" });
          await waitFor("!!document.querySelector('.agent-memory')");
          // The agent page opens on its Chat tab: its own chats, the open
          // conversation and a composer; Work holds everything else.
          await waitFor("document.querySelectorAll('.agent-chat-item').length === 1");
          assert.equal(await js("document.querySelector('.agent-tab.active').textContent"), "Chat", label + " opens on Chat");
          assert.equal(await js("[...document.querySelectorAll('.agent-tab')][1].textContent"), "Work (2)", label + " work tab counts what waits");
          await waitFor("document.querySelectorAll('.agent-chat-main .session-chat-messages .msg').length > 0");
          assert.ok(await js("document.querySelector('.agent-chat-item.active')?.textContent.includes('Chat with Scout')"), label + " last chat open");
          assert.ok(await js("!!document.querySelector('.agent-chat-main .side-chat-input')"), label + " composer");
          assert.ok(await js("document.querySelector('.agent-work-host').hidden"), label + " work hidden on chat tab");
          assert.deepEqual(await overflow(), [], label + " overflow on agent chat");
          await capture(label + "-agent-chat");
          await js("[...document.querySelectorAll('.agent-tab')][1].click()");
          await waitFor("!document.querySelector('.agent-work-host').hidden");
          await waitFor("document.querySelector('.agent-computer-section .computer-frame')?.naturalWidth > 0");
          const titles = await js("[...document.querySelectorAll('.agent-work-host > .glass > .title')].map(n => n.textContent)");
          assert.deepEqual(titles, ["Inbox", "Computer", "Standing goals", "Work", "Memory", "History", "Teams", "Triggers"], label + " agent work sections");
          assert.equal(await js("document.querySelector('.agent-work-host [aria-label=\"Keep this agent signed in\"]').getAttribute('aria-checked')"), 'true');
          assert.ok(await js("document.querySelector('.agent-forget-logins').disabled"), label + ' logins cannot be forgotten while running');
          await capture(label + '-agent-computer');
          assert.ok(await js("document.querySelector('.agent-triggers-panel').textContent.includes('GitHub pushes · asks you first')"), label + " the triggers that start this agent's work");
          assert.ok(await js("document.querySelector('.agent-teams-panel').textContent.includes('Launch team · teammate · Working')"), label + " the agent's teams");
          assert.ok(await js("document.querySelector('.agent-memory').value.includes('remote roles only')"), label + " memory shown");
          assert.ok(await js("document.querySelector('.agents-view').textContent.includes('Every day at 08:00')"), label + " goal cadence");
          assert.deepEqual(await overflow(), [], label + " overflow on agent page");
          await capture(label + "-agent-page");
          await navigate("agents");
          await waitFor("document.querySelectorAll('.agent-tile:not(.team-tile)').length === 2");
          const agentComputerStart = writes.length;
          await js("[...document.querySelectorAll('.agent-inbox-question button')].find(b => b.textContent === 'Open computer').click()");
          await waitFor("document.querySelector('.agent-work-host:not([hidden]) .computer-state')?.textContent.includes('You have control')");
          await capture(label + '-agent-computer-takeover');
          await js("document.querySelector('.agent-computer-section .computer-record').click()");
          await waitFor("!!document.querySelector('.agent-computer-section .is-recording')");
          await js("document.querySelector('.agent-computer-section .computer-record').click()");
          await waitFor("document.querySelector('.recording-review')?.textContent.includes(\"Mention in Scout's instructions\")");
          await capture(label + '-agent-recording-review');
          await js("[...document.querySelectorAll('.recording-review button')].find(b => b.textContent === 'Discard').click()");
          await js("document.querySelector('.agent-computer-section .computer-actions button:first-child').click()");
          for (let i = 0; i < 40 && !writes.slice(agentComputerStart).some(w => w.path.endsWith('/handback')); i++) await delay(50);
          assert.ok(writes.slice(agentComputerStart).some(w => w.path === '/api/computer/agent%3Aa1/handback'), label + ' Hand back calls the computer route');
          writes.splice(agentComputerStart);
          demoState.stoppedOwners = []; demoState.takenOwners = [];
          demoState.recordingOwners = []; demoState.recordedSteps = {};
          await navigate('agents');
        }
        if (tab === "tool-store" && !demoState.empty) {
          const headerButton = text => `[...document.querySelectorAll('.tool-store-manage button')].find(b => b.textContent === ${JSON.stringify(text)})`;
          for (const [text, ready] of [
            ['Install a single-file skill from GitHub', `!!document.querySelector('.modal-panel input[aria-label="GitHub SKILL.md URL"]')`],
            ['Add MCP server', `!!document.querySelector('.modal-panel input[aria-label="MCP server URL"]')`],
            ['Manage skills', `!!document.querySelector('.modal-panel #skills-list .card')`],
          ]) await popupDismissals(headerButton(text), ready, label + ' ' + text);
          const viewSkill = `[...document.querySelectorAll('.tool-store-card')].find(c=>c.querySelector('h4')?.textContent==='build-custom-tab').querySelector('button')`;
          await popupDismissals(viewSkill, "!!document.querySelector('.modal-panel .tool-store-skill-body')", label + ' View skill');
          await openPopup(viewSkill, "!!document.querySelector('.modal-panel .tool-store-skill-body')", label + ' View skill');
          assert.ok(await js("document.querySelector('.modal-panel .tool-store-skill-body').textContent.includes('Procedure')"), label + ' skill body is readable');
          await capture(label + '-tool-store-view-skill');
          // Switching from viewing to managing replaces the popup and focuses that skill.
          await js("document.querySelector('.modal-panel .btn').focus(); document.querySelector('.modal-panel .btn').click()");
          await waitFor("!!document.querySelector('.modal-panel .tool-store-manager #skills-list .card')");
          assert.equal(await js("document.querySelectorAll('.panel-dialog').length"), 1, label + ' Manage skill replaces View skill');
          assert.ok(await js("document.activeElement.closest('[data-skill]')?.dataset.skill === 'build-custom-tab'"), label + ' manager focuses the selected skill');
          await js("document.activeElement.click()");
          await waitFor("!!document.querySelector('.modal-panel #skills-list textarea')");
          assert.ok(await js("document.querySelector('.modal-panel #skills-list textarea').value.includes('Procedure')"), label + ' skill editor keeps its body');
          await closePopup('escape', label + ' Manage skill');
          const managerWrites = writes.length;
          await openPopup(headerButton('Manage skills'), "!!document.querySelector('.modal-panel #skills-list [data-skill=\"build-custom-tab\"]')", label + ' skill save');
          await js("document.querySelector('.modal-panel [data-skill=\"build-custom-tab\"] button[aria-label=\"Edit skill\"]').click()");
          await waitFor("!!document.querySelector('.modal-panel #skills-list textarea')");
          await js("document.querySelector('.modal-panel #skills-list textarea').value='Updated in the popup.'; [...document.querySelectorAll('.modal-panel #skills-list button')].find(b=>b.textContent==='Save').click()");
          await waitFor("!document.querySelector('.panel-dialog')");
          assert.ok(writes.slice(managerWrites).some(w=>w.path==='/api/skills/build-custom-tab' && w.method==='PUT' && JSON.parse(w.body).body.includes('Updated in the popup.')), label + ' saving a skill closes the popup and keeps the edited content');
          writes.splice(managerWrites);
          await openPopup(headerButton('Add MCP server'), `!!document.querySelector('.modal-panel input[aria-label="MCP server URL"]')`, label + ' server validation');
          await js("document.querySelector('.modal-panel input[aria-label=\"Server name\"]').value='Example'; document.querySelector('.modal-panel input[aria-label=\"MCP server URL\"]').value='invalid'; [...document.querySelectorAll('.modal-panel button')].find(b=>b.textContent==='Add server').click()");
          await waitFor("document.querySelector('.modal-panel [role=status]').textContent.includes('HTTP or HTTPS')");
          await closePopup('button', label + ' server validation');
          await waitFor("document.querySelectorAll('[data-community-slug]').length === 4");
          assert.ok(await js("document.querySelector('[data-community-heading]').textContent.includes('Community')"), label + " Community section renders");
          assert.equal(await js("document.querySelector('[data-community-slug=meeting_summary] .tool-store-badge').textContent"), 'Update available', label + ' Community update badge');
          const communityWrites = writes.length;
          await js("document.querySelector('[data-community-slug=weekly_review] .btn.primary').click()");
          await waitFor("document.querySelector('[data-community-slug=weekly_review] .tool-store-badge').textContent === 'Installed'");
          assert.ok(writes.slice(communityWrites).some(w => w.path === '/api/store/install' && JSON.parse(w.body).kind === 'automation'), label + ' Community install uses the store gate');
          await js("document.querySelector('[data-community-heading]').scrollIntoView({ block: 'start' })");
          await capture(label + '-tool-store-community');
          const publishWrites = writes.length;
          await js("[...document.querySelectorAll('.store-github-strip button')].find(b=>b.textContent==='Sign in with GitHub').click()");
          await waitFor("document.querySelector('.store-device-code')?.textContent === 'KAIROS42'");
          await capture(label + '-store-device-code');
          await waitFor("document.querySelector('.store-github-strip').textContent.includes('@alex-demo')");
          await waitFor("document.querySelector('.store-submissions').textContent.includes('Meeting notes')");
          await js("[...document.querySelectorAll('.tool-store-filters button')].find(b=>b.textContent==='Tabs').click(); document.querySelector('[data-tab-slug=project_tracker] .store-share').click()");
          await waitFor("!!document.querySelector('.store-publish-panel') && ![...document.querySelectorAll('.store-publish-panel button')].find(b=>b.textContent==='Preview').disabled");
          await js("[...document.querySelectorAll('.store-publish-panel button')].find(b=>b.textContent==='Preview').click()");
          await waitFor("document.querySelectorAll('.store-publish-preview pre').length === 4");
          assert.ok(await js("document.querySelector('.store-publish-preview').textContent.includes('manifest.json') && document.querySelector('.store-publish-preview').textContent.includes('from fastapi import APIRouter')"), label + ' full publish files and manifest');
          assert.ok(await js("[...document.querySelectorAll('.store-publish-panel button')].find(b=>b.textContent==='Open pull request').disabled"), label + ' public confirmation required');
          assert.deepEqual(await overflow(), [], label + ' publish preview mobile overflow');
          assert.ok(await js("[...document.querySelectorAll('.store-publish-panel, .store-publish-preview')].every(n=>n.scrollWidth<=n.clientWidth+2)"), label + ' dialog content fits the viewport');
          await capture(label + '-store-publish-preview');
          await js("document.querySelector('[data-public-confirm]').click(); [...document.querySelectorAll('.store-publish-panel button')].find(b=>b.textContent==='Open pull request').click()");
          await waitFor("document.querySelector('.store-pr-link')?.href === 'https://github.com/david-darr/kairos-store/pull/42'");
          await capture(label + '-store-published');
          await js("[...document.querySelectorAll('.store-publish-panel button')].find(b=>b.textContent==='Done').click(); [...document.querySelectorAll('.tool-store-filters button')].find(b=>b.textContent==='All').click()");
          await waitFor("document.querySelector('.store-submissions').textContent.includes('#42')");
          await js("[...document.querySelectorAll('.store-github-strip button')].find(b=>b.textContent==='Sign out of GitHub').click()");
          await waitFor("document.querySelector('.store-github-strip').textContent.includes('Sign in with GitHub')");
          demoState.githubSignedIn = false; demoState.storePublished = false;
          writes.splice(publishWrites);
          // Reset the synthetic install so both viewport passes exercise Install.
          demoState.communityInstalls = {};
          writes.splice(communityWrites);
          // Roadmap phase 6: server health, a tool held for review, and an
          // unreadable skill that says why.
          await waitFor("document.querySelectorAll('.tool-store-health').length === 3");
          assert.ok(await js("[...document.querySelectorAll('.tool-store-health')].some(n => n.textContent.includes('Working · 12 tools'))"), label + " a working server says so");
          assert.ok(await js("[...document.querySelectorAll('.tool-store-health')].some(n => n.textContent.includes('Not responding · FileNotFoundError'))"), label + " a down server says why");
          assert.ok(await js("[...document.querySelectorAll('.tool-store-health')].some(n => n.textContent.includes('Not signed in'))"), label + " a signed-out server says so");
          assert.ok(await js("document.querySelector('.tool-store-held').textContent.includes('delete_page')"), label + " a held tool is listed for review");
          assert.ok(await js("[...document.querySelectorAll('.tool-store-held button')].some(b => b.textContent === 'Accept')"), label + " and can be accepted");
          assert.ok(await js("document.querySelector('.tool-store-broken').textContent.includes('not UTF-8')"), label + " an unreadable skill says why");
          assert.ok(await js("[...document.querySelectorAll('.tool-store-card')].some(c => c.textContent.includes('Local files') && c.querySelector('.tool-store-badge').textContent === 'Not responding')"), label + " a down server is not called connected");
          await js("document.querySelector('.tool-store-held').scrollIntoView()");
          await capture(label + "-tool-store-health");
          // The Tabs steps below save on purpose; their writes are checked here
          // and then dropped, so later "nothing was saved" checks stay exact.
          const tabStoreWrites = writes.length;
          await js("[...document.querySelectorAll('.tool-store-filters button')].find(b=>b.textContent==='Tabs').click()");
          await waitFor("document.querySelectorAll('[data-tab-kind=prebuilt]').length === 2");
          assert.deepEqual(await js("[...document.querySelectorAll('[data-tab-kind=prebuilt] h4')].map(n=>n.textContent).sort()"), ["CRM", "School"], label + " prebuilt tabs in store");
          assert.ok(await js("document.querySelector('[data-tab-slug=project_tracker]').textContent.includes('Needs approval') && [...document.querySelectorAll('[data-tab-slug=project_tracker] button')].some(b=>b.textContent==='Approve')"), label + " admin sees pending source approval");
          assert.ok(await js("!document.querySelector('.sidebar-devmode-btn, .nav-item[data-tab=new-tab]') && !document.documentElement.classList.contains('dev-mode')"), label + " retired Developer Mode and New Tab entry are gone");
          await js("[...document.querySelectorAll('[data-tab-slug=crm] button')].find(b=>b.textContent==='Add').click()");
          await waitFor("!!document.querySelector('.nav-item[data-tab=crm]') && document.querySelector('[data-tab-slug=crm]').textContent.includes('Remove')");
          assert.ok(writes.some(w => w.path === "/api/system/tab-templates/crm" && JSON.parse(w.body).enabled), label + " Add enables a prebuilt immediately");
          await js("[...document.querySelectorAll('[data-tab-slug=crm] button')].find(b=>b.textContent==='Remove').click()");
          await waitFor("!document.querySelector('.nav-item[data-tab=crm]') && document.querySelector('[data-tab-slug=crm]').textContent.includes('Add')");
          assert.deepEqual(await overflow(), [], label + " tabs category overflow");
          assert.ok(await js("[...document.querySelectorAll('.tool-store-manage button')].some(b=>!b.hidden && b.textContent==='Build a tab') && [...document.querySelectorAll('.tool-store-manage button')].some(b=>!b.hidden && b.textContent==='Install a tab')"), label + " tab creation actions in header");
          assert.ok(await js("document.querySelector('[data-tab-slug=crm] .tool-store-card-foot button').classList.contains('primary') && document.querySelector('[data-tab-slug=project_tracker] .tool-store-card-foot button').classList.contains('primary')"), label + " Add and Approve are primary buttons");
          assert.ok(await js("[...document.querySelectorAll('[data-tab-slug=project_tracker] .tool-store-card-foot button')].some(b=>b.textContent==='Export' && b.classList.contains('quiet')) && !!document.querySelector('[data-tab-slug=project_tracker] .btn.quiet.danger')"), label + " Export and Remove are styled buttons");
          assert.ok(await js("document.querySelector('[data-tab-slug=crm] .set-pill-muted')?.textContent === 'Off' && document.querySelector('[data-tab-slug=project_tracker] .set-pill-warn')?.textContent === 'Needs approval'"), label + " shared status tones");
          assert.ok(await js("[...document.querySelectorAll('.tool-store-manage button')].find(b=>b.textContent==='Build a tab').classList.contains('primary')"), label + " Build a tab is the primary header action");
          assert.ok(await js("document.querySelector('.tool-store-build-card')?.textContent.includes('Build your own tab') && !!document.querySelector('.tool-store-build-card .btn.primary')"), label + " Yours ends with a Build your own tab card");
          await js("document.getElementById('view-content').scrollTop=0");
          await capture(label + "-tool-store-tabs");
          const reviewOpener = `[...document.querySelectorAll('[data-tab-slug=project_tracker] button')].find(b=>b.textContent==='Review files')`;
          const reviewReady = "!!document.querySelector('.modal-panel .tool-store-tab-review')";
          await popupDismissals(reviewOpener, reviewReady, label + ' Review files');
          await openPopup(reviewOpener, reviewReady, label + ' Review files');
          assert.ok(await js("document.querySelector('.modal-panel .tool-store-tab-files').textContent.includes('routes.py') && document.querySelector('.modal-panel .tool-store-tab-hash').textContent.includes('a'.repeat(64))"), label + ' file review includes source list and fingerprint');
          assert.ok(await js("!!document.querySelector('.modal-panel .store-share') && ['Approve','Remove','Export'].every(text=>[...document.querySelectorAll('.modal-panel button')].some(b=>b.textContent===text))"), label + ' review keeps every action');
          await capture(label + "-tool-store-tabs-review");
          await closePopup('button', label + ' Review files');
          await popupDismissals(headerButton('Install a tab'), "!!document.querySelector('.modal-panel input[aria-label=\"Tab archive\"]')", label + ' Install a tab');
          await popupDismissals(headerButton('Build a tab'), "!!document.querySelector('.modal-panel .tab-build-form:not([data-builder])')", label + ' Build a tab');
          await popupDismissals("document.querySelector('.tool-store-build-card button')", "!!document.querySelector('.modal-panel .tab-build-form:not([data-builder])')", label + ' Build your own tab');
          // The approval request must carry the fingerprint displayed in the review.
          await openPopup(reviewOpener, reviewReady, label + ' approval');
          const reviewedFingerprint = await js("document.querySelector('.modal-panel .tool-store-tab-hash').textContent.replace('SHA-256: ', '')");
          await js("[...document.querySelectorAll('.modal-panel button')].find(b=>b.textContent==='Approve').focus(); [...document.querySelectorAll('.modal-panel button')].find(b=>b.textContent==='Approve').click()");
          await waitFor("!!document.querySelector('.confirm-panel')");
          await js("[...document.querySelectorAll('.confirm-panel button')].find(b=>b.textContent==='Approve source').click()");
          await waitFor("!document.querySelector('.panel-dialog') && document.querySelector('[data-tab-slug=project_tracker] .set-pill-ok')?.textContent === 'On'");
          assert.ok(writes.slice(tabStoreWrites).some(w=>w.path==='/api/system/custom-tabs/project_tracker/approve' && JSON.parse(w.body).fingerprint===reviewedFingerprint), label + ' approval binds to the reviewed fingerprint');
          fixture(new URL('/api/system/tabs', base)).find(t=>t.slug==='project_tracker').status='needs_approval';
          if (label === "desktop") {
            // The card at the end of Yours opens the same brief as the header button.
            await openPopup("document.querySelector('.tool-store-build-card button')", "!!document.querySelector('.modal-panel .tab-build-form:not([data-builder])')", label + ' tab build handoff');
            await js("document.querySelector('.modal-panel .tab-build-form:not([data-builder]) input').value='Research'; document.querySelector('.modal-panel .tab-build-form:not([data-builder]) textarea').value='Track my sources'; document.querySelector('.modal-panel .tab-build-form:not([data-builder]) .tab-build-build-btn').click()");
            await waitFor("document.querySelector('.nav-item[data-tab=chat]').classList.contains('active')");
            await waitFor("!!document.querySelector('.chat-layout')");
            await waitFor("sessionStorage.getItem('jarvis:pendingChatHandoff') === null");
            for (let i = 0; i < 80 && !writes.some(w => w.path === "/api/chat/stream" && w.body.includes('Research')); i++) await delay(50);
            const handoff = writes.find(w => w.path === "/api/chat/stream" && w.body.includes('Research'));
            assert.ok(handoff && JSON.parse(handoff.body).message.startsWith('First call read_skill with slug "build-custom-tab"'), "Build reads the tab skill first");
            await navigate("tool-store");
          }
          writes.splice(tabStoreWrites);
        }
        if (tab === "calendar" && !demoState.empty) {
          const calendarWrites = writes.length;
          await waitFor("[...document.querySelectorAll('.cal-day-panel .card')].some(c=>c.textContent.includes('Review the course project'))");
          assert.ok(await js("[...document.querySelectorAll('.cal-day-panel .card')].find(c=>c.textContent.includes('Review the course project')).textContent.includes('School')"), label + " Calendar names the tab source");
          // Toggle it either way (desktop leaves it ticked): each layout must send its own PATCH.
          const wasChecked = await js("[...document.querySelectorAll('.cal-day-panel .card')].find(c=>c.textContent.includes('Review the course project')).querySelector('input[type=checkbox]').checked");
          await js("[...document.querySelectorAll('.cal-day-panel .card')].find(c=>c.textContent.includes('Review the course project')).querySelector('input[type=checkbox]').click()");
          await waitFor(`[...document.querySelectorAll('.cal-day-panel .card')].find(c=>c.textContent.includes('Review the course project'))?.querySelector('input').checked === ${!wasChecked}`);
          // The PATCH goes out just after the box ticks; poll for it.
          for (let i = 0; i < 80 && !writes.slice(calendarWrites).some(w => w.path === "/api/tab-school/assignments/a1" && w.method === "PATCH"); i++) await delay(50);
          assert.ok(writes.slice(calendarWrites).some(w => w.path === "/api/tab-school/assignments/a1" && w.method === "PATCH"), label + " Calendar PATCHes the tab toggle URL");
          writes.splice(calendarWrites);
          await require('./google-workspace-checks.cjs')({ js, waitFor, capture, navigate, overflow, reads, writes, label });
        }
        if (tab === "tasks" && !demoState.empty) {
          // The work board (Hermes track 2026-09-23): cards sit in their
          // columns with the actions that column allows, and stay out of the
          // scheduled list.
          await waitFor("document.querySelectorAll('.board-card').length === 4");
          assert.ok(await js("[...document.querySelectorAll('.board-card-running button')].some(b => b.textContent === 'Stop')"), label + " a running card offers Stop");
          assert.ok(await js("[...document.querySelectorAll('.board-card-review button')].some(b => b.textContent === 'Approve')"), label + " review card offers Approve");
          assert.ok(await js("document.querySelector('.board-card-blocked .board-card-text').textContent.includes('model stopped')"), label + " blocked card shows its error");
          assert.ok(await js("document.querySelector('.board-card-ready').textContent.includes('waits for Gather sources')"), label + " a waiting card names what it waits for");
          assert.ok(await js("!document.getElementById('tasks-list').textContent.includes('Gather sources')"), label + " cards stay out of the scheduled list");
          // Run history: the scheduled task's last run and its full history,
          // and a card's history loaded when opened.
          await waitFor("!!document.querySelector('#tasks-list .run-history-panel')");
          assert.ok(await js("document.getElementById('tasks-list').textContent.includes('Succeeded · 42s')"), label + " last run shows outcome and duration");
          assert.equal(await js("document.querySelector('#tasks-list .run-history-panel > summary').textContent"), "Run history (4)", label + " history count");
          assert.deepEqual(await js("[...document.querySelectorAll('#tasks-list .run-outcome')].map(n => n.textContent)"), ["Succeeded", "Stopped", "Didn't finish", "Failed"], label + " each outcome labelled");
          // Durable work (roadmap phase 4): a running task offers Stop, a late
          // run says so, and a given-up delivery can be sent again.
          assert.ok(await js("[...document.querySelectorAll('#tasks-list button')].some(b => b.textContent === 'Stop')"), label + " a running task offers Stop");
          assert.ok(await js("document.getElementById('tasks-list').textContent.includes('was given up — check Settings > Channels')"), label + " a given-up delivery is said on the last run");
          assert.ok(await js("[...document.querySelectorAll('#tasks-list .run-delivery button')].some(b => b.textContent === 'Send again')"), label + " and can be sent again");
          assert.ok(await js("[...document.querySelectorAll('#tasks-list .run-meta')].some(n => n.textContent.includes('late: due'))"), label + " a late run says so");
          await js("document.querySelector('#tasks-list .run-history-panel').open = true; document.getElementById('tasks-list').scrollIntoView()");
          await capture(label + "-task-list");
          assert.ok(await js("document.querySelector('#tasks-list .run-history').textContent.includes('duration unknown')"), label + " an old record's duration is unknown, not guessed");
          await js("document.querySelector('.board-card-review .board-card-output:last-of-type').open = true");
          await waitFor("!!document.querySelector('.board-card-review .run-row')");
          assert.ok(await js("document.querySelector('.board-card-review .run-row').textContent.includes('2m 05s · Local model · attempt 1')"), label + " card history row");
          // Webhook triggers: what waits for your OK, each trigger's state, its page and the add form.
          await waitFor("document.querySelectorAll('.trigger-row').length === 2");
          assert.ok(await js("document.querySelector('.trigger-pending').textContent.includes('GitHub pushes: Push: Fix the thing')"), label + " a waiting event is listed");
          assert.ok(await js("[...document.querySelectorAll('.trigger-pending button')].some(b => b.textContent === 'Approve')"), label + " and can be approved here");
          assert.ok(await js("document.querySelector('[data-trigger=tr1]').textContent.includes('Asks first') && document.querySelector('[data-trigger=tr2]').textContent.includes('Runs straight away')"), label + " each trigger says whether it asks first");
          await js("document.querySelector('.triggers-card').scrollIntoView()");
          await capture(label + "-triggers");
          await js("document.querySelector('[data-trigger=tr1] .set-row-title').click()");
          await waitFor("document.querySelectorAll('.trigger-event').length === 2");
          assert.ok(await js("document.querySelector('.triggers-card').textContent.includes('/api/triggers/tr1')"), label + " the trigger's address is shown");
          assert.ok(!(await js("document.querySelector('.triggers-card').textContent.includes('Secret')")), label + " no secret on a saved trigger");
          assert.deepEqual(await overflow(), [], label + " trigger page overflow");
          await js("document.querySelector('.triggers-card').scrollIntoView()");
          await capture(label + "-trigger-page");
          await js("document.querySelector('.triggers-card .set-back').click()");
          await waitFor("document.querySelectorAll('.trigger-row').length === 2");
          await js("[...document.querySelectorAll('.triggers-card .btn')].find(b => b.textContent === 'Add a trigger').click()");
          await waitFor("!!document.querySelector('.trigger-add')");
          assert.deepEqual(await overflow(), [], label + " add trigger overflow");
          await js("document.querySelector('.triggers-card').scrollIntoView()");
          assert.ok(await js("[...document.querySelectorAll('.trigger-add .set-row-title')].filter(t => t.offsetParent).every(t => t.getBoundingClientRect().width > 60)"), label + " field labels keep their width");
          assert.equal(await js("[...document.querySelectorAll('.trigger-add .set-row-title')].filter(t => t.offsetParent).map(t => t.textContent).includes('Task')"), false, label + " a card trigger hides the task picker");
          await capture(label + "-trigger-add");
          await js("document.querySelector('.triggers-card .set-back').click()");
          await waitFor("document.querySelectorAll('.trigger-row').length === 2");
        }
        assert.deepEqual(await overflow(), [], label + " overflow in " + tab);
        await capture(label + "-" + tab);
      }
      await navigate("library", { section: "vault" });
      await js("[...document.querySelectorAll('.library-vault-modes button')].find(b => b.textContent === 'Map').click()");
      await waitFor("document.querySelector('.vault-caption').textContent.includes('130 notes')");
      await delay(400);
      await capture(label + "-vault");
      await js("document.querySelector('.vault-search').value = 'Projects index'; document.querySelector('.vault-search').dispatchEvent(new Event('input'))");
      assert.equal(await js("document.querySelectorAll('.vault-search-result').length"), 1);
      await js("document.querySelector('.vault-search-result').click()");
      await waitFor("document.querySelector('.vault-panel-body').textContent.includes('connected place')");
      assert.equal(await js("document.querySelector('.vault-note-content h1')?.textContent"), "Projects index", "Vault Markdown has a rendered heading");
      assert.equal(await js("document.querySelector('.vault-note-content strong')?.textContent"), "connected place", "Vault Markdown has rendered emphasis");
      assert.ok(await js("!document.querySelector('.vault-note-content').textContent.includes('status: active')"), "Vault reading view hides frontmatter");
      assert.equal(await js("document.querySelector('.vault-note-link')?.textContent"), "the next note", "Wikilink has a readable label");
      await js("document.querySelector('.vault-panel-actions button').click()");
      assert.ok(await js("document.querySelector('#vault-note-editor').value.includes('status: active')"), "Edit preserves original frontmatter");
      await js("document.querySelector('.vault-panel-actions button:nth-child(3)').click()");
      await waitFor("!!document.querySelector('.vault-note-content h1')");
      await capture(label + "-vault-note");
      assert.deepEqual(await overflow(), [], label + " vault note overflow");
      await js("document.querySelector('.vault-note-link').click()");
      assert.equal(await js("document.querySelector('.vault-panel-header span').textContent"), "Projects note 1", "Wikilink opens its vault note");
      await navigate("home");
      await js("document.querySelector('.sidebar-settings-btn').click()");
      if (label === "mobile") {
        // Phones (2026-10-05): Settings opens on a list of sections, not a
        // page with a sideways strip of tabs; a row opens its page.
        await waitFor("document.querySelector('#view-content.settings-mobile-page')?.dataset.mobileView === 'list' && document.querySelectorAll('.settings-nav-block .settings-nav-item').length > 10");
        assert.equal(await js("document.querySelector('.settings-mobile-title').textContent"), "Settings", "phone list is titled Settings");
        assert.equal(await js("getComputedStyle(document.querySelector('#settings-content')).display"), "none", "no page behind the list");
        assert.equal(await js("document.querySelector('.settings-nav-list').scrollWidth <= document.querySelector('.settings-nav-list').clientWidth + 1"), true, "the list does not scroll sideways");
        assert.ok(await js("document.querySelector('.settings-search').getBoundingClientRect().width > 250"), "Settings search has room to type");
        assert.deepEqual(await overflow(), [], label + " settings list overflow");
        await capture("mobile-settings-list");
        await js("document.querySelector('[data-section=\"add-models\"]').click()");
        await waitFor("document.querySelector('#view-content.settings-mobile-page')?.dataset.mobileView === 'page'");
        assert.equal(await js("document.querySelector('.settings-mobile-title').textContent"), "Add Models", "the bar names the open page");
      }
      // Every Settings page has the same frame (redesign 2026-10-05): a
      // header with its title, then groups of rows.
      await waitFor("!!document.querySelector('#settings-content .set-header .set-title') && !!document.querySelector('#settings-content .set-row, #settings-content .set-empty')");
      assert.equal(await js("document.querySelector('#settings-content .set-title').textContent"),
        await js("document.querySelector('.settings-nav-item.active').textContent"), label + " page header names the page");
      assert.equal(await js("document.querySelectorAll('.settings-nav-item svg').length === document.querySelectorAll('.settings-nav-item').length"), true, label + " every nav item has an icon");
      assert.deepEqual(await overflow(), [], label + " settings overflow");
      if (label === "mobile") {
        assert.ok(await js("[...document.querySelectorAll('#settings-content input')].filter(e=>e.offsetWidth).every(e=>e.offsetWidth>=170)"), "Mobile model fields do not collapse");
      }

      // -- grouped nav + keyword search (David's ask 2026-09-15).
      assert.deepEqual(
        await js("[...document.querySelectorAll('.settings-nav-group')].filter(g=>!g.hidden).map(g=>g.textContent)"),
        ["Models", "Connections", "Workspace", "Personal", "Administration"],
        label + " settings groups",
      );
      assert.ok(!(await js("document.querySelector('[data-section=custom-tabs]')")), "Tabs are managed in the Tool Store");
      // -- Speech panel (David's ask 2026-09-15). Without it there is no way
      // to obtain a model, so dictation could only ever refuse.
      await js("document.querySelector('[data-section=speech]').click()");
      // The panel root mounts before its status fetch resolves, so waiting on
      // the root alone races the content it is supposed to be showing.
      await waitFor("document.querySelectorAll('.speech-model').length===2");
      // A downloaded model shows its state; an absent one offers the download.
      assert.ok(await js("document.querySelector('.speech-panel').textContent.includes('Downloaded')"), 'Downloaded model is marked');
      // Scoped to the model rows: .speech-model-actions is the shared layout
      // for the device and voice rows too, and an unscoped count silently
      // turns this into an assertion about the whole panel.
      assert.equal(await js("document.querySelectorAll('.speech-model .speech-model-actions .btn').length"), 1, 'Only the missing model offers a download');
      assert.ok(await js("document.querySelector('#settings-content .set-description').textContent.includes('never uploaded')"), 'Page states audio stays local');
      // -- device selection. Input is selectable; output deliberately is not,
      // because speechSynthesis exposes no sink control at all (verified
      // against this Chromium). The panel says so rather than staying silent,
      // since this is exactly where someone looks for it.
      await waitFor("!!document.querySelector('#speech-input-device')");
      assert.ok(await js("[...document.querySelector('#speech-input-device').options].some(o=>o.value==='')"), 'System default is offered');
      // The voice picker (David's ask 2026-09-15). The output-device limitation
      // is stated here, next to the voice, because that is where someone looks
      // for it once they have picked how the replies sound.
      await waitFor("!!document.querySelector('.speech-voice')");
      assert.ok(await js("document.querySelector('.speech-voice').textContent.includes('system default output')"), 'Output limitation is stated, not hidden');
      assert.ok(await js("[...document.querySelector('#speech-voice').options].some(o=>o.value==='')"), 'Automatic voice is offered');
      await js("import('/static/js/voiceOutput.js').then(m=>m.setPreferredVoice('Fixture Voice'))");
      assert.equal(await js("import('/static/js/voiceOutput.js').then(m=>m.getPreferredVoice())"), 'Fixture Voice');
      await js("import('/static/js/voiceOutput.js').then(m=>m.setPreferredVoice(''))");
      assert.equal(await js("import('/static/js/voiceOutput.js').then(m=>m.getPreferredVoice())"), '');
      // A British voice is preferred when one exists, which is the whole point
      // of the preference order - asserted against fixtures because this
      // machine has no en-GB voice installed to test against.
      assert.equal(await js(`import('/static/js/voiceOutput.js').then(m=>m.resolveVoice([
        {name:'Microsoft Zira Desktop',lang:'en-US',localService:true},
        {name:'Microsoft Ryan',lang:'en-GB',localService:true},
        {name:'Microsoft Hazel',lang:'en-GB',localService:true}
      ]).name)`), 'Microsoft Ryan', 'A British male voice wins when installed');
      assert.equal(await js(`import('/static/js/voiceOutput.js').then(m=>m.resolveVoice([
        {name:'Cloud Voice',lang:'en-GB',localService:false},
        {name:'Microsoft David Desktop',lang:'en-US',localService:true}
      ]).name)`), 'Microsoft David Desktop', 'A local voice beats a better-matching network one');
      // The choice persists locally and survives a reload.
      await js("import('/static/js/voiceInput.js').then(m=>m.setInputDevice('fixture-device-id'))");
      assert.equal(await js("import('/static/js/voiceInput.js').then(m=>m.getInputDevice())"), 'fixture-device-id');
      await js("import('/static/js/voiceInput.js').then(m=>m.setInputDevice(''))");
      assert.equal(await js("import('/static/js/voiceInput.js').then(m=>m.getInputDevice())"), '');
      assert.deepEqual(await overflow(), [], label + " speech overflow");
      await capture(label + "-speech");
      // -- Logs (Hermes track, 2026-09-22): entries render by level, a
      // traceback stays with its line, and a chat is named, not shown as an id.
      await js("document.querySelector('[data-section=logs]').click()");
      await waitFor("document.querySelectorAll('.log-entry').length===3");
      assert.ok(await js("document.querySelector('.log-entry.log-error').textContent.includes('RuntimeError: the model stopped responding')"), "Traceback stays with its entry");
      assert.equal(await js("document.querySelector('.log-entry.log-error .log-tag').textContent"), "A clearer direction for the workspace", "A chat is shown by its title");
      assert.ok(await js("!!document.querySelector('.log-entry.log-warning') && !document.querySelector('.log-entry.log-warning .log-tag')"), "An untagged line has no chat label");
      assert.ok(await js("document.querySelector('.logs-file').textContent.includes('Backend (200 KB)')"), "Files show their size");
      assert.deepEqual(await overflow(), [], label + " logs overflow");
      await capture(label + "-logs");
      // -- MCP catalog (Hermes track 2026-09-23): one-step add only where no sign-in is needed.
      // Roadmap phase 8: the Runs list opens a failed run step by step.
      await js("document.querySelector('[data-section=runs]').click()");
      await waitFor("document.querySelectorAll('.run-item').length === 2");
      assert.ok(await js("document.querySelector('[data-run=r2]').textContent.includes('Card: Draft the newsletter')"), label + " a run says what it was");
      await js("document.querySelector('[data-run=r2] .run-item-head .btn').click()");
      await waitFor("document.querySelectorAll('[data-run=r2] .run-step').length === 5");
      assert.ok(await js("document.querySelector('[data-run=r2] .run-step.is-error').textContent.includes('create_note (failed)')"), label + " a failed tool is marked");
      assert.ok(await js("document.querySelector('[data-run=r2] .run-timeline').textContent.includes('Helpers: done')"), label + " what hangs off the run");
      assert.deepEqual(await overflow(), [], label + " runs overflow");
      await capture(label + "-runs-steps");
      await js("document.querySelector('[data-section=system]').click()");
      await waitFor("!!document.querySelector('.backup-full')");
      assert.ok(await js("document.querySelector('.backup-last').textContent.includes('before-restore-')"), label + " the last restore names its safety copy");
      await js("document.querySelector('.backup-full').scrollIntoView({ block: 'start', behavior: 'instant' })");
      await capture(label + "-settings-backup");
      await js("document.querySelector('[data-section=integrations]').click()");
      await waitFor("!!document.querySelector('.mcp-catalog')");
      await js("document.querySelector('.mcp-catalog').open = true");
      assert.ok(await js("[...document.querySelectorAll('[data-server=deepwiki] button')].some(b => b.textContent === 'Add')"), label + " a no-sign-in server offers Add");
      assert.ok(await js("[...document.querySelectorAll('[data-server=linear] button')].some(b => b.textContent === 'Add and sign in')"), label + " an OAuth server offers Add and sign in");
      const rowText = (name) => js(`document.querySelector('.integration-row[data-integration="${name}"]')?.textContent || ''`);
      assert.ok((await rowText("Notion")).includes("Signed in") && (await rowText("Notion")).includes("Sign out"), label + " a signed-in server shows it and offers Sign out");
      assert.ok((await rowText("Sentry")).includes("Needs sign-in") && !(await rowText("Sentry")).includes("Sign out"), label + " a signed-out server offers Sign in only");
      assert.ok(await js("document.querySelector('[data-server=context7]').textContent.includes('Added')"), label + " an added server is marked");
      await js("{ const s = document.querySelector('.mcp-catalog-search'); s.value = 'linear'; s.dispatchEvent(new Event('input')); }");
      assert.equal(await js("document.querySelectorAll('.mcp-catalog-row').length"), 1, label + " catalog search filters");
      assert.deepEqual(await overflow(), [], label + " integrations overflow");
      await capture(label + "-mcp-catalog");
      // -- Channels (redesign 2026-10-05): one list of every channel with its
      // live state; a row opens that channel's page; adding starts from a
      // picker of platform tiles, not an 18-item dropdown.
      await js("document.querySelector('[data-section=channels]').click()");
      await waitFor("document.querySelectorAll('.channel-row').length === 2");
      assert.equal(await js("document.querySelector('.channel-row[data-state=error] .set-pill').textContent"), "Problem", label + " a failing connector says so");
      assert.ok(await js("document.querySelector('.channel-row[data-state=error]').textContent.includes('failed (401)')"), label + " and why");
      assert.ok(await js("document.querySelectorAll('.channel-row')[1].textContent.includes('send only')"), label + " send-only is labelled");
      assert.deepEqual(await overflow(), [], label + " channels overflow");
      await capture(label + "-channels");
      await js("document.querySelector('.channel-row[data-kind=telegram] .set-row-title').click()");
      await waitFor("!!document.querySelector('.connector-settings [data-field=bot_token]')");
      assert.equal(await js("document.querySelector('#settings-content .set-title').textContent"), "My phone", label + " a channel has its own page");
      assert.equal(await js("document.querySelector('[data-field=bot_token] input').placeholder"), "Saved; leave blank to keep", label + " a saved token is never shown");
      assert.deepEqual(await overflow(), [], label + " channel page overflow");
      await capture(label + "-channel-page");
      await js("document.querySelector('.set-back').click()");
      await waitFor("document.querySelectorAll('.channel-row').length === 2");
      await js("[...document.querySelectorAll('.set-header .btn')].find(b => b.textContent === 'Add a channel').click()");
      await waitFor("!!document.querySelector('.set-tile[data-kind=discord]') && !!document.querySelector('.set-tile[data-kind=ntfy]')");
      assert.deepEqual(await overflow(), [], label + " channel picker overflow");
      await capture(label + "-channel-picker");
      await js("document.querySelector('.set-tile[data-kind=telegram]').click()");
      await waitFor("[...document.querySelectorAll('.connector-field .set-row-title')].some(l => l.textContent === 'Bot token')");
      assert.deepEqual(await overflow(), [], label + " add channel overflow");
      await capture(label + "-connectors");
      // -- Sandbox changes (Hermes phase 7): review before anything reaches the code.
      await js("document.querySelector('[data-section=\"sandbox-changes\"]').click()");
      await waitFor("document.querySelectorAll('.sandbox-change').length === 2");
      await js("document.querySelector('[data-change=c0ffee000001] .sandbox-change-actions button').click()");
      await waitFor("!!document.querySelector('[data-change=c0ffee000001] .sandbox-diff-line.add')");
      assert.ok(await js("document.querySelector('[data-change=c0ffee000001] .sandbox-diff-line.del').textContent.includes('<b>old</b>')"),
        label + " a diff is shown as text, never as HTML");
      assert.equal(await js("document.querySelector('[data-change=c0ffee000001] .sandbox-diff b')"), null, label + " no markup from a diff");
      const applyDisabled = (id) => js(`[...document.querySelectorAll('[data-change=${id}] button')].find(b => b.textContent === 'Apply').disabled`);
      assert.equal(await applyDisabled("c0ffee000001"), false, label + " an applicable change set can be applied");
      assert.equal(await applyDisabled("c0ffee000002"), true, label + " an oversized one is read only");
      assert.deepEqual(await overflow(), [], label + " sandbox changes overflow");
      await capture(label + "-sandbox-changes");
      // -- Hooks (2026-10-05): each hook with its last outcome; its page shows
      // the exact command and its runs; a new command is shown in full and
      // confirmed before anything is saved.
      await js("document.querySelector('[data-section=hooks]').click()");
      await waitFor("document.querySelectorAll('.hook-row').length === 2");
      assert.equal(await js("document.querySelector('[data-hook=h1] .set-pill').textContent"), "Blocked", label + " a hook shows its last outcome");
      assert.equal(await js("document.querySelector('[data-hook=h2] .set-switch').getAttribute('aria-checked')"), "false", label + " a hook that is off says so");
      assert.ok(await js("document.querySelector('.set-page[data-page=hooks] .set-note').textContent.includes('Codex')"), label + " what hooks cannot see is stated");
      assert.deepEqual(await overflow(), [], label + " hooks overflow");
      await capture(label + "-hooks");
      await js("document.querySelector('[data-hook=h1] .set-row-title').click()");
      await waitFor("document.querySelectorAll('.hook-run').length === 2");
      assert.ok(await js("document.querySelector('#settings-content').textContent.includes('guard.py')"), label + " the exact command is shown");
      assert.ok(await js("document.querySelector('#settings-content').textContent.includes('Only Scout · Agents only')"), label + " and whose work it watches");
      assert.deepEqual(await overflow(), [], label + " hook page overflow");
      await capture(label + "-hook-page");
      await js("document.querySelector('#settings-content .set-back').click()");
      await waitFor("document.querySelectorAll('.hook-row').length === 2");
      await js("document.querySelector('[data-hook=h2] .set-row-title').click()");
      await waitFor("document.querySelector('#settings-content').textContent.includes('hooks.example.test')");
      assert.ok(await js("document.querySelector('#settings-content').textContent.includes('X-JARVIS-Signature')"), label + " a signed post says so, and never shows the secret");
      await js("document.querySelector('#settings-content .set-back').click()");
      await waitFor("document.querySelectorAll('.hook-row').length === 2");
      await js("[...document.querySelectorAll('#settings-content .set-header .btn')].find(b => b.textContent === 'Add a hook').click()");
      await waitFor("!!document.querySelector('.hook-form')");
      const hookFields = () => js("[...document.querySelectorAll('.hook-form .set-row-title')].filter(t => t.offsetParent).map(t => t.textContent)");
      assert.deepEqual(await hookFields(), ["Start from", "Name", "When", "Only for", "From", "Does", "Channel", "Text"], label + " a new hook starts as a channel message");
      await js("(() => { const s = document.querySelector('.hook-form .custom-select'); s.value = '3'; s.dispatchEvent(new Event('change')); })()");
      assert.deepEqual(await hookFields(), ["Start from", "Name", "When", "Tools", "Only for", "From", "Does", "Command", "Stops after", "Block if it fails"],
        label + " a blocking command shows only its own fields");
      assert.deepEqual(await overflow(), [], label + " add hook overflow");
      await capture(label + "-hook-add");
      // Earlier steps (Tool Store, Calendar) save things; count only this one.
      const writesBeforeCancel = writes.length;
      await js("[...document.querySelectorAll('.hook-form .btn')].find(b => b.textContent === 'Add hook').click()");
      await waitFor("!!document.querySelector('.confirm-panel')");
      assert.ok(await js("document.querySelector('.confirm-panel').textContent.includes(\"$d -match 'Remove-Item\")"), label + " the exact command is shown before it is saved");
      await capture(label + "-hook-confirm");
      await js("[...document.querySelectorAll('.confirm-panel .btn')].find(b => b.textContent === 'Cancel').click()");
      await waitFor("!document.querySelector('.confirm-panel')");
      assert.equal(writes.length, writesBeforeCancel, label + " cancelling saves nothing");
      await js("document.querySelector('#settings-content .set-back').click()");
      await waitFor("document.querySelectorAll('.hook-row').length === 2");
      await js("document.querySelector('[data-section=vault]').click()");
      await waitFor("document.querySelector('#settings-content .set-title')?.textContent === 'Vault' && !!document.querySelector('#settings-content .set-row, #settings-content .set-empty')");
      // Search matches what someone would actually type, not just the
      // visible label — "2fa" appears nowhere in the word "Account".
      await js("{ const s=document.querySelector('.settings-search'); s.value='2fa'; s.dispatchEvent(new Event('input')); }");
      await waitFor("[...document.querySelectorAll('.settings-nav-item')].filter(i=>!i.hidden).length===1");
      assert.equal(await js("document.querySelector('.settings-nav-item:not([hidden])').dataset.section"), "account");
      // An emptied group takes its heading with it rather than leaving a
      // stray label over nothing.
      assert.deepEqual(await js("[...document.querySelectorAll('.settings-nav-group')].filter(g=>!g.hidden).map(g=>g.textContent)"), ["Personal"]);
      await js("{ const s=document.querySelector('.settings-search'); s.value='tailscale'; s.dispatchEvent(new Event('input')); }");
      await waitFor("document.querySelector('.settings-nav-item:not([hidden])').dataset.section==='remote'");
      await js("{ const s=document.querySelector('.settings-search'); s.value=''; s.dispatchEvent(new Event('input')); }");
      await waitFor("[...document.querySelectorAll('.settings-nav-item')].filter(i=>!i.hidden).length>5");
      await capture(label + "-settings");
      await js("document.querySelector('[data-section=computer-use]').click()");
      await waitFor("document.querySelector('.set-page[data-page=computer-use] .set-switch')");
      await capture(label + '-settings-computer-use');
      const computerWriteStart = writes.length;
      // Each click waits for its own save: a click that lands while the last
      // save is in flight, or a wait satisfied by an older request, would let
      // a late write leak into the next pass.
      const toggleSaves = async (selector, key, value, what) => {
        await waitFor(`!${selector}?.disabled`);
        const start = writes.length;
        await js(`${selector}.click()`);
        const saved = () => writes.slice(start).some(w => w.path === '/api/settings/computer-use' && JSON.parse(w.body)[key] === value);
        for (let i = 0; i < 60 && !saved(); i++) await delay(50);
        assert.ok(saved(), `${label} ${what}`);
        await waitFor(`${selector}?.getAttribute('aria-checked') === '${value}'`);
      };
      const mainSwitch = "document.querySelector('.set-page[data-page=computer-use] .set-switch')";
      await toggleSaves(mainSwitch, 'enabled', false, 'computer switch saves');
      await toggleSaves(mainSwitch, 'enabled', true, 'computer switch saves on');
      const reactionSwitch = "document.querySelector('.set-page[data-page=computer-use] [aria-label=\"Let the computer like, follow and react for you\"]')";
      assert.equal(await js(`${reactionSwitch}?.getAttribute('aria-checked')`), 'false', label + ' reactions stay with the person by default');
      await toggleSaves(reactionSwitch, 'allow_reactions', true, 'reactions switch saves');
      await toggleSaves(reactionSwitch, 'allow_reactions', false, 'reactions switch saves off');
      const desktopSwitch = "document.querySelector('.set-page[data-page=computer-use] [aria-label=\"Agent desktop\"]')";
      assert.equal(await js(`${desktopSwitch}?.getAttribute('aria-checked')`), 'false', label + ' agent desktop is opt-in');
      assert.ok(await js("document.querySelector('.set-page[data-page=computer-use]').textContent.includes('The desktop image will be prepared on first use.')"), label + ' desktop image status renders');
      await toggleSaves(desktopSwitch, 'desktop', true, 'agent desktop switch saves');
      await js("document.querySelector('[data-section=vault]').click()");
      await waitFor("document.querySelector('.set-page')?.dataset.page === 'vault'");
      await js("document.querySelector('[data-section=computer-use]').click()");
      await waitFor(`${desktopSwitch}?.getAttribute('aria-checked') === 'true'`);
      await toggleSaves(desktopSwitch, 'desktop', false, 'agent desktop switch saves off');
      demoState.computerUse = null;
      writes.splice(computerWriteStart);
      // Every page opens in the same frame (redesign 2026-10-05): its own
      // title in the header, real content under it, nothing wider than the
      // pane. A page that throws leaves the header without content.
      for (const id of await js("[...document.querySelectorAll('.settings-nav-item')].map(i => i.dataset.section)")) {
        await js(`document.querySelector('[data-section="${id}"]').click()`);
        await waitFor(`document.querySelector('.set-page')?.dataset.page === '${id}' && document.querySelector('#settings-content .set-title')?.textContent === document.querySelector('[data-section="${id}"]').textContent
          && !!document.querySelector('#settings-content .set-body').querySelector('.set-row, .set-empty, .appearance-panel, .layout-list, .log-entry, .logs-status, .sandbox-change, .run-item')`);
        assert.deepEqual(await overflow(), [], `${label} ${id} overflow`);
        await capture(`${label}-settings-${id}`);
      }
      // Long local/API chats compact themselves unless switched off (roadmap phase 3).
      await js("document.querySelector('[data-section=\"added-models\"]').click()");
      await waitFor("!!document.querySelector('.long-chats .set-switch')");
      assert.equal(await js("document.querySelector('.long-chats .set-switch').getAttribute('aria-checked')"), "true",
        label + " auto-compaction is on by default");
      // A long dropdown scrolls instead of closing (found 2026-10-05: the
      // platform list closed the moment it was scrolled). Add Models' provider
      // list is long enough to scroll.
      await js("document.querySelector('[data-section=\"add-models\"]').click()");
      await waitFor("document.querySelectorAll('.set-page[data-page=\"add-models\"] .custom-select').length > 0");
      await js("document.querySelector('.set-page .custom-select .custom-select-btn').scrollIntoView({ block: 'end' })");
      await delay(200);
      await js("document.querySelector('.set-page .custom-select .custom-select-btn').click()");
      await waitFor("[...document.querySelectorAll('.custom-select-menu')].some(m => !m.classList.contains('hidden') && m.scrollHeight > m.clientHeight)");
      await js("{ const m = [...document.querySelectorAll('.custom-select-menu')].find(m => !m.classList.contains('hidden')); m.scrollTop = 120; m.dispatchEvent(new Event('scroll')); }");
      await delay(150);
      assert.ok(await js("[...document.querySelectorAll('.custom-select-menu')].some(m => !m.classList.contains('hidden') && m.scrollTop > 0)"), label + " a scrolled dropdown stays open");
      assert.ok(await js("(() => { const m = [...document.querySelectorAll('.custom-select-menu')].find(m => !m.classList.contains('hidden')).getBoundingClientRect(); return m.top >= 0 && m.bottom <= innerHeight; })()"), label + " the dropdown fits on screen");
      await js("document.body.click()");

      if (label === "desktop") {
        // -- titlebar drag, constrained to the viewport. Losing the titlebar
        // off screen would be unrecoverable, since dragging is the only way
        // to bring the window back.
        const drag = (dx, dy) => js(`(() => {
          const bar = document.querySelector('.settings-titlebar');
          const r = bar.getBoundingClientRect();
          const from = { clientX: r.left + r.width / 2, clientY: r.top + r.height / 2, button: 0, bubbles: true };
          bar.dispatchEvent(new MouseEvent('mousedown', from));
          document.dispatchEvent(new MouseEvent('mousemove', { ...from, clientX: from.clientX + ${dx}, clientY: from.clientY + ${dy}, bubbles: true }));
          document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
          const w = document.querySelector('.settings-window').getBoundingClientRect();
          return { left: Math.round(w.left), top: Math.round(w.top), right: Math.round(w.right), bottom: Math.round(w.bottom) };
        })()`);
        const moved = await drag(60, 40);
        const pinned = await js("(() => { const w=document.querySelector('.settings-window').getBoundingClientRect(); return {left:Math.round(w.left), top:Math.round(w.top)}; })()");
        assert.equal(moved.left, pinned.left, "window actually moves with the titlebar");
        // Shoved hard past every edge in turn; it must stay wholly inside.
        for (const [dx, dy] of [[-4000, -4000], [4000, 4000]]) {
          const box = await drag(dx, dy);
          assert.ok(box.left >= 0 && box.top >= 0, `stays inside top-left (${JSON.stringify(box)})`);
          assert.ok(box.right <= 1441 && box.bottom <= 901, `stays inside bottom-right (${JSON.stringify(box)})`);
        }
        // Shrinking the app must not strand a window that was fine before.
        win.setContentSize(900, 640);
        await delay(150);
        const after = await js("(() => { const w=document.querySelector('.settings-window').getBoundingClientRect(); return {left:Math.round(w.left), top:Math.round(w.top), right:Math.round(w.right), bottom:Math.round(w.bottom)}; })()");
        assert.ok(after.left >= 0 && after.top >= 0 && after.right <= 901 && after.bottom <= 641, `re-clamped on resize (${JSON.stringify(after)})`);
        win.setContentSize(1440, 900);
        await delay(150);
      }

      // Leaving Settings keeps you on the tab you were on (David,
      // 2026-10-05), never Home. Opened here over Tasks to prove it.
      if (label === "mobile") {
        // A sub-page goes back to its page, the page to the list.
        await js("document.querySelector('[data-section=channels]').click()");
        await waitFor("document.querySelectorAll('.channel-row').length === 2");
        await js("document.querySelector('.channel-row[data-kind=telegram] .set-row-title').click()");
        await waitFor("document.querySelector('.settings-mobile-title').textContent === 'My phone'");
        await capture("mobile-channel-subpage");
        await js("document.querySelector('.settings-mobile-back').click()");
        await waitFor("document.querySelector('.settings-mobile-title').textContent === 'Channels' && document.querySelectorAll('.channel-row').length === 2");
        await js("document.querySelector('.settings-mobile-back').click()");
        await waitFor("document.querySelector('#view-content.settings-mobile-page')?.dataset.mobileView === 'list'");
      }
      await navigate("tasks");
      await js("document.querySelector('.sidebar-settings-btn').click()");
      if (label === "mobile") {
        await waitFor("document.querySelector('#view-content.settings-mobile-page')?.dataset.mobileView === 'list'");
        await js("document.querySelector('.settings-mobile-back').click()");
      } else {
        await waitFor("!!document.querySelector('.modal-backdrop:not(.hidden) .settings-window')");
        // Dialogs rise in and fade out: the scrim's display waits for its fade.
        assert.ok(await js("(() => { const b = getComputedStyle(document.querySelector('.settings-window').parentElement); return b.transitionProperty.includes('display') && b.transitionBehavior.includes('allow-discrete') && getComputedStyle(document.querySelector('.settings-window')).transitionProperty.includes('transform'); })()"), label + " dialogs animate in and out");
        assert.equal(await js("getComputedStyle(document.querySelector('.set-page')).animationName"), "set-page-in", label + " Settings pages fade in");
        await js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))");
        await waitFor("!!document.querySelector('.modal-backdrop.hidden .settings-window')");
        await js("document.querySelector('.sidebar-settings-btn').click()");
        await waitFor("!!document.querySelector('.modal-backdrop:not(.hidden) .settings-window')");
        await js(`document.querySelector(".settings-titlebar-btn[title='Close']").click()`);
      }
      await delay(300);
      assert.equal(await js("document.querySelector('#view-content').dataset.view"), "tasks", label + " leaving Settings stays on the tab you were on");
      assert.ok(await js("document.querySelector('.nav-item[data-tab=tasks]').classList.contains('active')"), label + " and that tab is still highlighted");
    }
    win.setContentSize(1440, 900);
    await navigate("home");
    await waitFor("document.querySelectorAll('.dashboard-row').length > 0");
    // Forge sessions are listed too (newer here) and open the Forge screen; pick the plain chat.
    await js("[...document.querySelectorAll('.dashboard-row')].find(r => r.textContent.includes('A clearer direction for the workspace')).click()");
    await waitFor("document.querySelectorAll('#chat-messages .msg').length === 2");
    await delay(400);
    await capture("desktop-conversation");
    // -- Composer history and the terminal chat style (2026-09-25).
    const lastUserText = await js("[...document.querySelectorAll('#chat-messages .msg.user .msg-body')].at(-1)._rawText");
    const key = (name) => js(`(() => { const i = document.querySelector('#chat-input'); i.focus(); i.dispatchEvent(new KeyboardEvent('keydown', { key: '${name}', bubbles: true, cancelable: true })); return i.value; })()`);
    await js("(() => { const i = document.querySelector('#chat-input'); i.value = ''; })()");
    assert.equal(await key('ArrowUp'), lastUserText, "Up on an empty composer recalls the last message");
    assert.equal(await key('ArrowUp'), lastUserText, "Up stops at the oldest message");
    assert.equal(await key('ArrowDown'), '', "Down returns to what was being typed");
    await js("(() => { const i = document.querySelector('#chat-input'); i.value = 'half typed'; })()");
    assert.equal(await key('ArrowUp'), 'half typed', "Up never replaces text being typed");
    await js("(() => { const i = document.querySelector('#chat-input'); i.value = ''; })()");
    await js("import('/static/js/appearance.js').then((m) => m.updateAppearance({ chatStyle: 'terminal' }))");
    assert.equal(await js("document.documentElement.dataset.chatStyle"), 'terminal');
    assert.ok(await js("/Cascadia|Consolas/.test(getComputedStyle(document.querySelector('#chat-messages')).fontFamily)"), "Terminal style is monospace");
    assert.equal(await js("getComputedStyle(document.querySelector('#chat-messages .msg.user .msg-body')).backgroundColor"), 'rgba(0, 0, 0, 0)', "No bubble in terminal style");
    assert.ok(await js("getComputedStyle(document.querySelector('#chat-messages .msg.assistant'), '::before').content.includes('kairos')"), "Assistant turns carry a prompt label");
    assert.deepEqual(await overflow(), [], "terminal style overflow");
    await capture("desktop-conversation-terminal");
    await js("import('/static/js/appearance.js').then((m) => m.updateAppearance({ chatStyle: 'standard' }))");
    assert.notEqual(await js("getComputedStyle(document.querySelector('#chat-messages .msg.user .msg-body')).backgroundColor"), 'rgba(0, 0, 0, 0)', "Standard style keeps the bubble");
    // -- Side-by-side chats (2026-09-25): open, drag and drop, swap, close.
    const mainId = await js("document.querySelector('.session-item.active')?.dataset.sessionId");
    const sideId = () => js("document.querySelector('.side-chat')?.dataset.sessionId || null");
    await js("import('/static/js/sideChat.js').then((m) => m.openSideChat('s2'))");
    await waitFor("document.querySelectorAll('.side-chat .side-messages .msg').length === 2");
    assert.ok(await js("document.querySelector('.chat-layout').classList.contains('has-side-chat')"));
    assert.equal(await js("document.querySelectorAll('#chat-messages .msg').length"), 2, "the main chat is untouched");
    assert.ok(await js("(() => { const a = document.querySelector('#chat-main').getBoundingClientRect(), b = document.querySelector('.side-chat').getBoundingClientRect(); return a.width >= 380 && b.width >= 380 && a.right <= b.left + 1; })()"), "two readable columns side by side");
    assert.deepEqual(await overflow(), [], "side by side overflow");
    await capture("desktop-side-by-side");
    await js(`(() => { const main = document.querySelector('#chat-main'), box = main.getBoundingClientRect(), dt = new DataTransfer();
      dt.setData('application/x-jarvis-session', 's3');
      const at = { dataTransfer: dt, clientX: box.right - 40, clientY: box.top + 200, bubbles: true, cancelable: true };
      main.dispatchEvent(new DragEvent('dragover', at)); window.__hintShown = document.querySelector('.side-chat-drop-hint').classList.contains('active');
      main.dispatchEvent(new DragEvent('drop', at)); })()`);
    assert.equal(await js("window.__hintShown"), true, "dragging a chat over the right half shows where it will land");
    await waitFor("document.querySelector('.side-chat')?.dataset.sessionId === 's3'");
    await js(`import('/static/js/sideChat.js').then((m) => m.openSideChat('${mainId}', { quiet: true }))`);
    await delay(150);
    assert.equal(await sideId(), 's3', "the chat already open in the main pane is not opened twice");
    await js("document.querySelector('.side-chat-promote').click()");
    await waitFor(`document.querySelector('.side-chat')?.dataset.sessionId === '${mainId}'`);
    assert.equal(await js("document.querySelector('.session-item.active')?.dataset.sessionId"), 's3', "Make main swaps the two chats");
    await js("document.querySelector('.side-chat-close').click()");
    assert.equal(await sideId(), null);
    assert.equal(await js("document.querySelector('.chat-layout').classList.contains('has-side-chat')"), false);
    assert.equal(await js("localStorage.getItem('jarvis:side-chat')"), null, "a closed side chat does not come back");
    // The side browser shares this right-hand region; capture its own toolbar.
    await js("import('/static/js/browserPane.js').then((m) => m.openBrowser('about:blank'))");
    await waitFor("!!document.querySelector('.browser-panel .browser-toolbar')");
    assert.deepEqual(await overflow(), [], "side browser chrome overflow");
    await capture("desktop-side-browser");
    await js("import('/static/js/browserPane.js').then((m) => m.closeBrowser())");
    // -- Find in chat (Ctrl+F, 2026-09-25).
    await js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'f', ctrlKey: true, bubbles: true, cancelable: true }))");
    assert.equal(await js("document.querySelector('.chat-find').hidden"), false, "Ctrl+F opens the find bar");
    assert.equal(await js("document.activeElement.classList.contains('chat-find-input')"), true);
    const expected = await js("[...document.querySelectorAll('#chat-messages .msg-body')].map((b) => b.textContent.toLowerCase().split('workspace').length - 1).reduce((a, b) => a + b, 0)");
    assert.ok(expected >= 2, "the fixture chat mentions the word more than once");
    const findKey = (extra = '') => js(`document.querySelector('.chat-find-input').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true ${extra} }))`);
    await js("document.querySelector('.chat-find-input').value = 'Workspace'");
    await findKey();
    assert.equal(await js("document.querySelector('.chat-find-count').textContent"), `${expected} of ${expected}`, "every match is found, case-insensitively, starting from the latest");
    assert.equal(await js("CSS.highlights.get('chat-find').size"), expected);
    await findKey();
    assert.equal(await js("document.querySelector('.chat-find-count').textContent"), `1 of ${expected}`, "Enter wraps around");
    await findKey(", shiftKey: true");
    assert.equal(await js("document.querySelector('.chat-find-count').textContent"), `${expected} of ${expected}`, "Shift+Enter goes back");
    assert.equal(await js("document.querySelectorAll('#chat-messages mark, #chat-messages .chat-find-hit').length"), 0, "the transcript's HTML is not rewritten");
    await capture("desktop-chat-find");
    await js("document.querySelector('.chat-find-input').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))");
    assert.equal(await js("document.querySelector('.chat-find').hidden"), true, "Esc closes it");
    assert.equal(await js("CSS.highlights.has('chat-find')"), false, "and clears the highlights");
    await js("document.querySelector('#chat-history-toggle').click()");
    await delay(300);
    assert.equal(await js("document.querySelector('#chat-sessions').inert"), false);
    assert.equal(await js("getComputedStyle(document.querySelector('.chat-history-close')).display"), 'none', 'Drawer close control stays mobile-only');
    await capture('desktop-chat-history');
    await js("document.querySelector('#chat-history-toggle').click()");
    await delay(300);
    assert.equal(writes.length, 0, "Navigation must not write data");
    await js("document.querySelector('#model-picker-btn').click()");
    await capture("desktop-model-menu");
    const menuFits = await js("(() => {const r=document.querySelector('#model-picker-menu').getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight})()");
    assert.ok(menuFits, "Model menu fits viewport");
    // Every popup shares one flat surface (no sheen), and the composer's
    // menus open fully above it rather than over its text box.
    const popupLook = "(m => { const s = getComputedStyle(m); const r = m.getBoundingClientRect(); const c = document.querySelector('.chat-input-bar').getBoundingClientRect(); return s.backgroundImage === 'none' && s.borderRadius === '14px' && r.bottom <= c.top; })";
    assert.ok(await js(popupLook + "(document.querySelector('#model-picker-menu'))"), "Model menu: shared popup look, above the composer");
    await js("document.body.click(); document.querySelector('#overflow-plus-btn').click()");
    assert.ok(await js(popupLook + "(document.querySelector('#overflow-menu'))"), "+ menu: shared popup look, above the composer");
    assert.ok(await js("[...document.querySelectorAll('.chat-input-bar button')].filter(b => !b.closest('#overflow-menu')).every(b => !b.classList.contains('chat-computer-toggle') && b.id !== 'chat-compact' && !['Capture screen', 'Computer', 'Compact'].includes(b.title) && !['Capture screen', 'Computer', 'Compact'].includes(b.textContent.trim()) && b.getAttribute('aria-label') !== 'Capture from this device')"), "Capture screen, Computer and Compact are absent from the composer bar outside the + menu");
    assert.ok(await js("[...document.querySelectorAll('#overflow-menu .overflow-menu-item')].some(b => b.textContent.trim() === 'Capture screen' && !b.hidden)"), "+ menu always contains Capture screen");
    assert.deepEqual(await js("[...document.querySelectorAll('#overflow-menu .overflow-menu-item')].map(b => b.textContent.trim())"), ['Attach files', 'Capture screen', 'Documents', 'Workspace', 'Browse the web', 'Computer', 'Integrations', 'Prompt', 'Compact'], "+ menu keeps the requested item order");
    assert.ok(await js("[...document.querySelectorAll('#overflow-menu .overflow-menu-item[hidden]')].every(b => getComputedStyle(b).display === 'none')"), "Hidden + menu items leave no gaps");
    await capture("desktop-plus-menu");
    await js("document.body.click()");
    // Native <select>s (the composer's Mode here) open the app's own menu,
    // above the composer, not the system's list; Escape closes it.
    await js("document.getElementById('chat-permission-mode').dispatchEvent(new MouseEvent('mousedown', { bubbles: true, button: 0 }))");
    assert.ok(await js("(() => { const m = document.querySelector('.custom-select-menu'); const c = document.querySelector('.chat-input-bar').getBoundingClientRect(); return !!m && m.querySelectorAll('.custom-select-item').length === document.getElementById('chat-permission-mode').options.length && m.querySelector('.custom-select-item.active') && m.getBoundingClientRect().bottom <= c.top; })()"), "Mode opens the app's menu above the composer");
    await capture("desktop-mode-menu");
    await js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))");
    assert.ok(await js("!document.querySelector('.custom-select-menu')"), "Escape closes the Mode menu");
    // The halftone background: the sky behind a chat with messages, the figure
    // behind a new one; Home to Chat runs a view transition (app.js).
    assert.equal(await js("document.querySelector('.chat-backdrop').dataset.scene"), "sky", "A chat with messages shows the halftone sky");
    await navigate("home");
    await delay(600);
    await js("window.__viewTransitions = 0; window.__startViewTransition = document.startViewTransition; document.startViewTransition = function (cb) { window.__viewTransitions++; return window.__startViewTransition.call(document, cb); }; true");
    await navigate("chat");
    await waitFor("document.querySelector('.chat-backdrop')?.dataset.scene === 'figure' && Number(document.querySelector('.chat-backdrop').dataset.drawMs) >= 0 && !document.documentElement.classList.contains('tab-transition')");
    assert.equal(await js("window.__viewTransitions"), 1, "Home to Chat runs a view transition");
    const backdropMs = Number(await js("document.querySelector('.chat-backdrop').dataset.drawMs"));
    console.log(`chat halftone draw: ${backdropMs} ms`);
    assert.ok(backdropMs < 400, "The chat halftone draws in under 400 ms (" + backdropMs + ")");
    await capture("desktop-chat-new-halftone");
    const forming = await js(`import('/static/js/chatContent.js').then(async (m) => {
      const text = 'Here:\\n\\n\\\`\\\`\\\`js\\nlet a = 1\\n\\\`\\\`\\\`\\n\\n| a | b |\\n|---|---|\\n| 1 | 2 |';
      const live = document.createElement('div');
      m.renderMessageBody(live, text, 's1', true, { live: true });
      const first = [...live.querySelectorAll('.chat-code-block, .chat-table-wrap')].map(n => n.classList.contains('is-forming'));
      await new Promise(r => setTimeout(r, 300));
      m.renderMessageBody(live, text + ' more', 's1', true, { live: true });
      const resumed = parseInt(live.querySelector('.chat-code-block').style.animationDelay, 10);
      await new Promise(r => setTimeout(r, 700));
      m.renderMessageBody(live, text + ' done', 's1', true, { live: false });
      const settled = !live.querySelector('.is-forming');
      const history = document.createElement('div');
      m.renderMessageBody(history, text, 's1', true);
      m.renderMessageBody(history, text, 's1', true, { live: true });
      return JSON.stringify({ first, resumed, settled, history: !history.querySelector('.is-forming') });
    })`);
    const formed = JSON.parse(forming);
    assert.deepEqual(formed.first, [true, true], "Code and tables form in mid-reply");
    assert.ok(formed.resumed <= -250, "A re-render resumes the forming animation (" + formed.resumed + ")");
    assert.ok(formed.settled, "Formed blocks settle");
    assert.ok(formed.history, "History never animates");
    await navigate("home");
    assert.equal(await js("window.__viewTransitions"), 2, "Chat to Home runs a view transition");
    await waitFor("document.querySelector('.dashboard-core')?.classList.contains('is-dithered')");
    // The halftone switch (appearance.js): two-tone in Color, transitions kept;
    // off means no chat halftone, no transition and a plain Home card; Image
    // and Shader don't offer it.
    const appearance = (patch) => js("import('/static/js/appearance.js').then(m => { m.updateAppearance(" + JSON.stringify(patch) + "); return document.documentElement.dataset.halftone; })");
    assert.equal(await appearance({ mode: "color", color: "#233447", halftone: true }), "on", "Color offers the halftone");
    await navigate("chat");
    assert.equal(await js("window.__viewTransitions"), 3, "Color keeps the Home to Chat transition");
    await waitFor("Number(document.querySelector('.chat-backdrop')?.dataset.drawMs) >= 0");
    assert.ok(await js("(() => { const c = document.querySelector('.chat-backdrop canvas:last-child'); const [r, g, b] = c.getContext('2d').getImageData(4, 4, 1, 1).data; return b > r + 12; })()"), "Color's halftone is drawn in the theme's colors (cool Ocean, not the painting's warm tones)");
    await capture("desktop-chat-halftone-color");
    assert.equal(await appearance({ halftone: false }), "off", "The halftone switch turns off");
    assert.equal(await js("getComputedStyle(document.querySelector('.chat-backdrop')).display"), "none", "No chat halftone when off");
    await navigate("home");
    assert.equal(await js("window.__viewTransitions"), 3, "No transition when off");
    assert.equal(await js("getComputedStyle(document.querySelector('.dashboard-core canvas') || document.body).display === 'none' && getComputedStyle(document.querySelector('.dashboard-core')).backgroundImage"), "none", "A plain Home card when off");
    assert.equal(await appearance({ mode: "shader", halftone: true }), "none", "Shader doesn't offer the halftone");
    assert.equal(await appearance({ mode: "default", halftone: true }), "on", "Back to Kairos");
    await waitFor("document.querySelector('.dashboard-core')?.classList.contains('is-dithered')");
    await navigate("chat");
    assert.equal(await js("getComputedStyle(document.querySelector('.border-beam'),'::before').animationName"), "border-orbit");
    await js("document.querySelector('.border-beam').dataset.active='false'");
    assert.equal(await js("getComputedStyle(document.querySelector('.border-beam'),'::before').animationPlayState"), "paused");
    await js("document.querySelector('.border-beam').dataset.active='true'");
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    assert.equal(await js("getComputedStyle(document.querySelector('.border-beam'),'::before').animationName"), "none");
    assert.ok(await js("parseFloat(getComputedStyle(document.querySelector('#sidebar')).transitionDuration) < .01"), "Reduced motion disables the rail transition");
    const transitionsBefore = await js("window.__viewTransitions");
    await navigate("home");
    await navigate("chat");
    assert.equal(await js("window.__viewTransitions"), transitionsBefore, "Reduced motion skips the Home and Chat transitions");
    await js("document.startViewTransition = window.__startViewTransition; true");
    // Home's "Your models" shows each subscription's limits as the overlay does
    // (quotaReadings.js), once per account, with signed-out and stale states.
    await navigate("home");
    await waitFor("document.querySelectorAll('.dashboard-quota').length === 2");
    assert.ok(await js("(() => { const c = document.querySelector('.dashboard-quota[data-provider=claude]'); return c.querySelectorAll('.dashboard-quota-window').length === 2 && c.textContent.includes('34% Used · 66% left') && c.textContent.includes('Resets') && !!c.querySelector('.tone-ample'); })()"), "Claude's limits show on Home");
    assert.ok(await js("!!document.querySelector('.dashboard-quota[data-provider=codex] .tone-crit')"), "A limit past 80% shows as critical");
    await capture("desktop-home-limits");
    demoState.quotas = { providers: [
      { provider: "claude", status: "needs_sign_in", updated_at: 0, note: "", windows: [] },
      { provider: "codex", status: "ok", updated_at: Math.floor(Date.now() / 1000) - 3600, note: "", windows: [{ name: "5-hour", used_percent: 40, resets_at: Math.floor(Date.now() / 1000) + 600 }] },
    ], recorded: [] };
    await navigate("home");
    await waitFor("document.querySelector('.dashboard-quota[data-provider=claude]')?.textContent.includes('Sign in to Claude Code')");
    assert.ok(await js("document.querySelector('.dashboard-quota[data-provider=codex]').textContent.includes('Updated 1h ago')"), "A stale reading says how old it is");
    delete demoState.quotas;

    // Layout (layout.js): Agents sits third by default; a moved and a hidden
    // tab, and Home's order, apply live and survive a reload; resets restore.
    const navOrder = () => js("[...document.querySelectorAll('#nav .nav-item[data-tab]')].map(n => n.dataset.tab)");
    assert.deepEqual((await navOrder()).slice(0, 3), ["home", "chat", "agents"], "Agents sits with Home and Chats");
    await js(`import('/static/js/layout.js').then(m => { const l = m.getLayout(); l.groups.main = ['home', 'notes', 'chat', 'agents']; l.groups.workspace = l.groups.workspace.filter(t => t !== 'notes'); l.hiddenTabs = ['cookbook']; l.home = ['models', 'stats', 'chats', 'schedule', 'projects', 'activity', 'system']; l.hiddenHome = ['projects']; m.saveLayout(l); })`);
    await waitFor("document.querySelectorAll('#nav .nav-item[data-tab]')[1]?.dataset.tab === 'notes'");
    assert.ok(await js("!document.querySelector('#nav .nav-item[data-tab=cookbook]') && document.querySelector('.dashboard-grid').firstElementChild === document.querySelector('.dashboard-grid .dashboard-section:has(.dashboard-quota)') && document.querySelectorAll('.dashboard-grid > .dashboard-section').length === 5"), "Layout changes apply live");
    await win.loadURL(base);
    await waitFor("document.querySelectorAll('#nav .nav-item[data-tab]').length >= 10");
    assert.deepEqual((await navOrder()).slice(0, 4), ["home", "notes", "chat", "agents"], "The sidebar layout survives a reload");
    await waitFor("document.querySelectorAll('.dashboard-grid > *').length === 6");
    assert.ok(await js("!document.querySelector('#nav .nav-item[data-tab=cookbook]') && document.querySelector('.dashboard-grid').firstElementChild.querySelector('.dashboard-section-header h2').textContent === 'Your models'"), "Home's layout survives a reload");
    // The Layout page lists every tab, with Home's switch locked on.
    await js("document.querySelector('.sidebar-settings-btn').click()");
    await waitFor("!!document.querySelector('.settings-nav-item[data-section=layout]')");
    await js("document.querySelector('.settings-nav-item[data-section=layout]').click()");
    await waitFor("document.querySelectorAll('.layout-item').length >= 17");
    assert.ok(await js("document.querySelector('.layout-item[data-id=home] .set-switch').disabled && document.querySelector('.layout-item[data-id=cookbook]').classList.contains('is-hidden')"), "The Layout page shows the layout, Home locked on");
    await capture("desktop-settings-layout");
    await js("document.querySelector('.layout-item[data-id=notes] .layout-move:nth-of-type(2)').click()");
    await waitFor("document.querySelectorAll('#nav .nav-item[data-tab]')[2]?.dataset.tab === 'notes'");
    await js("[...document.querySelectorAll('.set-section .btn')].filter(b => /^Reset (sidebar|Home)$/.test(b.textContent)).forEach(b => b.click())");
    await waitFor("[...document.querySelectorAll('#nav .nav-item[data-tab]')].slice(0, 3).map(n => n.dataset.tab).join() === 'home,chat,agents'");
    assert.ok(await js("!!document.querySelector('#nav .nav-item[data-tab=cookbook]')"), "Reset shows hidden tabs again");
    await js("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))");
    await navigate("home");
    assert.equal(await js("document.querySelector('.dashboard-grid').firstElementChild.className"), "dashboard-stats", "Reset restores Home");
    await navigate("home");
    await waitFor("document.querySelector('.dashboard-core')?.classList.contains('is-dithered')");
    await capture("desktop-reduced-motion");
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [] });
    await navigate("chat");
    assert.ok(await js("document.querySelector('#chat-main').classList.contains('is-empty') && !document.querySelector('.chat-welcome')"));
    assert.ok(await js("(() => {const r=document.querySelector('.chat-input-bar').getBoundingClientRect(); return Math.abs((r.top+r.bottom)/2-innerHeight/2)<60})()"), 'New-chat composer is centered');
    await js("window.originalComposer=document.querySelector('#chat-input'); originalComposer.value='A draft worth keeping'; document.querySelector('#chat-history-toggle').click()");
    await delay(300);
    assert.equal(await js("document.querySelector('#chat-history-toggle').getAttribute('aria-expanded')"), 'true');
    assert.equal(await js("document.querySelector('#chat-input')===window.originalComposer && originalComposer.value==='A draft worth keeping'"), true);
    await navigate('notes');
    await navigate('chat');
    assert.equal(await js("document.querySelector('#chat-history-toggle').getAttribute('aria-expanded')"), 'true', 'History preference survives remount');
    await js("document.querySelector('#chat-history-toggle').click(); document.querySelector('.chat-header-new').click()");
    assert.equal(writes.length, 0, 'New-chat landing and history navigation do not create sessions');
    win.setContentSize(390, 844);
    await delay(300);
    await js("document.querySelector('#chat-history-toggle').click()");
    assert.equal(await js("document.querySelector('#chat-sessions').inert"), false);
    await capture('mobile-chat-history');
    await js("document.querySelector('.chat-history-close').click()");
    assert.equal(await js("document.activeElement.id"), 'chat-history-toggle');
    assert.equal(await js("document.querySelector('#chat-sessions').inert"), true);
    win.setContentSize(1440, 900);
    await navigate("notes");
    await js("document.querySelector('#notes-list input[type=checkbox]').click()");
    await delay(100);
    assert.equal(writes.at(-1).method, "PATCH");
    assert.equal(JSON.parse(writes.at(-1).body).completed, true);
    sessionDelay = 300;
    await js("void import('/static/js/app.js').then(m => m.switchTab('chat'))");
    await waitFor("!!document.querySelector('.border-beam')");
    await navigate("notes");
    await delay(500);
    assert.equal(await js("document.querySelector('#view-content').dataset.view"), "notes");
    assert.equal(await js("document.querySelector('.view-header h2').textContent"), "Notes");
    sessionDelay = 0;
    await navigate("chat");
    assert.ok(await js("document.querySelector('#chat-main').classList.contains('is-empty')"));
    assert.equal(await js("document.querySelector('#brand .instance-badge')"), null, "the real app shows no instance badge");
    demoState.empty = true;
    // The empty pass plays a development copy: its name on every page.
    await win.loadURL(base);
    await waitFor("document.querySelector('#brand .instance-badge')?.textContent === 'DEV'");
    assert.equal(await js("document.title"), "Kairos (dev)");
    for (const tab of ["home", "chat", "notes", "library", "calendar", "tasks", "email", "tool-store", "agents", "cookbook", "school"]) {
      await navigate(tab);
      if (tab === "home") await waitFor("document.querySelector('.dashboard-stat-value')?.textContent === '0'");
      assert.deepEqual(await overflow(), [], "empty " + tab);
    }
    // The desktop title bar (style.css .titlebar), checked by switching on the
    // desktop shell's class: it spans the window, names the tab, sits above
    // Settings' dimming, and the colour sent for the window controls is its own.
    win.setContentSize(1440, 900);
    await js("document.documentElement.classList.add('electron-shell')");
    await navigate("chat");
    assert.ok(await js("(() => { const b = document.getElementById('titlebar').getBoundingClientRect(); return b.top === 0 && b.height === 36 && b.width === innerWidth; })()"), "Title bar spans the window");
    assert.equal(await js("document.getElementById('titlebar-tab').textContent"), "Chats", "Title bar names the tab");
    assert.ok(await js("!!document.querySelector('#titlebar-brand .brand-wordmark')"), "Title bar carries the wordmark");
    assert.ok(await js("document.getElementById('app').getBoundingClientRect().top === 36"), "The app starts below the bar");
    assert.ok(await js("Math.abs(document.querySelector('.project-picker-wrap').getBoundingClientRect().bottom - document.querySelector('.chat-mobile-header').getBoundingClientRect().bottom) < 1.5 || getComputedStyle(document.getElementById('chat-sessions')).width === '0px'"), "Chat history and conversation headers line up");
    await capture("desktop-titlebar-chat");
    await js("document.querySelector('.sidebar-settings-btn').click()");
    await waitFor("!!document.querySelector('.modal-backdrop:not(.hidden)')");
    assert.ok(await js("document.elementFromPoint(innerWidth - 60, 12).closest('#titlebar') !== null"), "Settings' dimming passes under the title bar");
    assert.ok(await js("(() => { const p = document.createElement('span'); document.body.append(p); p.style.color = 'var(--sidebar-bg)'; const v = getComputedStyle(p).color; p.remove(); return getComputedStyle(document.getElementById('titlebar')).backgroundColor === v; })()"), "The bar is the colour sent for the window controls");
    await capture("desktop-titlebar-settings");
    await js("document.querySelector('.settings-titlebar-btn[title=Close]').click()");
    await js("document.documentElement.classList.remove('electron-shell')");
    // Agent mentions use one renderer in the main, side and embedded chats.
    // Each action waits for its own request and each status for its own DOM.
    // They need the sample agents, so they run outside the empty pass, which
    // is restored for the checks after them.
    // Each brief validates required answers and uses the same one-shot chat
    // handoff at both viewport sizes. Synthetic writes are kept only here.
    const emptyBeforeBuilders = demoState.empty;
    demoState.empty = false;
    for (const [label, width, height] of [['desktop', 1440, 900], ['mobile', 390, 844]]) {
      win.setContentSize(width, height);
      for (const [kind, category, skill] of [['skill', 'Skills', 'build-skill'], ['mcp', 'Tools', 'build-mcp-server'], ['automation', 'Automations', 'build-automation'], ['automation', null, 'build-automation']]) {
        await navigate(category ? 'tool-store' : 'tasks');
        if (category) await js(`[...document.querySelectorAll('.tool-store-filters button')].find(b => b.textContent === ${JSON.stringify(category)}).click()`);
        const scope = `.modal-panel [data-builder="${kind}"]`;
        const buttonKind = kind === 'skill' ? 'skills' : kind === 'mcp' ? 'tools' : category ? 'automations' : 'automation';
        const opener = `document.querySelector('[data-build-kind="${buttonKind}"]')`;
        const ready = `!!document.querySelector('${scope} .tab-build-build-btn')`;
        await popupDismissals(opener, ready, label + ' ' + kind + ' brief');
        await openPopup(opener, ready, label + ' ' + kind + ' brief');
        const start = writes.length;
        await js(`document.querySelector('${scope} .tab-build-build-btn').click()`);
        await waitFor(`!document.querySelector('${scope} .tab-build-error[role="status"]').classList.contains('hidden')`);
        assert.equal(writes.length, start, label + ' ' + kind + ' empty brief creates no session');
        await js(`document.querySelectorAll('${scope} textarea, ${scope} input').forEach((input, index) => { input.value = 'Builder ${kind} ${label} answer ' + index; })`);
        assert.deepEqual(await overflow(), [], label + ' ' + kind + ' brief fits');
        await capture(label + '-builder-' + kind + (category === 'Automations' ? '-store' : ''));
        await js(`document.querySelector('${scope} .tab-build-build-btn').click()`);
        await waitFor("document.querySelector('.nav-item[data-tab=chat]').classList.contains('active') && sessionStorage.getItem('jarvis:pendingChatHandoff') === null");
        assert.equal(await js("document.querySelector('.panel-dialog')"), null, label + ' build handoff closes its popup');
        const marker = `Builder ${kind} ${label} answer`;
        for (let i = 0; i < 80 && !writes.slice(start).some(w => w.path === '/api/chat/stream' && w.body.includes(marker)); i++) await delay(50);
        const handoff = writes.slice(start).find(w => w.path === '/api/chat/stream' && w.body.includes(marker));
        assert.ok(handoff && JSON.parse(handoff.body).message.startsWith(`First call read_skill with slug "${skill}"`), label + ' ' + kind + ' reads its skill first');
        assert.ok(writes.slice(start).some(w => /\/api\/sessions\/[^/]+\/model$/.test(w.path)), label + ' builder sets the selected model');
        writes.splice(start);
      }
    }
    demoState.empty = emptyBeforeBuilders;
    const emptyBeforeMentions = demoState.empty;
    demoState.empty = false;
    const waitForWrite = async (start, path, matches) => {
      for (let i = 0; i < 80; i++) {
        const request = writes.slice(start).find(w => w.path === path && matches(JSON.parse(w.body || '{}')));
        if (request) return request;
        await delay(50);
      }
      throw new Error('Timed out waiting for ' + path);
    };
    const mentionAgent = async (scope, inputSelector, sendSelector, id, work) => {
      demoState.handoffStatus = 'queued'; demoState.handoffAnswered = false;
      await js(`(() => { const input = document.querySelector(${JSON.stringify(inputSelector)}); input.focus(); input.value = '@Scout'; input.dispatchEvent(new Event('input')); })()`);
      await waitFor(`!!document.querySelector(${JSON.stringify(scope + ' .chat-reference-option')})`);
      assert.ok(await js(`document.querySelector(${JSON.stringify(scope + ' .chat-reference-option')}).textContent.includes('Agent')`), 'Agent appears in @ search');
      await js(`document.querySelector(${JSON.stringify(scope + ' .chat-reference-option')}).click()`);
      assert.ok(await js(`!!document.querySelector(${JSON.stringify(scope + ' .chat-reference-chip .handoff-avatar')})`), 'Agent chip carries its avatar');
      const start = writes.length;
      await js(`(() => { const input = document.querySelector(${JSON.stringify(inputSelector)}); input.value = ${JSON.stringify(work)}; input.dispatchEvent(new Event('input')); document.querySelector(${JSON.stringify(sendSelector)}).click(); })()`);
      await waitForWrite(start, '/api/chat/stream', body => body.session_id === id && body.message.includes(work) && body.references?.some(ref => ref.kind === 'agent' && ref.id === 'a1'));
      await waitFor(`!!document.querySelector(${JSON.stringify(scope + ' .agent-handoff[data-status="queued"]')})`);
      demoState.handoffStatus = 'working';
      await waitFor(`!!document.querySelector(${JSON.stringify(scope + ' .agent-handoff[data-status="working"]')})`);
    };
    const expectAgentReply = async scope => {
      demoState.handoffStatus = 'done';
      await waitFor(`!!document.querySelector(${JSON.stringify(scope + ' .agent-handoff[data-status="done"]')})`);
      await waitFor(`[...document.querySelectorAll(${JSON.stringify(scope + ' .agent-message .msg-body')})].some(body => body.textContent.includes('Two remote roles found'))`);
      assert.ok(await js(`!!document.querySelector(${JSON.stringify(scope + ' .agent-message-heading .handoff-avatar')})`), 'Returned reply has agent identity');
      assert.ok(await js(`!document.querySelector(${JSON.stringify(scope)}).textContent.includes('No text response was returned')`), 'Handoff has no chat-model placeholder');
      assert.deepEqual(await overflow(), [], 'Handoff fits the chat');
    };
    for (const [label, width, height] of [['desktop', 1440, 900], ['mobile', 390, 844]]) {
      win.setContentSize(width, height);
      const id = 'handoff-' + label;
      await navigate('chat', { sessionId: id });
      await mentionAgent('#chat-main', '#chat-input', '#chat-send', id, 'Find remote roles ' + label);
      await capture(label + '-agent-handoff-working');
      demoState.handoffStatus = 'needs_you';
      await waitFor("!!document.querySelector('#chat-main .agent-handoff-answer')");
      await capture(label + '-agent-handoff-question');
      const questionId = await js("document.querySelector('#chat-main .agent-handoff-answer').closest('.agent-message').dataset.handoffKey");
      const answerStart = writes.length;
      await js("(() => { const form = document.querySelector('#chat-main .agent-handoff-answer'); form.querySelector('textarea').value = 'Europe'; form.querySelector('button').click(); })()");
      await waitForWrite(answerStart, '/api/agents/inbox/' + questionId + '/answer', body => body.text === 'Europe' && body.choice === 'reply');
      await waitFor("document.querySelector('#chat-main .agent-message')?.textContent.includes('Handed to Scout') && !document.querySelector('#chat-main .agent-handoff-answer')");
      await expectAgentReply('#chat-main');
      await capture(label + '-agent-handoff-reply');
      if (label === 'desktop') {
        await js("import('/static/js/sideChat.js').then(m => m.openSideChat('handoff-side'))");
        await waitFor("document.querySelector('.side-chat')?.dataset.sessionId === 'handoff-side'");
        await mentionAgent('.side-chat', '.side-chat .side-chat-input', '.side-chat .side-chat-send', 'handoff-side', 'Find roles from side chat');
        await expectAgentReply('.side-chat');
        await capture('desktop-side-agent-handoff-reply');
        await js("document.querySelector('.side-chat-close').click()");
      }
      delete demoState.handoffSessions.as1;
      await navigate('agents', { agentId: 'a1' });
      await waitFor("!!document.querySelector('.agent-chat-main .side-chat-input')");
      await mentionAgent('.agent-chat-main', '.agent-chat-main .side-chat-input', '.agent-chat-main .side-chat-send', 'as1', 'Find roles from agent chat ' + label);
      await expectAgentReply('.agent-chat-main');
      await capture(label + '-agent-chat-handoff-reply');
    }
    demoState.handoffSessions = {}; delete demoState.handoffStatus; delete demoState.handoffAnswered;
    demoState.empty = emptyBeforeMentions;
    win.setContentSize(1440, 900);
    unavailable = true;
    await navigate("home");
    await waitFor("document.querySelector('.dashboard-system-pill')?.textContent === 'Status unavailable'");
    await capture("desktop-status-unavailable");
    assert.deepEqual(errors, [], "Renderer errors");
    // Explicit opt-in only: these are actual renderer captures with synthetic
    // data. Neither the live backend nor personal screenshots are a source.
    if (updateChatImage && !updateDocImages) fs.copyFileSync(path.join(output, 'desktop-conversation.png'), path.join(root, 'docs', 'img', 'chat.png'));
    if (updateDocImages) {
      const mapping = { home: "desktop-home", chat: "desktop-conversation", "chat-new": "desktop-chat-minimal", tasks: "desktop-tasks", vault: "desktop-vault", calendar: "desktop-calendar", notes: "desktop-notes", settings: "desktop-settings", library: "desktop-library", "sidebar-collapsed": "desktop-sidebar-collapsed" };
      for (const [name, source] of Object.entries(mapping)) fs.copyFileSync(path.join(output, source + ".png"), path.join(root, "docs", "img", name + ".png"));
    }
    for (const [label, width, height] of [["desktop",1440,900],["mobile",390,844]]) {
      win.setContentSize(width, height);
      await win.loadURL(base + "/docs/");
      await js("Promise.all([...document.images].map(image => { image.loading = 'eager'; return image.decode(); }))");
      await js("window.scrollTo({top:0,behavior:'instant'})");
      // The hero halftone draws live (static/js/dither.js, through the demo's copy); its pre-rendered fallback is styled underneath.
      await waitFor("document.querySelector('.hero-art').classList.contains('is-dithered') && !!document.querySelector('.hero-art canvas')");
      assert.ok(await js("(() => { const d = document.createElement('div'); d.className = 'hero-art'; document.body.append(d); const v = getComputedStyle(d).backgroundImage; d.remove(); return v.includes('hero-dither.webp'); })()"), label + " halftone fallback styled");
      assert.ok(await js("fetch('img/hero-dither.webp').then(r => r.ok)"), label + " halftone fallback exists");
      await waitFor("!document.querySelector('.hero-art .dither-cover')");
      // Sections flow in with the scroll (scroll-driven animations, CSS only):
      // below the window a card is still faint, centred it is settled.
      assert.ok(await js("(() => { const f = getComputedStyle(document.querySelector('.feature')); return f.animationName === 'flow-in' && String(f.animationTimeline).includes('view'); })()"), label + " sections flow in with the scroll");
      assert.ok(await js("Number(getComputedStyle(document.querySelector('.download-card')).opacity) < .5"), label + " a section below the window is still faint");
      await js("document.querySelector('.feature').scrollIntoView({ block: 'center', behavior: 'instant' })");
      await waitFor("Number(getComputedStyle(document.querySelector('.feature')).opacity) > .97");
      assert.ok(await js("getComputedStyle(document.querySelector('.faq details'), '::details-content').transitionProperty.includes('block-size')"), label + " FAQ answers open smoothly");
      await js("window.scrollTo(0, 0)");
      const drawMs = Number(await js("document.querySelector('.hero-art').dataset.drawMs"));
      console.log(`${label} halftone draw: ${drawMs} ms`);
      assert.ok(drawMs < 400, label + " halftone draws in under 400 ms (" + drawMs + ")");
      // The latest release's files and version reach the download buttons.
      await waitFor("document.getElementById('release-line').textContent.includes('Version 2.0.0')");
      assert.ok(await js("document.querySelector('[data-asset=\\'.exe\\']').href.endsWith('Kairos-Setup-2.0.0.exe')"), label + " Windows asset link");
      assert.ok(await js("document.querySelector('[data-asset=\\'.dmg\\']').href.endsWith('Kairos-2.0.0-arm64.dmg')"), label + " macOS asset link");
      assert.ok(await js("document.getElementById('primary-download').textContent.startsWith('Download for Windows') && document.getElementById('primary-download').href.endsWith('.exe')"), label + " primary download matches this system");
      assert.ok(await js("document.querySelector('.download-card[data-platform=windows]').classList.contains('recommended')"), label + " this system's card is marked");
      assert.ok(await js("document.documentElement.scrollWidth <= innerWidth"), label + " website fits");
      const anchors = await js("[...document.querySelectorAll('a[href^=\"#\"]')].every(a => document.querySelector(a.getAttribute('href')))");
      assert.ok(anchors, "Website anchors resolve");
      await capture(label + "-website");
      // The live demo (docs/demo): the real interface on sample data, answered in the page by demo/shim.js.
      const inDemo = (code) => js("(() => { const d = document.getElementById('demo-frame').contentDocument; return " + code + "; })()");
      const waitDemo = async (code, what) => {
        for (let i = 0; i < 200; i++) { try { if (await inDemo(code)) return; } catch (_) { /* the frame is between pages */ } await delay(100); }
        const seen = await js("(() => { const f = document.getElementById('demo-frame'); const d = f.contentDocument; return JSON.stringify({ src: f.src, url: d && d.location.href, ready: d && d.readyState, nav: d && d.querySelectorAll('.nav-item').length, body: d && d.body && d.body.innerHTML.slice(0, 200) }); })()").catch((e) => String(e));
        throw new Error("Demo timed out: " + what + " " + seen);
      };
      await js("document.getElementById('demo').scrollIntoView({block:'center',behavior:'instant'})");
      // The frame loads lazily, and this window is never shown on screen; load it now.
      await js("document.getElementById('demo-frame').loading = 'eager'");
      await waitDemo("d && d.querySelectorAll('.nav-item[data-tab]').length >= 10 && !!d.querySelector('.dashboard-core')", "Home loads in the demo");
      assert.ok(await inDemo("!!d.querySelector('.demo-badge')"), label + " the demo says it is a demo");
      for (const tab of ["chat", "notes", "library", "calendar", "email", "tasks", "tool-store", "agents", "cookbook", "school", "home"]) {
        await inDemo("d.querySelector('.nav-item[data-tab=" + tab + "]').click()");
        await waitDemo("d.getElementById('view-content')?.dataset.view === '" + tab + "' && !d.querySelector('#view-content .empty-state[role=status]')", tab + " opens in the demo");
        assert.ok(await inDemo("!d.getElementById('view-content').textContent.includes(\"couldn't load\")"), label + " demo " + tab + " renders");
      }
      await inDemo("d.querySelector('.nav-item[data-tab=chat]').click()");
      await waitDemo("!!d.getElementById('chat-input')", "the demo composer");
      await inDemo("(() => { const i = d.getElementById('chat-input'); i.value = 'What can you do?'; i.dispatchEvent(new Event('input', { bubbles: true })); d.getElementById('chat-send').click(); return true; })()");
      await waitDemo("[...d.querySelectorAll('.msg.assistant')].some(m => m.textContent.includes('Demo reply') && m.textContent.includes('download Kairos'))", "the demo's scripted reply");
      await waitDemo("d.querySelector('.chat-backdrop').dataset.scene === 'sky' && !d.querySelector('.chat-backdrop').classList.contains('is-dissolving')", "the background turns to the sky after the first message");
      await capture(label + "-website-demo");
      await inDemo("d.querySelector('#forge-mode-switch [data-mode=forge]').click()");
      await waitDemo("!!d.querySelector('.forge-home .forge-composer')", 'Forge Home opens in the demo');
      await waitDemo("!!d.querySelector('[data-forge-project=fp1]')", 'Forge projects load in the demo sidebar');
      await inDemo("d.querySelector('[data-forge-project=fp1]').click()");
      await waitDemo("!!d.querySelector('.forge-session-card[data-session-id=fs1]')", 'Forge sessions load in the demo');
      await inDemo("d.querySelector('.forge-session-card[data-session-id=fs1]').click()");
      await waitDemo("!!d.querySelector('.forge-shell .forge-transcript-layout')", 'Forge session renders in the bundle');
      assert.ok(await inDemo("d.querySelector('.forge-session-header').textContent.includes('Kairos garden')"), label + ' demo Forge header');
      await inDemo("d.querySelector('#forge-mode-switch [data-mode=kairos]').click()");
      await waitDemo("!!d.querySelector('.dashboard-core')", 'demo returns to Kairos');
      if (label === "mobile") {
        assert.ok(await js("getComputedStyle(document.querySelector('.menu-button')).display !== 'none' && getComputedStyle(document.querySelector('.nav-links')).display === 'none'"), "phone menu starts closed");
        await js("document.querySelector('.menu-button').click()");
        assert.ok(await js("document.querySelector('.site-nav').classList.contains('open') && getComputedStyle(document.querySelector('.nav-links')).display === 'flex' && document.querySelector('.menu-button').getAttribute('aria-expanded') === 'true'"), "phone menu opens");
        await capture(label + "-website-menu");
        await js("document.querySelector('.nav-links a[href=\\'#faq\\']').click()");
        assert.ok(await js("!document.querySelector('.site-nav').classList.contains('open')"), "choosing a link closes the phone menu");
      }
      await js("document.querySelector('#faq details').open = true");
      assert.ok(await js("document.querySelector('#faq details').open && document.querySelector('#faq details p').offsetHeight > 0"), label + " FAQ opens");
      for (const section of ["idea","features","everything","tour","principles","download","faq","closing"]) {
        await js("document.querySelector('#" + section + "').scrollIntoView({behavior:'instant'})");
        await delay(100);
        await capture(label + "-website-" + section);
      }
      await js("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})");
      await delay(100);
      await capture(label + "-website-footer");
      await js("document.querySelector('.install-note').open = true");
      assert.ok(await js("document.documentElement.scrollWidth <= innerWidth"));
      assert.ok(await js("[...document.images].every(i => i.complete && i.naturalWidth > 0)"), "Every website image loads");
      // Each feature image is shown whole: drawn in its file's own shape (so
      // nothing is zoomed or cropped) and entirely inside its card.
      const featureImages = await js(`[...document.querySelectorAll('.feature img')].map(i => { const r = i.getBoundingClientRect(), c = i.closest('.feature').getBoundingClientRect();
        return { src: i.getAttribute('src'), drawn: r.width / r.height, file: i.naturalWidth / i.naturalHeight, inside: r.left >= c.left && r.right <= c.right && r.bottom <= c.bottom }; })`);
      assert.equal(featureImages.length, 6, label + " six feature images");
      for (const image of featureImages) {
        assert.ok(Math.abs(image.drawn - image.file) < 0.02, `${label} ${image.src} is drawn whole (drawn ${image.drawn.toFixed(2)}, file ${image.file.toFixed(2)})`);
        assert.ok(image.inside, `${label} ${image.src} sits inside its card`);
      }
    }
    await win.loadURL(base + "/kairos/404.html");
    await waitFor("document.querySelector('.not-found h1')?.textContent === 'Not the right time.'");
    assert.ok(await js("[...document.images].every(i => i.complete && i.naturalWidth > 0) && getComputedStyle(document.body).fontFamily.includes('Jost')"), "404 page styled");
    await capture("website-404");
    assert.deepEqual([...new Set(outside.filter(u => !u.startsWith("https://api.github.com/repos/david-darr/kairos/releases/latest")))], [], "No outside requests but GitHub's release data");
    assert.deepEqual(errors, [], "Website renderer errors");
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    assert.equal(await js("getComputedStyle(document.documentElement).scrollBehavior"), "auto");
    assert.equal(await js("getComputedStyle(document.querySelector('.button')).transitionDuration"), "0s");
    // The demo also runs straight from disk (someone opening docs/ locally),
    // where browsers allow no service worker or module scripts. From disk the
    // browser refuses the fonts and logo masks; that noise isn't kept.
    const errorsBefore = errors.length;
    await win.loadURL(docsOnDisk + "demo/index.html");
    await waitFor("document.querySelectorAll('.nav-item[data-tab]').length >= 10 && !!document.querySelector('.dashboard-core')");
    assert.deepEqual(await js("[...document.querySelectorAll('.nav-item[data-tab]')].filter(n=>['school','crm'].includes(n.dataset.tab)).map(n=>n.dataset.tab)"), ["school"], "Demo starts with School only");
    await js("document.querySelector('.nav-item[data-tab=school]').click()");
    await waitFor("document.getElementById('view-content')?.dataset.view === 'school' && document.getElementById('school-body')?.textContent.includes('Software Design')");
    assert.ok(await js("!document.getElementById('view-content').textContent.includes(\"couldn't load\")"), "School loads from the bundle on file://");
    await js("document.querySelector('.nav-item[data-tab=chat]').click()");
    await waitFor("!!document.getElementById('chat-input')");
    await js("(() => { const i = document.getElementById('chat-input'); i.value = 'Hello'; i.dispatchEvent(new Event('input', { bubbles: true })); document.getElementById('chat-send').click(); return true; })()");
    for (let i = 0; i < 100 && !(await js("[...document.querySelectorAll('.msg.assistant')].some(m => m.textContent.includes('Demo reply'))")); i++) await delay(100);
    assert.ok(await js("[...document.querySelectorAll('.msg.assistant')].some(m => m.textContent.includes('Demo reply'))"), "The demo runs from disk");
    errors.length = errorsBefore;
    console.log("PASS: app desktop/mobile and icon rail, Forge sessions/composer/review, persistence/keyboard/tooltips, reduced motion, vault/chat/Settings, empty/error states, website layouts/links/images/previews.");
    console.log("Screenshots: " + output);
    fs.writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: true, checks: ["artifact split/resize/persistence, maximize/Escape, retained tabs/renderers and overlays", "10 tabs desktop/mobile", "52px icon rail layout and animation", "sidebar persistence, keyboard, tooltips, mobile override", "vault search/read", "Settings and usable mobile forms", "Home chat link and model menu", "beam/core reduced motion", "centered new-chat composer", "independent history persistence, draft retention and mobile focus", "new-chat landing does not write data", "mocked note completion", "delayed navigation", "10 empty views and unavailable status", "website desktop/mobile layouts, anchors, images, five preview states, live halftone and fallback, release-driven downloads, phone menu, FAQ, 404 and reduced motion", "site demo from disk"], docImagesUpdated: updateDocImages, errors, writes }, null, 2));
  } catch (error) {
    await capture("failure").catch(() => {});
    fs.writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: false, failure: error.stack, actual: error.actual, expected: error.expected, errors }, null, 2));
    console.error(error.stack); process.exitCode = 1;
  }
  finally { win.destroy(); server.close(); exitAfterFlush(process.exitCode); }
});
