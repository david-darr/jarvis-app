// Launches the PACKAGED Windows app and proves it actually opens - and,
// since roadmap phase 8 (2026-10-06), that it upgrades old data, restarts,
// starts with no internet, and restores a backup.
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
//
// Stages, each in a throwaway profile (--user-data-dir):
//   1. first start      an empty profile reaches its own new backend
//   2. upgrade          an old-format profile (scripts/legacy_profile.py: a
//                       v3 session store with chats, a task cut off by an
//                       older build, an unreadable skill) starts, upgrades,
//                       keeps every chat, and does not rerun the task
//   3. restart          the same profile stops and starts again; nothing
//                       reruns and nothing upgrades twice
//   4. offline          the same profile starts with every outbound
//                       connection pointed at a dead proxy
//   5. restore          the packaged runtime backs that profile up
//                       (core/backup.py); a fresh profile restores it at
//                       start and holds the same chats
// The first stage alone (the original check) runs with --first-start-only.

const { spawn, spawnSync } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const os = require('node:os');

const root = path.resolve(__dirname, '..');
const appExe = path.join(root, 'dist', 'win-unpacked', 'JARVIS.exe');
const asar = path.join(root, 'dist', 'win-unpacked', 'resources', 'app.asar');
const backendDir = path.join(root, 'dist', 'win-unpacked', 'resources', 'backend');
const runtimeExe = path.join(backendDir, 'runtime', 'python.exe');
const legacyScript = path.join(root, 'scripts', 'legacy_profile.py');
const PORT = 8420;
const BACKSLASH = String.fromCharCode(92);
const FIRST_ONLY = process.argv.includes('--first-start-only');

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
const profiles = [];
let realRuntime = null;
let baseline = new Set();

function newProfile() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jarvis-packaged-verify-'));
  profiles.push(dir);
  return dir;
}

// Stop the app this verifier started: its shell, and only new processes
// running THIS package's exact bundled runtime (the shell can exit first,
// close-to-tray).
function stopApp() {
  if (child && child.pid) spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
  if (realRuntime && child) {
    try {
      for (const pid of bundledBackends(realRuntime)) {
        if (!baseline.has(pid)) spawnSync('taskkill', ['/PID', String(pid), '/T', '/F'], { stdio: 'ignore' });
      }
    } catch (e) { console.error('stop: ' + e.message); }
  }
  child = null;
}

let cleaned = false;
function cleanup() {
  if (cleaned) return;
  cleaned = true;
  stopApp();
  const tempRoot = fs.realpathSync(os.tmpdir()) + path.sep;
  for (const dir of profiles) {
    if (!fs.existsSync(dir)) continue;
    const owned = fs.realpathSync(dir);
    if (!owned.startsWith(tempRoot) || !path.basename(owned).startsWith('jarvis-packaged-verify-')) {
      console.error('refusing to remove a user-data directory outside the verifier temp root: ' + owned);
      continue;
    }
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
  }
}
process.on('exit', cleanup);

// Start the packaged app on a profile and wait for its own new backend.
async function launch(profile, label, { args = [], env: extra = {} } = {}) {
  await requireFreePort();
  const logPath = path.join(profile, `packaged-${label}.log`);
  const log = fs.openSync(logPath, 'a');
  const env = { ...process.env, ELECTRON_ENABLE_LOGGING: '1', ...extra };
  delete env.JARVIS_BACKEND_URL; // An inherited override would bypass the packaged backend.
  child = spawn(appExe, ['--user-data-dir=' + profile, ...args], { stdio: ['ignore', log, log], env });
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
  if (output.trim()) console.log(`--- app output (${label}) ---\n` + output.trim());
  // Startup failures in Electron's main process are otherwise SILENT.
  if (/UnhandledPromiseRejection|Cannot find module|\[startup\]|\[tray\] unavailable/.test(output)) {
    fail(`${label}: the app reported a startup error (see output above)`);
  }
  if (!healthy) fail(`${label}: the packaged app never reached its own backend`);
  const owner = listenerPid();
  const found = bundledBackends(realRuntime);
  if (!owner || baseline.has(owner) || !found.has(owner)) {
    fail(`${label}: health responded, but the listener is not a new backend from this package (listener PID ${owner}, bundled backend PIDs ${[...found]}, runtime ${realRuntime})`);
  }
  // Let the backend finish its startup work (recovery, upgrades, first
  // scheduler pass) before it is stopped and inspected.
  await new Promise((r) => setTimeout(r, 20000));
  stopApp();
  await new Promise((r) => setTimeout(r, 1500));
  await requireFreePort();
}

// Run the packaged runtime; JSON out when asked.
function packagedPython(args, env = {}) {
  const result = spawnSync(runtimeExe, args, { cwd: backendDir, encoding: 'utf8', env: { ...process.env, ...env } });
  if (result.status !== 0) fail(`packaged runtime failed: ${args.join(' ')}\n${result.stdout}\n${result.stderr}`);
  return result.stdout.trim();
}

function checkProfile(profile, stage) {
  const out = spawnSync(runtimeExe, [legacyScript, 'check', path.join(profile, 'data'), stage], { encoding: 'utf8' });
  console.log(`${stage}: ${out.stdout.trim()}`);
  if (out.status !== 0) fail(`${stage}: ${out.stderr.trim() || 'the profile check failed (see above)'}`);
}

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

  // 1. First start.
  const empty = newProfile();
  await launch(empty, 'first-start');
  const dataDir = path.join(empty, 'data');
  if (!fs.existsSync(dataDir) || fs.readdirSync(dataDir).length === 0) {
    fail('--user-data-dir did not place backend data in the temporary profile');
  }
  console.log('PASS 1/5: packaged app launched its own backend with isolated user data');
  if (FIRST_ONLY) return;

  // 2. Upgrade an old profile.
  const old = newProfile();
  packagedPython([legacyScript, 'make', path.join(old, 'data')]);
  await launch(old, 'upgrade');
  checkProfile(old, 'upgraded');
  console.log('PASS 2/5: an old-format profile upgraded with every chat kept and nothing rerun');

  // 3. Restart it.
  await launch(old, 'restart');
  checkProfile(old, 'restarted');
  console.log('PASS 3/5: restarted with nothing rerun and nothing upgraded twice');

  // 4. Offline: Python's HTTP clients follow these variables; Chromium the flag.
  const dead = 'http://127.0.0.1:9';
  await launch(old, 'offline', {
    args: ['--proxy-server=' + dead],
    env: { HTTP_PROXY: dead, HTTPS_PROXY: dead, ALL_PROXY: dead, NO_PROXY: '127.0.0.1,localhost' },
  });
  checkProfile(old, 'restarted');
  console.log('PASS 4/5: started and served with no internet');

  // 5. Back it up with the packaged code, restore into a fresh profile.
  const zip = path.join(old, 'backup.zip');
  packagedPython(['-c', `import sys; sys.path.insert(0, '.'); from core import backup; backup.make_backup(${JSON.stringify(zip)})`],
                 { JARVIS_DATA_DIR: path.join(old, 'data') });
  const fresh = newProfile();
  fs.mkdirSync(path.join(fresh, 'data', '.restore'), { recursive: true });
  fs.copyFileSync(zip, path.join(fresh, 'data', '.restore', 'pending.zip'));
  await launch(fresh, 'restore');
  checkProfile(fresh, 'restored');
  console.log('PASS 5/5: a backup made by the package restored at start into a fresh profile');
  console.log('PASS: packaged app first start, upgrade, restart, offline and restore');
})().catch((error) => {
  console.error('FAIL: ' + error.message);
  process.exitCode = 1;
}).finally(cleanup);
