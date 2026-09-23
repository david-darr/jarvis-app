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
const net = require('node:net');
const path = require('node:path');
const os = require('node:os');

const root = path.resolve(__dirname, '..');
const appExe = path.join(root, 'dist', 'win-unpacked', 'JARVIS.exe');
const asar = path.join(root, 'dist', 'win-unpacked', 'resources', 'app.asar');
const runtimeExe = path.join(root, 'dist', 'win-unpacked', 'resources', 'backend', 'runtime', 'python.exe');
const PORT = 8420;
const BACKSLASH = String.fromCharCode(92);

function fail(msg) { throw new Error(msg); }

// A healthy installed JARVIS (or any other listener) could answer the health
// poll while the package exits at requestSingleInstanceLock. Refuse to start
// if this port is bound at all, including by a non-HTTP service.
async function requireFreePort() {
  await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(PORT, '127.0.0.1', () => server.close(resolve));
  }).catch(() => fail('127.0.0.1:8420 is already in use; close JARVIS first. The verifier did not launch or stop anything.'));
}

// netstat gives the PID that owns the exact loopback listener. A 200 alone is
// not proof: only this package's newly started Python runtime may satisfy it.
function listenerPid() {
  const result = spawnSync('netstat', ['-ano', '-p', 'TCP'], { encoding: 'utf8' });
  if (result.status !== 0) fail('could not identify the health-port owner');
  const line = result.stdout.split(/\r?\n/).find((row) =>
    /^\s*TCP\s+127\.0\.0\.1:8420\s+\S+\s+LISTENING\s+\d+\s*$/i.test(row));
  return line ? Number(line.trim().split(/\s+/).at(-1)) : null;
}

function bundledBackends(realRuntime) {
  const quoted = realRuntime.replace(/'/g, "''");
  const command = `Get-CimInstance Win32_Process -Filter "name = 'python.exe'" -ErrorAction Stop | ` +
    `Where-Object { $_.ExecutablePath -ieq '${quoted}' -and $_.CommandLine -like '*-m uvicorn app:app*' } | ` +
    'Select-Object ProcessId | ConvertTo-Json -Compress';
  const result = spawnSync('powershell', ['-NoProfile', '-Command', command], { encoding: 'utf8' });
  if (result.status !== 0) fail('could not inspect the packaged backend process');
  if (!result.stdout.trim()) return new Set();
  const parsed = JSON.parse(result.stdout);
  return new Set([].concat(parsed).map((entry) => Number(entry.ProcessId)));
}

let child = null;
let tempUserData = null;
let realRuntime = null;
let baseline = new Set();
let cleaned = false;
function cleanup() {
  if (cleaned) return;
  cleaned = true;
  if (child && child.pid) {
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
  }
  // The shell can exit before cleanup (close-to-tray). Only new processes
  // running THIS package's exact bundled runtime are eligible for fallback.
  if (realRuntime && child) {
    try {
      for (const pid of bundledBackends(realRuntime)) {
        if (!baseline.has(pid)) spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
      }
    } catch (e) { console.error('cleanup: ' + e.message); }
  }
  if (tempUserData) {
    const tempRoot = fs.realpathSync(os.tmpdir()) + path.sep;
    const owned = fs.realpathSync(tempUserData);
    if (!owned.startsWith(tempRoot) || !path.basename(owned).startsWith('jarvis-packaged-verify-')) {
      fail('refusing to remove a user-data directory outside the verifier temp root');
    }
    fs.rmSync(tempUserData, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
  }
}
process.on('exit', cleanup);

(async () => {
  await requireFreePort();
  if (!fs.existsSync(appExe)) fail('no packaged app at ' + appExe + ' - run `npm run dist` in electron/ first');
  if (!fs.existsSync(runtimeExe)) fail('no bundled runtime at ' + runtimeExe);
  // Windows reports the launch path in Win32_Process even when dist is a junction.
  realRuntime = runtimeExe;

  // Static check first. Every module main.js requires by relative path must
  // actually be inside the asar. This alone would have caught the 1.9.0 bug.
  const listed = spawnSync('npx', ['asar', 'list', asar], { encoding: 'utf8', shell: true });
  if (listed.status !== 0) fail('could not read ' + asar);
  const packaged = new Set(
    listed.stdout.split('\n')
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

  baseline = bundledBackends(realRuntime);
  tempUserData = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-packaged-verify-'));
  const logPath = path.join(tempUserData, 'packaged.log');
  const log = fs.openSync(logPath, 'a');
  const env = { ...process.env, ELECTRON_ENABLE_LOGGING: '1' };
  delete env.JARVIS_BACKEND_URL; // An inherited override would bypass the packaged backend.
  child = spawn(appExe, ['--user-data-dir=' + tempUserData], {
    stdio: ['ignore', log, log], env,
  });
  fs.closeSync(log);

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
  const output = fs.readFileSync(logPath, 'utf8');
  if (output.trim()) console.log('--- app output ---\n' + output.trim());
  // Startup failures in Electron's main process are otherwise SILENT.
  if (/UnhandledPromiseRejection|Cannot find module|\[startup\]|\[tray\] unavailable/.test(output)) {
    fail('the app reported a startup error (see output above)');
  }
  if (!healthy) fail('the packaged app never reached its own backend');
  const owner = listenerPid();
  const found = bundledBackends(realRuntime);
  if (!owner || baseline.has(owner) || !found.has(owner)) {
    fail(`health responded, but the listener is not a new backend from this package (listener PID ${owner}, bundled backend PIDs ${[...found]}, runtime ${realRuntime})`);
  }
  const dataDir = path.join(tempUserData, 'data');
  if (!fs.existsSync(dataDir) || fs.readdirSync(dataDir).length === 0) {
    fail('--user-data-dir did not place backend data in the temporary profile');
  }
  cleanup();
  await requireFreePort();
  console.log('PASS: packaged app launched its own backend with isolated user data');
})().catch((error) => {
  console.error('FAIL: ' + error.message);
  process.exitCode = 1;
}).finally(cleanup);
