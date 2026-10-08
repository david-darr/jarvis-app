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
const now = Date.now() / 1000;
const { fixture } = require("../demo/fixtures.js")({ now, state: demoState });
const errors = [];
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
  // The site (docs/) may read GitHub's public release data; nothing else leaves.
  const site = url.pathname.startsWith("/docs/") || url.pathname.startsWith("/kairos/");
  res.setHeader('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-src 'self' https:" + (site ? "; connect-src 'self' https://api.github.com" : ""));
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
    if (url.pathname === "/api/sessions" && sessionDelay) await delay(sessionDelay);
    res.setHeader("Content-Type", "application/json");
    if (unavailable && url.pathname === "/api/system/status") { res.writeHead(503); res.end('{"detail":"Unavailable"}'); return; }
    if (req.method !== "GET") {
      let body = ""; for await (const chunk of req) body += chunk;
      writes.push({ path: url.pathname, method: req.method, body });
      res.end('{"ok":true}'); return;
    }
    try { res.end(JSON.stringify(fixture(url))); }
    catch (error) { errors.push(error.message); res.writeHead(404); res.end('{"detail":"Missing fixture"}'); }
    return;
  }
  // /kairos/ is the site's address on GitHub Pages (404.html uses it).
  const pathname = url.pathname.startsWith("/kairos/") ? "/docs/" + url.pathname.slice("/kairos/".length) : url.pathname;
  // A folder serves its index.html, as GitHub Pages does (docs/demo/).
  const file = path.resolve(root, pathname === "/" ? "static/index.html" : "." + decodeURIComponent(pathname) + (pathname.endsWith("/") ? "index.html" : ""));
  if (!["static", "docs"].some(dir => file.startsWith(path.join(root, dir) + path.sep))) { res.writeHead(404); res.end(); return; }
  try {
    const mime = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".png": "image/png", ".svg": "image/svg+xml",
      ".webp": "image/webp", ".jpg": "image/jpeg", ".woff2": "font/woff2" };
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
  const overflow = async () => js(`Array.from(document.querySelectorAll('#view-content, #view-content .view-constrained, .chat-input-bar, .settings-content, .cal-left')).filter(e => e.clientWidth > 0 && e.scrollWidth > e.clientWidth + 2).map(e => ({class: e.className, width:e.clientWidth, scroll:e.scrollWidth}))`);
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
    await js("(async () => { const overlay=document.getElementById('onboarding-overlay'); overlay.classList.remove('hidden'); await import('/static/js/onboarding.js').then(m => m.run(overlay, () => {})); })()");
    assert.equal(await js("document.querySelector('.onboarding-card h2').textContent"), 'Not more time. The right time.');
    await capture('desktop-onboarding');
    win.setContentSize(390, 844); await delay(100);
    await capture('mobile-onboarding');
    win.setContentSize(1440, 900); await delay(350);
    await js("document.getElementById('onboarding-overlay').classList.add('hidden')");
    const railWidth = () => js("document.querySelector('#sidebar').getBoundingClientRect().width");
    assert.equal(await railWidth(), 204);
    await js("document.querySelector('#sidebar-toggle').click()");
    await delay(80);
    const movingWidth = await railWidth();
    assert.ok(movingWidth > 52 && movingWidth < 204, "Sidebar animates between widths");
    await delay(300);
    assert.equal(await railWidth(), 52);
    assert.ok(await js("[...document.querySelectorAll('#sidebar .nav-item svg')].every(e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=52})"), 'Icons fit the slim rail');
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
    await delay(350);
    assert.equal(await railWidth(), 204, "Keyboard expands sidebar");
    await js("document.activeElement.blur()");
    for (const [label, width, height] of [["desktop", 1440, 900], ["mobile", 390, 844]]) {
      win.setContentSize(width, height);
      await delay(100);
      for (const tab of ["home", "chat", "notes", "library", "calendar", "tasks", "email", "tool-store", "agents", "cookbook", "school"]) {
        await navigate(tab);
        if (tab === "chat") {
          // A new chat lands on the halftone figure (static/js/chatBackdrop.js).
          await waitFor("document.querySelector('.chat-backdrop')?.dataset.scene === 'figure' && document.querySelector('.chat-backdrop').dataset.drawMs !== undefined");
          assert.ok(await js("document.querySelector('.chat-backdrop').clientWidth > 0"), label + " default Chat draws the halftone background");
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
          await waitFor("document.querySelectorAll('.agent-chat-messages .msg').length > 0");
          assert.ok(await js("document.querySelector('.agent-chat-item.active')?.textContent.includes('Chat with Scout')"), label + " last chat open");
          assert.ok(await js("!!document.querySelector('.agent-chat-main .side-chat-input')"), label + " composer");
          assert.ok(await js("document.querySelector('.agent-work-host').hidden"), label + " work hidden on chat tab");
          assert.deepEqual(await overflow(), [], label + " overflow on agent chat");
          await capture(label + "-agent-chat");
          await js("[...document.querySelectorAll('.agent-tab')][1].click()");
          await waitFor("!document.querySelector('.agent-work-host').hidden");
          const titles = await js("[...document.querySelectorAll('.agent-work-host > .glass > .title')].map(n => n.textContent)");
          assert.deepEqual(titles, ["Inbox", "Standing goals", "Work", "Memory", "History", "Teams", "Triggers"], label + " agent work sections");
          assert.ok(await js("document.querySelector('.agent-triggers-panel').textContent.includes('GitHub pushes · asks you first')"), label + " the triggers that start this agent's work");
          assert.ok(await js("document.querySelector('.agent-teams-panel').textContent.includes('Launch team · teammate · Working')"), label + " the agent's teams");
          assert.ok(await js("document.querySelector('.agent-memory').value.includes('remote roles only')"), label + " memory shown");
          assert.ok(await js("document.querySelector('.agents-view').textContent.includes('Every day at 08:00')"), label + " goal cadence");
          assert.deepEqual(await overflow(), [], label + " overflow on agent page");
          await capture(label + "-agent-page");
          await navigate("agents");
          await waitFor("document.querySelectorAll('.agent-tile:not(.team-tile)').length === 2");
        }
        if (tab === "tool-store" && !demoState.empty) {
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
      // Custom Tabs stays behind Developer Mode, which this fixture reports
      // as off — the regrouping must not have loosened that gate.
      assert.ok(!(await js("[...document.querySelectorAll('.settings-nav-item')].some(i=>i.dataset.section==='custom-tabs')")), "Custom Tabs stays dev-mode gated");
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
      await js("[...document.querySelectorAll('.hook-form .btn')].find(b => b.textContent === 'Add hook').click()");
      await waitFor("!!document.querySelector('.confirm-panel')");
      assert.ok(await js("document.querySelector('.confirm-panel').textContent.includes(\"$d -match 'Remove-Item\")"), label + " the exact command is shown before it is saved");
      await capture(label + "-hook-confirm");
      await js("[...document.querySelectorAll('.confirm-panel .btn')].find(b => b.textContent === 'Cancel').click()");
      await waitFor("!document.querySelector('.confirm-panel')");
      assert.equal(writes.length, 0, label + " cancelling saves nothing");
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
      // Every page opens in the same frame (redesign 2026-10-05): its own
      // title in the header, real content under it, nothing wider than the
      // pane. A page that throws leaves the header without content.
      for (const id of await js("[...document.querySelectorAll('.settings-nav-item')].map(i => i.dataset.section)")) {
        await js(`document.querySelector('[data-section="${id}"]').click()`);
        await waitFor(`document.querySelector('.set-page')?.dataset.page === '${id}' && document.querySelector('#settings-content .set-title')?.textContent === document.querySelector('[data-section="${id}"]').textContent
          && !!document.querySelector('#settings-content .set-body').querySelector('.set-row, .set-empty, .appearance-panel, .log-entry, .logs-status, .sandbox-change, .run-item')`);
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
    await js("document.querySelector('.dashboard-row').click()");
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
      for (const tab of ["chat", "notes", "library", "calendar", "email", "tasks", "tool-store", "agents", "cookbook", "home"]) {
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
    await js("document.querySelector('.nav-item[data-tab=chat]').click()");
    await waitFor("!!document.getElementById('chat-input')");
    await js("(() => { const i = document.getElementById('chat-input'); i.value = 'Hello'; i.dispatchEvent(new Event('input', { bubbles: true })); document.getElementById('chat-send').click(); return true; })()");
    for (let i = 0; i < 100 && !(await js("[...document.querySelectorAll('.msg.assistant')].some(m => m.textContent.includes('Demo reply'))")); i++) await delay(100);
    assert.ok(await js("[...document.querySelectorAll('.msg.assistant')].some(m => m.textContent.includes('Demo reply'))"), "The demo runs from disk");
    errors.length = errorsBefore;
    console.log("PASS: app desktop/mobile and icon rail, persistence/keyboard/tooltips, reduced motion, vault/chat/Settings, empty/error states, website layouts/links/images/previews.");
    console.log("Screenshots: " + output);
    fs.writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: true, checks: ["10 tabs desktop/mobile", "52px icon rail layout and animation", "sidebar persistence, keyboard, tooltips, mobile override", "vault search/read", "Settings and usable mobile forms", "Home chat link and model menu", "beam/core reduced motion", "centered new-chat composer", "independent history persistence, draft retention and mobile focus", "new-chat landing does not write data", "mocked note completion", "delayed navigation", "10 empty views and unavailable status", "website desktop/mobile layouts, anchors, images, five preview states, live halftone and fallback, release-driven downloads, phone menu, FAQ, 404 and reduced motion", "site demo from disk"], docImagesUpdated: updateDocImages, errors, writes }, null, 2));
  } catch (error) {
    await capture("failure").catch(() => {});
    fs.writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: false, failure: error.stack, actual: error.actual, expected: error.expected, errors }, null, 2));
    console.error(error.stack); process.exitCode = 1;
  }
  finally { win.destroy(); server.close(); exitAfterFlush(process.exitCode); }
});
