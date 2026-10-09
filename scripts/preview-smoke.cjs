// Offline native preview isolation, with real local fixture pages. Run via run-electron-test.cjs.
const { app, BrowserWindow, session } = require('electron');
const assert = require('node:assert/strict');
const http = require('node:http');
const path = require('node:path');
const { exitAfterFlush } = require('./electron-exit.cjs');
const preview = require('../electron/forgePreview');
const browser = require('../electron/browser');
app.setPath('userData', path.join(__dirname, '..', 'data', 'preview-smoke-profile'));
const requests = [], handoffs = [];
let win, appPort, backendPort, allowed;
const fixture = http.createServer((req, res) => {
  requests.push(req.url);
  if (req.url === '/redirect') { res.writeHead(302, { Location: 'https://outside.test/' }); res.end(); return; }
  res.setHeader('Content-Type', 'text/html');
  res.end('<!doctype html><title>Forge app fixture</title><h1>Preview works</h1>');
});
const backend = http.createServer((_req, res) => { requests.push('BACKEND REACHED'); res.end('privileged'); });
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
const wait = async condition => { for (let i = 0; i < 100; i++) { if (await condition()) return; await new Promise(r => setTimeout(r, 50)); } throw new Error('Timed out'); };
function finish(code, message) {
  preview.close(); browser.close(); win?.destroy(); fixture.close(); backend.close();
  (code ? console.error : console.log)(message); exitAfterFlush(code);
}
process.on('unhandledRejection', error => finish(1, error.stack));
setTimeout(() => finish(1, 'Preview smoke timed out'), 90000).unref();
app.whenReady().then(async () => {
  appPort = await listen(fixture); backendPort = await listen(backend); allowed = [appPort, backendPort];
  win = new BrowserWindow({ show: false, webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false } });
  await win.loadURL(`http://127.0.0.1:${backendPort}/`); requests.length = 0;
  const send = win.webContents.send.bind(win.webContents);
  win.webContents.send = (channel, payload) => { if (channel === 'browser:open-request') handoffs.push(payload); send(channel, payload); };
  const options = { backendOrigin: `http://127.0.0.1:${backendPort}`, getAllowedPorts: async id => { assert.equal(id, 'own-session'); return allowed; } };
  preview.attach(win, options); browser.attach(win, options);
  const url = `http://127.0.0.1:${appPort}/`;
  assert.equal((await preview.open('own-session', url, { x: 0, y: 0, width: 1000, height: 600 })).ok, true);
  await wait(() => preview.state().url === url && !preview.state().loading);
  const wc = preview._webContents();
  assert.equal(await wc.executeJavaScript('document.querySelector("h1").textContent'), 'Preview works');
  for (const bad of [`http://127.0.0.1:${backendPort}/api/settings`, 'http://localhost:1/',
    'http://127.9.1.2:3000/', 'http://[::1]:3000/', 'http://0.0.0.0:3000/',
    'file:///etc/passwd', 'data:text/html,secret', 'https://localhost:3000/', 'https://outside.test/']) assert.equal(preview._safeUrl(bad), null, bad);
  assert.ok(preview._safeUrl(`http://localhost:${appPort}/nested?q=1`));
  // Requests (not navigations): the app's own port incl. dev-server live
  // reload over ws, and outside resources; never other loopback or the backend.
  assert.ok(preview._requestAllowed(`ws://localhost:${appPort}/__vite_hmr`, 'webSocket'), 'live reload websocket');
  assert.ok(preview._requestAllowed('https://fonts.example.test/font.woff2', 'font'), 'outside subresource');
  for (const [bad, type] of [[`http://127.0.0.1:${backendPort}/api/settings`, 'xhr'], [`ws://127.0.0.1:${backendPort}/`, 'webSocket'],
    ['http://localhost:1/', 'xhr'], ['ws://localhost:1/', 'webSocket'], ['http://[::1]:3000/', 'image'],
    ['https://outside.test/', 'mainFrame'], ['file:///etc/passwd', 'image'], [`http://user:pw@localhost:${appPort}/`, 'xhr']]) {
    assert.equal(preview._requestAllowed(bad, type), false, `${type} ${bad}`);
  }
  assert.equal(browser._safeUrl(url), null, 'side browser still blocks every loopback port');
  assert.equal(browser._safeUrl(`http://localhost:${appPort}/`), null);
  assert.notEqual(session.fromPartition(preview.PARTITION), win.webContents.session);
  assert.notEqual(session.fromPartition(preview.PARTITION), session.fromPartition(browser.PARTITION));
  assert.equal(preview.PARTITION, 'persist:forge-preview');
  const prefs = wc.getLastWebPreferences();
  assert.equal(prefs.sandbox, true); assert.equal(prefs.nodeIntegration, false); assert.ok(!prefs.preload);
  const probe = await wc.executeJavaScript('({jarvis:typeof window.jarvis,require:typeof require,process:typeof process,module:typeof module})');
  assert.deepEqual(probe, { jarvis: 'undefined', require: 'undefined', process: 'undefined', module: 'undefined' });
  for (const permission of ['media', 'geolocation', 'notifications', 'clipboard-read']) {
    let answer; preview._denyPermissionRequest(null, permission, value => { answer = value; });
    assert.equal(answer, false); assert.equal(preview._denyPermissionCheck(null, permission), false);
  }
  assert.equal(preview._denyDevicePermission({}), false);
  await wc.executeJavaScript(`fetch('http://127.0.0.1:${backendPort}/api/settings', {method:'POST', mode:'no-cors'}).catch(() => {})`);
  assert.ok(!requests.includes('BACKEND REACHED'), 'even no-cors POST cannot reach backend');
  assert.equal((await preview.navigate('file:///etc/passwd')).ok, false);
  assert.equal((await preview.navigate('http://localhost:1/')).ok, false);
  assert.equal((await preview.navigate('https://outside.test/')).ok, false);
  assert.ok(handoffs.includes('https://outside.test/'), 'outside navigation goes to side browser through renderer');
  await wc.executeJavaScript("window.open('https://popup.test/')");
  await wait(() => handoffs.includes('https://popup.test/'));
  await preview.navigate(url + 'redirect');
  await wait(() => handoffs.filter(value => value === 'https://outside.test/').length >= 2);
  assert.ok(!preview.state().url.startsWith('https:'), 'outside redirect stays blocked');
  await preview.navigate(url + 'second');
  await wait(() => preview.state().url === url + 'second' && !preview.state().loading);
  await preview.goBack(); await wait(() => preview.state().url === url);
  await preview.goForward(); await wait(() => preview.state().url === url + 'second');
  await preview.reload();
  for (const width of ['desktop', 'tablet', 'phone']) { preview.setWidth(width); assert.equal(preview.state().width, width); }
  preview.setVisible(false); assert.equal(preview.state().open, true); preview.setVisible(true);
  allowed = [];
  await wait(() => !preview.state().open);
  assert.ok(wc.isDestroyed(), 'revoking the session ports destroys the view');
  preview.close(); preview.close();
  finish(0, 'PASS: allowed local app loads, forbidden URLs and backend requests blocked, outside links handed off, partition isolated, no bridge, permissions denied, history and widths, revoked view destroyed; side browser unchanged.');
}).catch(error => finish(1, error.stack));
