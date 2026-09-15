// Launches the PACKAGED Windows app and proves it actually opens.
//
// Written after 1.9.0 shipped broken: electron/browser.js was missing from
// electron-builder's `files` allowlist, so the packaged main process died on
// require() before it ever started the backend, and the app never opened at
// all. The local pre-release check at the time only started the bundled
// Python backend directly, which of course worked - it never ran the
// Electron shell that was actually broken. The macOS CI does launch the app,
// which is why CI caught what the local check missed.
//
// This is the Windows equivalent of that CI step, so both platforms produce
// the same evidence before a release goes out. It deliberately drives
// dist/win-unpacked (the real packaged tree, asar included) rather than the
// dev checkout, because the entire class of bug being guarded against is
// "works from source, missing from the package".

const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const root = path.resolve(__dirname, '..');
const appExe = path.join(root, 'dist', 'win-unpacked', 'JARVIS.exe');
const asar = path.join(root, 'dist', 'win-unpacked', 'resources', 'app.asar');
const PORT = 8420;
const BACKSLASH = String.fromCharCode(92);

function fail(msg) { console.error('FAIL: ' + msg); process.exit(1); }

if (!fs.existsSync(appExe)) fail('no packaged app at ' + appExe + ' - run `npm run dist` in electron/ first');

// Static check first. Every module main.js requires by relative path must
// actually be inside the asar. This alone would have caught the 1.9.0 bug,
// and it runs in milliseconds without launching anything.
const listed = spawnSync('npx', ['asar', 'list', asar], { encoding: 'utf8', shell: true });
if (listed.status !== 0) fail('could not read ' + asar);
const packaged = new Set(
  listed.stdout
    .split('\n')
    .map((line) => line.trim().split(BACKSLASH).join('/').replace(/^\//, ''))
    .filter(Boolean),
);
const mainJs = fs.readFileSync(path.join(root, 'electron', 'main.js'), 'utf8');
const required = [...mainJs.matchAll(/require\(["']\.\/([^"']+)["']\)/g)].map((m) => m[1]);
const missing = required.filter(
  (rel) => ![rel, rel + '.js', rel + '.json'].some((c) => packaged.has(c)),
);
if (missing.length) fail('main.js requires modules that are NOT in the package: ' + missing.join(', '));
console.log('packaged modules OK (main.js requires: ' + (required.join(', ') || 'none') + ')');

// Then the real thing: launch it and wait for its own backend to answer.
const logPath = path.join(os.tmpdir(), 'jarvis-verify-' + Date.now() + '.log');
const log = fs.openSync(logPath, 'a');
const child = spawn(appExe, [], {
  stdio: ['ignore', log, log],
  env: { ...process.env, ELECTRON_ENABLE_LOGGING: '1' },
});

let cleaned = false;
const cleanup = () => {
  if (cleaned) return;
  cleaned = true;
  try { spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' }); } catch (e) {}
  // Close-to-tray means killing the shell can leave the backend holding the
  // port, which would make the NEXT run look healthy for the wrong reason.
  try {
    spawnSync('powershell', ['-NoProfile', '-Command',
      "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*uvicorn*app:app*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
      { stdio: 'ignore' });
  } catch (e) {}
};
process.on('exit', cleanup);

(async () => {
  const deadline = Date.now() + 180000;
  let healthy = false;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) break;
    try {
      const res = await fetch('http://127.0.0.1:' + PORT + '/api/health');
      if (res.ok) { healthy = true; break; }
    } catch (e) { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 2000));
  }
  let output = '';
  try { output = fs.readFileSync(logPath, 'utf8'); } catch (e) {}
  cleanup();

  if (output.trim()) console.log('--- app output ---\n' + output.trim());
  // Startup failures in Electron's main process are otherwise SILENT: the
  // app keeps running and shows nothing, so treat them as build failures.
  if (/UnhandledPromiseRejection|Cannot find module|\[startup\]|\[tray\] unavailable/.test(output)) {
    fail('the app reported a startup error (see output above)');
  }
  if (!healthy) fail('the packaged app never reached its own backend');
  console.log('PASS: packaged app launched and served its backend');
})();
