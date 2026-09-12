// Run: electron/node_modules/electron/dist/electron.exe scripts/ui-smoke.cjs
// Isolated renderer checks. All API responses are synthetic, all writes stay
// in this process, and no request can reach the running JARVIS backend.
const { app, BrowserWindow, session } = require("electron");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const root = path.resolve(__dirname, "..");
const output = path.join(root, "data", "ui-review", "run-" + Date.now());
const updateDocImages = process.argv.includes("--update-doc-images");
let empty = false, unavailable = false;
let sessionDelay = 0;
const writes = [];
const now = Date.now() / 1000;
const future = (hours) => new Date(Date.now() + hours * 3600000).toISOString();
const models = [
  { id: "m1", name: "Claude", model: "Sonnet", kind: "claude_cli" },
  { id: "m2", name: "Codex", model: "Default", kind: "codex_cli" },
  { id: "m3", name: "Local workspace", model: "Local model", kind: "local" },
];
const sessions = [
  { id: "s1", title: "A clearer direction for the workspace", updated_at: now - 1200, project_id: "p1" },
  { id: "s2", title: "Planning the week ahead", updated_at: now - 5000 },
  { id: "s3", title: "Connecting ideas across the vault", updated_at: now - 80000 },
];
const projects = [{ id: "p1", name: "Workspace design", document_ids: ["d1", "d2"], instructions: "Keep it clear." }];
const notes = [
  { id: "n1", text: "Review the workspace design and collect feedback", completed: false, due_date: future(24) },
  { id: "n2", text: "Prepare notes for the project check-in", completed: false, due_date: future(48) },
  { id: "n3", text: "Organize this week's reference material", completed: true },
];
const events = [
  { id: "e1", title: "Project check-in", start: future(3), end: future(4), source: "event" },
  { id: "e2", title: "Time to think", start: future(26), end: future(27), source: "event" },
  { id: "e3", title: "Weekly review", start: future(60), end: future(61), source: "event" },
];
const tasks = [{ id: "t1", name: "Daily briefing", enabled: true, schedule_kind: "daily", run_time: "07:00", next_run_at: future(6), last_run_at: null }];
const docs = ["Design principles", "Project research", "Ideas for next week"].map((title, i) => ({ id: "d" + (i + 1), title, updated_at: now - i * 3600 }));
function graph() {
  const nodes = [{ id: "", name: "Vault", type: "folder", folder: "" }], edges = [];
  if (empty) return { nodes, edges };
  for (const [index, folder] of ["Projects", "Resources", "Daily notes", "Personal", "Learning"].entries()) {
    nodes.push({ id: folder, name: folder, type: "folder", folder: "" });
    edges.push({ source: "", target: folder, kind: "contains" });
    for (let j = 0; j < 26; j++) {
      const id = folder + "/note-" + j + ".md";
      nodes.push({ id, name: j === 0 ? folder + " index" : folder + " note " + j, folder, type: "note" });
      edges.push({ source: folder, target: id, kind: "contains" });
      if (j > 0 && j % 4 === 0) edges.push({ source: id, target: folder + "/note-0.md", kind: "link" });
    }
  }
  return { nodes, edges };
}
const errors = [];
function fixture(url) {
  const route = url.pathname;
  const list = (data) => empty ? [] : data;
  if (route === "/api/auth/status") return { auth_enabled: false, setup_required: false, username: "Alex", is_admin: true };
  if (route === "/api/settings") return { onboarding_complete: true, developer_mode_enabled: false };
  if (route === "/api/system/custom-tabs") return [{ id: "school", label: "School" }];
  if (route === "/api/system/status") return { scheduler_running: true, vault_ok: true, enabled_task_count: empty ? 0 : 1, model_endpoint_count: empty ? 0 : 3, discord_connected_bots: [], next_task: empty ? null : { name: "Daily briefing", next_run_at: future(6) } };
  if (route === "/api/system/events") return list([{ message: "Daily briefing completed", level: "info", ts: now - 800 }, { message: "Memory sync finished", level: "info", ts: now - 2000 }]);
  if (route === "/api/sessions") return list(sessions);
  if (route.startsWith("/api/sessions/")) return { ...sessions[0], id: route.split("/")[3], model_endpoint_id: "m1", messages: [{ role: "user", content: "Let's make the workspace feel more focused.", ts: now - 100 }, { role: "assistant", content: "Let's start with what matters most: clear navigation, a calm reading space, and useful connections between your work.\n\nEverything should have a place, and enough room to breathe.", ts: now - 90 }] };
  if (route === "/api/projects") return list(projects);
  if (route.startsWith("/api/projects/")) return projects[0];
  if (route === "/api/notes") return list(url.searchParams.get("include_completed") === "false" ? notes.filter(n => !n.completed) : notes);
  if (route === "/api/calendar/events") return list(events);
  if (route === "/api/calendar/events/archived") return [];
  if (route === "/api/tasks") return list(tasks);
  if (route === "/api/tasks/builtin") return ["Daily briefing", "Review priorities", "Organize memory", "Inbox triage"].map((label, i) => ({ label, description: "Keep the important things in view with a regular review.", action_id: "routine" + i, enabled: i === 0 && !empty, task_id: "t1", uses_model: true, default_daily_time: "07:00" }));
  if (route === "/api/models") return list(models);
  if (route === "/api/models/usage") return { m1: { percentage: 18 } };
  if (route === "/api/documents") return list(docs);
  if (route === "/api/documents/search") return list(docs.filter(d => d.title.toLowerCase().includes(url.searchParams.get("q").toLowerCase())));
  if (route.startsWith("/api/documents/")) return { ...docs[0], content: "# Design principles\n\nMake the important things easy to find." };
  if (route === "/api/skills") return list([{ slug: "weekly-review", description: "Review the week and plan what comes next." }, { slug: "writing-partner", description: "Turn rough ideas into clear, useful writing." }]);
  if (route === "/api/vault/graph") return graph();
  if (route === "/api/vault/note") return { content: "# Projects index\n\nA connected place for ideas and ongoing work." };
  if (route === "/api/email/triage") return { generated_at: now, scanned: 12, items: empty ? [] : [{ subject: "Project check-in this afternoon", from: "team@example.test", reason: "An upcoming meeting needs your review." }] };
  if (route === "/api/email/accounts") return [];
  if (route === "/api/channels") return [];
  if (route === "/api/cookbook/status") return { reachable: true };
  if (route === "/api/cookbook/installed") return list([{ name: "local-workspace:8b", size: 4500000000 }]);
  if (route === "/api/cookbook/running") return [];
  if (route === "/api/cookbook/catalog" || route === "/api/cookbook/engine/catalog") return list([{ name: "local-workspace:8b", label: "Local workspace", params: "8B", description: "A compact model for everyday conversations." }]);
  if (route === "/api/cookbook/engine/status") return { running: false };
  if (route === "/api/cookbook/engine/downloaded") return [];
  if (route === "/api/tab-school/settings") return { canvas_base_url: "", ics_url: "", canvas_api_token_configured: false };
  if (route === "/api/tab-school/courses") return list([{ name: "Software Design", upcoming_count: 2, overdue_count: 0, assignment_count: 8 }]);
  if (route === "/api/tab-school/assignments") return url.searchParams.has("overdue") ? [] : list([{ id: "a1", course: "Software Design", title: "Review the project brief", due: future(40), completed: false, attachment_links: [] }]);
  if (route === "/api/integrations") return [];
  throw new Error("No fixture for " + route);
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://localhost");
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
  const file = path.resolve(root, url.pathname === "/" ? "static/index.html" : url.pathname === "/docs/" ? "docs/index.html" : "." + decodeURIComponent(url.pathname));
  if (!["static", "docs"].some(dir => file.startsWith(path.join(root, dir) + path.sep))) { res.writeHead(404); res.end(); return; }
  try {
    const mime = { ".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".png": "image/png" };
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
  session.defaultSession.webRequest.onBeforeRequest((details, cb) => cb({ cancel: !details.url.startsWith(base + "/") }));
  const win = new BrowserWindow({ width: 1440, height: 900, show: false, useContentSize: true, webPreferences: { offscreen: true, contextIsolation: true, nodeIntegration: false } });
  win.webContents.on("console-message", (_e, level, message) => { if (level >= 3) errors.push(message); });
  const js = (code) => win.webContents.executeJavaScript(code);
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
  };
  const overflow = async () => js(`Array.from(document.querySelectorAll('#view-content, #view-content .view-constrained, .chat-input-bar, .settings-content, .cal-left')).filter(e => e.clientWidth > 0 && e.scrollWidth > e.clientWidth + 2).map(e => ({class: e.className, width:e.clientWidth, scroll:e.scrollWidth}))`);
  try {
    await win.loadURL(base);
    // Offscreen windows do not receive native focus. Once a page exists,
    // emulate it for real DOM focus/keyboard events.
    win.webContents.debugger.attach("1.3");
    await win.webContents.debugger.sendCommand("Emulation.setFocusEmulationEnabled", { enabled: true });
    await waitFor("document.querySelectorAll('.dashboard-stat').length === 4");
    const railWidth = () => js("document.querySelector('#sidebar').getBoundingClientRect().width");
    assert.equal(await railWidth(), 204);
    await js("document.querySelector('#sidebar-toggle').click()");
    await delay(80);
    const movingWidth = await railWidth();
    assert.ok(movingWidth > 72 && movingWidth < 204, "Sidebar animates between widths");
    await delay(300);
    assert.equal(await railWidth(), 72);
    assert.equal(await js("document.querySelector('#sidebar-toggle').getAttribute('aria-expanded')"), "false");
    assert.equal(await js("localStorage.getItem('jarvis:sidebar-collapsed')"), "true");
    assert.ok(await js("[...document.querySelectorAll('#nav button')].every(b => b.getAttribute('aria-label') && b.querySelector('svg'))"), "Every icon-only tab has an accessible name and an icon");
    await capture("desktop-sidebar-collapsed");
    await win.loadURL(base);
    await waitFor("document.querySelectorAll('.dashboard-stat').length === 4");
    await delay(350);
    assert.equal(await railWidth(), 72, "Collapsed state survives reload");
    for (const tab of ["chat", "notes", "library", "calendar", "tasks", "email", "brain", "cookbook", "school"]) {
      await navigate(tab);
      assert.deepEqual(await overflow(), [], "collapsed " + tab);
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
      for (const tab of ["home", "chat", "notes", "library", "calendar", "tasks", "email", "brain", "cookbook", "school"]) {
        await navigate(tab, tab === "brain" ? { section: "skills" } : {});
        if (tab === "home") await waitFor("document.querySelectorAll('.dashboard-stat').length === 4");
        assert.deepEqual(await overflow(), [], label + " overflow in " + tab);
        await capture(label + "-" + tab);
      }
      await navigate("brain", { section: "vault" });
      await waitFor("document.querySelector('.vault-caption').textContent.includes('130 notes')");
      await delay(400);
      await capture(label + "-vault");
      await js("document.querySelector('.vault-search').value = 'Projects index'; document.querySelector('.vault-search').dispatchEvent(new Event('input'))");
      assert.equal(await js("document.querySelectorAll('.vault-search-result').length"), 1);
      await js("document.querySelector('.vault-search-result').click()");
      await waitFor("document.querySelector('.vault-panel-body').textContent.includes('connected place')");
      await capture(label + "-vault-note");
      assert.deepEqual(await overflow(), [], label + " vault note overflow");
      await navigate("home");
      await js("document.querySelector('.sidebar-settings-btn').click()");
      await waitFor("!!document.querySelector('#settings-content .card')");
      assert.deepEqual(await overflow(), [], label + " settings overflow");
      if (label === "mobile") {
        assert.ok(await js("document.querySelector('.settings-search').getBoundingClientRect().width > 250"), "Settings search has room to type");
        assert.ok(await js("[...document.querySelectorAll('#settings-content input')].filter(e=>e.offsetWidth).every(e=>e.offsetWidth>=170)"), "Mobile model fields do not collapse");
      }
      await capture(label + "-settings");
      await js(`document.querySelector(".settings-titlebar-btn[title='Close'], .settings-titlebar-btn[title='Back']").click()`);
    }
    win.setContentSize(1440, 900);
    await navigate("home");
    await waitFor("document.querySelectorAll('.dashboard-row').length > 0");
    await js("document.querySelector('.dashboard-row').click()");
    await waitFor("document.querySelectorAll('#chat-messages .msg').length === 2");
    await capture("desktop-conversation");
    assert.equal(writes.length, 0, "Navigation must not write data");
    await js("document.querySelector('#model-picker-btn').click()");
    await capture("desktop-model-menu");
    const menuFits = await js("(() => {const r=document.querySelector('#model-picker-menu').getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth && r.top>=0 && r.bottom<=innerHeight})()");
    assert.ok(menuFits, "Model menu fits viewport");
    await navigate("chat");
    assert.equal(await js("getComputedStyle(document.querySelector('.border-beam'),'::before').animationName"), "border-orbit");
    await js("document.querySelector('.border-beam').dataset.active='false'");
    assert.equal(await js("getComputedStyle(document.querySelector('.border-beam'),'::before').animationPlayState"), "paused");
    await js("document.querySelector('.border-beam').dataset.active='true'");
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    assert.equal(await js("getComputedStyle(document.querySelector('.border-beam'),'::before').animationName"), "none");
    assert.ok(await js("parseFloat(getComputedStyle(document.querySelector('#sidebar')).transitionDuration) < .01"), "Reduced motion disables the rail transition");
    await navigate("home");
    await waitFor("document.querySelector('.core-motion-toggle')?.textContent === 'Resume motion'");
    await capture("desktop-reduced-motion");
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [] });
    await navigate("chat");
    await js("document.querySelector('.suggestion-chip').click()");
    assert.ok((await js("document.querySelector('#chat-input').value")).includes("calendar"));
    assert.equal(writes.length, 0, "Suggestions only draft a prompt");
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
    assert.ok(await js("!!document.querySelector('.chat-welcome')"));
    empty = true;
    for (const tab of ["home", "chat", "notes", "library", "calendar", "tasks", "email", "brain", "cookbook", "school"]) {
      await navigate(tab, tab === "brain" ? { section: "skills" } : {});
      if (tab === "home") await waitFor("document.querySelector('.dashboard-stat-value')?.textContent === '0'");
      assert.deepEqual(await overflow(), [], "empty " + tab);
    }
    unavailable = true;
    await navigate("home");
    await waitFor("document.querySelector('.dashboard-system-pill')?.textContent === 'Status unavailable'");
    await capture("desktop-status-unavailable");
    assert.deepEqual(errors, [], "Renderer errors");
    // Explicit opt-in only: these are actual renderer captures with synthetic
    // data. Neither the live backend nor personal screenshots are a source.
    if (updateDocImages) {
      const mapping = { home: "desktop-home", chat: "desktop-conversation", tasks: "desktop-tasks", vault: "desktop-vault", calendar: "desktop-calendar", notes: "desktop-notes", brain: "desktop-brain", settings: "desktop-settings", library: "desktop-library", "sidebar-collapsed": "desktop-sidebar-collapsed" };
      for (const [name, source] of Object.entries(mapping)) fs.copyFileSync(path.join(output, source + ".png"), path.join(root, "docs", "img", name + ".png"));
    }
    for (const [label, width, height] of [["desktop",1440,900],["mobile",390,844]]) {
      win.setContentSize(width, height);
      await win.loadURL(base + "/docs/");
      await js("Promise.all([...document.images].map(image => { image.loading = 'eager'; return image.decode(); }))");
      await js("window.scrollTo({top:0,behavior:'instant'})");
      await waitFor("document.querySelector('#preview-image')?.complete");
      assert.ok(await js("document.documentElement.scrollWidth <= innerWidth"), label + " website fits");
      const anchors = await js("[...document.querySelectorAll('a[href^=\"#\"]')].every(a => document.querySelector(a.getAttribute('href')))");
      assert.ok(anchors, "Website anchors resolve");
      await capture(label + "-website");
      for (const key of ["chat","vault","sidebar-collapsed","home"]) {
        await js("document.querySelector('[data-preview=\"" + key + "\"]').click()");
        await waitFor("document.querySelector('#preview-image').getAttribute('src') === 'img/" + key + ".png'");
        assert.ok(await js("document.querySelector('#preview-image').naturalWidth > 0"));
      }
      await js("document.querySelector('.preview-switcher').scrollIntoView({behavior:'instant'})");
      await capture(label + "-website-preview");
      for (const section of ["features","tour","install"]) {
        await js("document.querySelector('#" + section + "').scrollIntoView({behavior:'instant'})");
        await delay(100);
        await capture(label + "-website-" + section);
      }
      await js("document.querySelector('.install-note').open = true");
      assert.ok(await js("document.documentElement.scrollWidth <= innerWidth"));
      assert.ok(await js("[...document.images].every(i => i.complete && i.naturalWidth > 0)"), "Every website image loads");
    }
    assert.deepEqual(errors, [], "Website renderer errors");
    await win.webContents.debugger.sendCommand("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
    assert.equal(await js("getComputedStyle(document.documentElement).scrollBehavior"), "auto");
    assert.equal(await js("getComputedStyle(document.querySelector('.button')).transitionDuration"), "0s");
    console.log("PASS: app desktop/mobile and icon rail, persistence/keyboard/tooltips, reduced motion, vault/chat/Settings, empty/error states, website layouts/links/images/previews.");
    console.log("Screenshots: " + output);
    fs.writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: true, checks: ["10 tabs desktop/mobile", "icon rail layout and animation", "sidebar persistence, keyboard, tooltips, mobile override", "vault search/read", "Settings and usable mobile forms", "Home chat link and model menu", "beam/core reduced motion", "draft suggestions and mocked note completion", "delayed navigation", "10 empty views and unavailable status", "website desktop/mobile layouts, anchors, images, preview switcher and reduced motion"], docImagesUpdated: updateDocImages, errors, writes }, null, 2));
  } catch (error) {
    await capture("failure").catch(() => {});
    fs.writeFileSync(path.join(output, "result.json"), JSON.stringify({ passed: false, failure: error.stack, actual: error.actual, expected: error.expected, errors }, null, 2));
    console.error(error.stack); process.exitCode = 1;
  }
  finally { win.destroy(); server.close(); app.exit(process.exitCode || 0); }
});
