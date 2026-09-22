// Isolated Electron renderer smoke. No real backend, CLI, or account calls.
const { app, BrowserWindow, ipcMain } = require('electron');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
let mode = 'normal';
const quota = { providers: [
  { provider: 'claude', stale: false, windows: [
    { name: '5-hour', used_percent: 24, resets_at: Date.now() / 1000 + 3600 },
    { name: 'Weekly', used_percent: 51, resets_at: Date.now() / 1000 + 86400 },
  ] },
  { provider: 'codex', stale: true, windows: [{ name: '5-hour', used_percent: 18, resets_at: null }] },
], recorded: [{ name: 'Local model', kind: 'local', total_tokens: 1234 }] };
const files = {
  '/usage-overlay': ['static/usage-overlay.html', 'text/html'],
  '/static/css/usage-overlay.css': ['static/css/usage-overlay.css', 'text/css'],
  '/static/js/usage-overlay.js': ['static/js/usage-overlay.js', 'text/javascript'],
};
const server = http.createServer((req, res) => {
  if (req.url === '/api/models/quotas') {
    res.writeHead(mode === 'auth' ? 401 : 200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(mode === 'empty' ? { providers: [], recorded: [] } : quota));
    return;
  }
  const file = files[req.url];
  if (!file) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': file[1] });
  res.end(fs.readFileSync(path.join(root, file[0])));
});
let win;
let collapsed = false;
let hide = false;
ipcMain.on('usage-overlay:collapse', (_event, value) => { collapsed = value; });
ipcMain.on('usage-overlay:hide', () => { hide = true; });
async function run() {
  await app.whenReady();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  win = new BrowserWindow({ width: 340, height: 500, show: false,
    webPreferences: { contextIsolation: true, nodeIntegration: false,
      preload: path.join(root, 'electron/usage-overlay-preload.js') } });
  await win.loadURL(`http://127.0.0.1:${port}/usage-overlay`);
  await new Promise(resolve => setTimeout(resolve, 300));
  const text = await win.webContents.executeJavaScript('document.body.innerText');
  assert.match(text, /Claude Code/);
  assert.match(text, /Codex/);
  assert.match(text, /Weekly/);
  assert.match(text, /JARVIS-recorded tokens/);
  const output = path.join(root, 'data', 'usage-overlay-smoke.png');
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, (await win.webContents.capturePage()).toPNG());
  await win.webContents.executeJavaScript("document.getElementById('collapse').click(); document.getElementById('hide').click()");
  assert.equal(collapsed, true);
  assert.equal(hide, true);
  await win.webContents.executeJavaScript("document.getElementById('collapse').click()");
  mode = 'auth';
  await win.webContents.executeJavaScript('refresh()');
  assert.match(await win.webContents.executeJavaScript('document.body.innerText'), /sign in/);
  console.log(`usage overlay smoke passed; screenshot: ${output}`);
}
run().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => {
  if (win && !win.isDestroyed()) win.destroy();
  server.close();
  app.exit(process.exitCode || 0);
});
