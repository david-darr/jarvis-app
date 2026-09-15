// PowerShell can detach GUI executables and report success before they exit.
// Node waits for Electron and propagates the actual suite result.
const { spawnSync } = require('node:child_process');
const path = require('node:path');
const suite = process.argv[2];
if (!['browser-smoke', 'chat-smoke', 'ui-smoke'].includes(suite)) {
  throw new Error('Choose browser-smoke, chat-smoke, or ui-smoke');
}
const result = spawnSync(require('../electron/node_modules/electron'),
  [path.join(__dirname, suite + '.cjs'), ...process.argv.slice(3)],
  { stdio: 'inherit', windowsHide: true, timeout: 240000 });
if (result.error) console.error(result.error.message);
process.exit(result.status ?? 1);
