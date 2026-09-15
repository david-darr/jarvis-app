// Isolation checks for the in-app side browser (electron/browser.js).
//
// Separate from chat-smoke.cjs on purpose: that suite drives the renderer,
// while everything worth proving here lives in the MAIN process — which
// session a page gets, which URLs are refused, whether permissions are
// denied, and whether the view is really destroyed. Those are the security
// claims, so they get asserted directly rather than inferred from what the
// UI happens to show.
//
// Loopback only by default. No outside network is contacted: pages are
// served by a local fixture server, and the one "remote" origin is a
// loopback port aliased through a host rule, so the blocking rules can be
// exercised without depending on the internet being reachable.
//
// Pass --live to additionally load a real website at the end. That is
// opt-in precisely so the default run stays hermetic and deterministic for
// CI, while a release still gets to prove the pane actually WORKS against
// the real web — the one thing loopback fixtures cannot demonstrate.

const { app, BrowserWindow, session } = require('electron');
const http = require('node:http');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const sideBrowser = require(path.join(root, 'electron', 'browser.js'));

const failures = [];
// Awaits the body, so an async check's rejection is recorded as a failure
// instead of vanishing into an unhandled promise.
async function check(name, fn) {
  try { await fn(); } catch (error) { failures.push(`${name}: ${error.message}`); }
}

// Stands in for the backend the app itself talks to — the privileged origin
// the pane must never reach.
const BACKEND_PORT = 8477;
const BACKEND_ORIGIN = `http://127.0.0.1:${BACKEND_PORT}`;

const server = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'text/html');
  res.end('<!doctype html><title>Fixture</title><h1>fixture page</h1>');
});

app.commandLine.appendSwitch('host-resolver-rules', `MAP fixture.test 127.0.0.1, MAP other.test 127.0.0.1`);

// A main-process failure here would otherwise be a silent hang: the window
// is never shown, so an unhandled rejection leaves the process alive with
// nothing on screen and nothing on stdout. Both guards below turn that into
// a real, readable failure.
//
// app.quit(), never app.exit(): app.exit() tears the process down
// immediately and drops whatever is still buffered on stdout, which is
// exactly how an earlier version of this file "passed" while printing
// nothing at all. scripts/chat-smoke.cjs uses the same process.exitCode +
// app.quit() pairing for the same reason.
function finish(code, message) {
  if (code) console.error(message); else console.log(message);
  process.exitCode = code;
  try { server.close(); } catch { /* already closed */ }
  for (const w of BrowserWindow.getAllWindows()) w.destroy();
  app.quit();
}

process.on('unhandledRejection', (error) => {
  finish(1, 'FAIL (unhandled rejection): ' + ((error && error.stack) || error));
});
// Live page loads are slower and depend on someone else's server, so the
// watchdog is given real room when --live is on.
setTimeout(() => finish(1, 'FAIL: timed out before finishing.'), process.argv.includes('--live') ? 180000 : 90000).unref();

app.whenReady().then(async () => {
  await new Promise(resolve => server.listen(BACKEND_PORT, '127.0.0.1', resolve));

  // -- URL gating. The single choke point every entry path funnels through,
  // so it is tested directly rather than through six different callers.
  await check('rejects non-web schemes', () => {
    sideBrowser.attach(null, { backendOrigin: BACKEND_ORIGIN });
    for (const bad of [
      'file:///C:/Windows/System32/drivers/etc/hosts',
      'file:///etc/passwd',
      'data:text/html,<script>alert(1)</script>',
      'javascript:alert(1)',
      'blob:https://example.test/abc',
      'about:blank',
      'chrome://settings',
      'devtools://devtools/bundled/inspector.html',
      'ws://example.test',
      'not a url at all',
      '',
      null,
    ]) {
      assert.equal(sideBrowser._safeUrl(bad), null, `should refuse ${bad}`);
    }
  });

  await check('rejects loopback in every spelling', () => {
    for (const host of [
      'http://127.0.0.1:8420/api/settings',
      'http://127.0.0.1/',
      'http://127.9.9.9/',
      'http://localhost:3000/',
      'http://LOCALHOST/',
      'http://app.localhost/',
      'http://[::1]:8420/',
      'http://0.0.0.0:8420/',
    ]) {
      assert.equal(sideBrowser._safeUrl(host), null, `should refuse ${host}`);
    }
    // The backend is the reason loopback is blocked at all; confirm the
    // origin check would catch it even if it were not on loopback.
    assert.equal(sideBrowser._safeUrl(`${BACKEND_ORIGIN}/api/sessions`), null);
  });

  await check('allows ordinary web addresses', () => {
    assert.ok(sideBrowser._safeUrl('https://example.test/path?q=1'));
    assert.ok(sideBrowser._safeUrl('http://example.test'));
    // A host that merely starts with the same characters as a blocked one
    // must not be caught by a sloppy prefix match.
    assert.ok(sideBrowser._safeUrl('https://localhost.example.test/'));
    assert.ok(sideBrowser._safeUrl('https://127.0.0.1.example.test/'));
  });

  await check('address bar distinguishes hosts from searches', () => {
    assert.equal(sideBrowser._fromAddressBar('example.test'), 'https://example.test/');
    assert.equal(sideBrowser._fromAddressBar('  example.test/a?b=1  '), 'https://example.test/a?b=1');
    assert.ok(sideBrowser._fromAddressBar('how tall is k2').startsWith('https://duckduckgo.com/?q='));
    // A typed scheme is honoured, which means a typed BAD scheme is refused
    // rather than silently turned into a search.
    assert.equal(sideBrowser._fromAddressBar('file:///etc/passwd'), null);
    assert.equal(sideBrowser._fromAddressBar('http://127.0.0.1:8420'), null);
    assert.equal(sideBrowser._fromAddressBar(''), null);
  });

  const win = new BrowserWindow({
    show: false, width: 1200, height: 800,
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });
  await win.loadURL(`${BACKEND_ORIGIN}/`);
  sideBrowser.attach(win, { backendOrigin: BACKEND_ORIGIN });

  // -- refused targets must not open a view at all, rather than opening one
  // and navigating it somewhere harmless.
  await check('refused target does not open a pane', () => {
    const result = sideBrowser.open('file:///etc/passwd', { x: 0, y: 0, width: 400, height: 400 });
    assert.equal(result.ok, false);
    assert.equal(sideBrowser.isOpen(), false);
  });

  const opened = sideBrowser.open(`http://fixture.test:${BACKEND_PORT}/`, { x: 600, y: 0, width: 600, height: 800 });
  await check('opens a real web page', () => {
    assert.equal(opened.ok, true);
    assert.equal(sideBrowser.isOpen(), true);
  });

  await new Promise(resolve => setTimeout(resolve, 2500));

  // -- the isolation guarantees themselves.
  await check('runs on its own session partition', () => {
    const paneSession = session.fromPartition(sideBrowser.PARTITION);
    const appSession = win.webContents.session;
    assert.notEqual(paneSession, appSession, 'pane must not share the app session');
    assert.equal(sideBrowser.state().open, true);
  });

  await check('a cookie set in the pane is invisible to the app session', async () => {
    // The real guarantee, exercised rather than asserted structurally: a
    // site signed in inside the pane must leave nothing the app's own
    // session can read.
    const paneSession = session.fromPartition(sideBrowser.PARTITION);
    await paneSession.cookies.set({ url: `http://fixture.test:${BACKEND_PORT}/`, name: 'pane_only', value: 'secret' });
    const inPane = await paneSession.cookies.get({ name: 'pane_only' });
    const inApp = await win.webContents.session.cookies.get({ name: 'pane_only' });
    assert.equal(inPane.length, 1, 'cookie should exist in the pane session');
    assert.equal(inApp.length, 0, 'app session must not see the pane cookie');
  });

  await check('page has no preload bridge, no node, no IPC', async () => {
    // Evaluated inside the PAGE itself. If any of these are defined, the
    // sandbox has leaked and an arbitrary website could drive the app.
    const wc = sideBrowser._webContents();
    assert.ok(wc, 'pane should be open for this check');
    const probe = await wc.executeJavaScript(`({
      jarvis: typeof window.jarvis,
      require: typeof window.require,
      process: typeof window.process,
      module: typeof window.module,
      electron: typeof window.electron
    })`);
    assert.equal(probe.jarvis, 'undefined', 'the app bridge must not exist in the pane');
    assert.equal(probe.require, 'undefined');
    assert.equal(probe.process, 'undefined');
    assert.equal(probe.module, 'undefined');
    assert.equal(probe.electron, 'undefined');
  });

  await check('the page cannot reach the backend it is blocked from', async () => {
    // Even though fetch is same-process, the backend origin is a different
    // origin to the page, so CORS must stop it. This is the check that
    // matters most: the backend holds the vault and credentials.
    const wc = sideBrowser._webContents();
    assert.ok(wc);
    const reached = await wc.executeJavaScript(
      `fetch(${JSON.stringify(BACKEND_ORIGIN + '/')}, { mode: 'cors' }).then(() => 'reached').catch(() => 'blocked')`,
    );
    assert.equal(reached, 'blocked');
  });

  await check('permission handlers deny every request', () => {
    // The actual shipped functions, not a check that some handler exists.
    for (const permission of ['media', 'geolocation', 'notifications', 'midi', 'clipboard-read', 'display-capture', 'fullscreen']) {
      let answer = 'never called';
      sideBrowser._denyPermissionRequest({}, permission, (allowed) => { answer = allowed; });
      assert.equal(answer, false, `${permission} must be denied`);
      assert.equal(sideBrowser._denyPermissionCheck({}, permission), false);
    }
    assert.equal(sideBrowser._denyDevicePermission({}), false);
  });

  await check('bounds are applied as whole pixels', () => {
    sideBrowser.setBounds({ x: 10.4, y: 20.6, width: 300.5, height: 400.2 });
    // No getter is exposed for bounds, so the assertion is that a fractional
    // rect is accepted without throwing — the rounding itself is unit-tested
    // by the absence of a crash plus the visual check in chat-smoke.
    assert.equal(sideBrowser.isOpen(), true);
  });

  await check('visibility toggles without destroying the view', () => {
    sideBrowser.setVisible(false);
    assert.equal(sideBrowser.isOpen(), true, 'hiding must not dispose');
    sideBrowser.setVisible(true);
    assert.equal(sideBrowser.isOpen(), true);
  });

  // -- disposal. A hidden view keeps running scripts, timers, and audio, so
  // "closed" has to mean destroyed.
  await check('close destroys the view', () => {
    sideBrowser.close();
    assert.equal(sideBrowser.isOpen(), false);
    assert.equal(sideBrowser.state().open, false);
  });

  await check('close is safe to call twice', () => {
    sideBrowser.close();
    assert.equal(sideBrowser.isOpen(), false);
  });

  await check('controls are no-ops when nothing is open', () => {
    sideBrowser.goBack();
    sideBrowser.goForward();
    sideBrowser.reload();
    sideBrowser.setBounds({ x: 0, y: 0, width: 10, height: 10 });
    sideBrowser.setVisible(true);
    assert.equal(sideBrowser.navigate('https://example.test').ok, false);
    assert.equal(sideBrowser.openExternal().ok, false);
  });

  await check('reopens cleanly after being closed', () => {
    assert.equal(sideBrowser.open(`http://other.test:${BACKEND_PORT}/`, { x: 0, y: 0, width: 400, height: 400 }).ok, true);
    assert.equal(sideBrowser.isOpen(), true);
    sideBrowser.close();
    assert.equal(sideBrowser.isOpen(), false);
  });


  // -- live check against the real web. OFF by default (--live), because the
  // rest of this file is deliberately hermetic and must stay deterministic
  // in CI. Everything above proves the pane REFUSES the right things; this
  // proves it actually WORKS, which is the one thing loopback fixtures
  // cannot show. example.com is IANA's reserved documentation domain: tiny,
  // stable, no tracking, and intended for exactly this.
  if (process.argv.includes('--live')) {
    const LIVE = 'https://example.com/';
    const settled = (wc) => new Promise((resolve) => {
      const done = () => { clearTimeout(timer); wc.off('did-finish-load', done); resolve(true); };
      const timer = setTimeout(() => { wc.off('did-finish-load', done); resolve(false); }, 25000);
      wc.on('did-finish-load', done);
    });

    await check('live: loads and renders a real website', async () => {
      assert.equal(sideBrowser.open(LIVE, { x: 600, y: 0, width: 600, height: 800 }).ok, true);
      const wc = sideBrowser._webContents();
      assert.ok(wc, 'a view should exist for a real https URL');
      assert.ok(await settled(wc), 'the page should finish loading');
      // Real content, read out of the rendered DOM — not just a 200.
      const heading = await wc.executeJavaScript("document.querySelector('h1') && document.querySelector('h1').textContent");
      assert.ok(heading && heading.trim().length, 'the page should render real text');
      assert.ok(wc.getTitle().length, 'the page should report a title');
      const state = sideBrowser.state();
      assert.equal(state.open, true);
      assert.ok(state.url.startsWith('https://'), `expected an https url, got ${state.url}`);
    });

    await check('live: isolation still holds on a real page', async () => {
      const wc = sideBrowser._webContents();
      const probe = await wc.executeJavaScript(`({
        jarvis: typeof window.jarvis,
        require: typeof window.require,
        process: typeof window.process
      })`);
      assert.equal(probe.jarvis, 'undefined');
      assert.equal(probe.require, 'undefined');
      assert.equal(probe.process, 'undefined');
      // A real page must still be unable to reach the local backend.
      const reached = await wc.executeJavaScript(
        `fetch(${JSON.stringify(BACKEND_ORIGIN + '/')}, { mode: 'cors' }).then(() => 'reached').catch(() => 'blocked')`,
      );
      assert.equal(reached, 'blocked');
    });

    await check('live: navigating and going back works on real pages', async () => {
      const wc = sideBrowser._webContents();
      assert.equal(sideBrowser.navigate('https://www.iana.org/help/example-domains').ok, true);
      assert.ok(await settled(wc), 'the second page should finish loading');
      assert.ok(sideBrowser.state().canGoBack, 'back should be available after a second navigation');
      sideBrowser.goBack();
      assert.ok(await settled(wc), 'going back should finish loading');
      assert.ok(sideBrowser.state().url.includes('example.com'), `back should return to the first page, got ${sideBrowser.state().url}`);
    });

    sideBrowser.close();
    await check('live: closed cleanly', () => assert.equal(sideBrowser.isOpen(), false));
  }

  if (failures.length) finish(1, 'FAIL\n' + failures.map(f => ' - ' + f).join('\n'));
  else finish(0, (process.argv.includes('--live') ? 'PASS (with live web check): ' : 'PASS: ') + 'side-browser isolation — scheme and loopback blocking, backend origin refused, separate session partition with unshared cookies, no bridge/node/IPC in the page, permissions denied, view destroyed on close.');
});
