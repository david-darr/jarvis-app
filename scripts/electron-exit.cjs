// Exit an Electron test harness with a real exit code AND its output intact.
//
// Neither of Electron's own exits does both. On Electron 31/Windows,
// `process.exitCode = 1; app.quit()` exits 0 — a failed suite reads as green
// to any shell, script, or CI step that checks the code. `app.exit(1)` keeps
// the code but tears the process down before stdout/stderr have drained,
// which is how an earlier run "passed" while printing nothing: on Windows
// both streams are async pipes when redirected. So drain both first, then
// app.exit(). Verified on the project's own binary with a 20KB line through
// a pipe and a non-zero code — both survive.
const { app } = require('electron');

function exitAfterFlush(code) {
  process.exitCode = code || 0;
  let pending = 2;
  const done = () => { if (--pending === 0) app.exit(process.exitCode); };
  process.stdout.write('', done);
  process.stderr.write('', done);
}

module.exports = { exitAfterFlush };
