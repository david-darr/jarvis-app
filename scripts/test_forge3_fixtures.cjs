// Standalone, offline fixture contracts. Electron exercises the actual widgets.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const state = {};
const { fixture, mutate } = require('../demo/fixtures.js')({ state });
const read = url => fixture(new URL(url, 'http://fixture'));
const base = '/api/forge/sessions/fs1';
state.forgeNoCommits.fp1 = true;
assert.equal(read('/api/forge/projects/fp1/summary').lifespan.commits, 0);
assert.equal(read('/api/forge/projects').find(p => p.id === 'fp1').git.state, 'empty');
assert.deepEqual(read('/api/forge/projects/fp1/branches'), []);
const task = { project_id: 'fp1', task: 'First task', isolation: 'in_place' };
assert.equal(mutate('/api/forge/sessions', 'POST', task)._status, 409);
const first = mutate('/api/forge/projects/fp1/first-commit', 'POST', {});
assert.equal(first._appStart.permission.title, 'Make the first commit?');
assert.ok(first._appStart.permission.description.includes('Files to commit: 2'));
mutate('/api/permissions/' + first._appStart.permission.id + '/answer', 'POST', { choice: 'reject' });
assert.ok(state.forgeNoCommits.fp1);
const approved = mutate('/api/forge/projects/fp1/first-commit', 'POST', {});
mutate('/api/permissions/' + approved._appStart.permission.id + '/answer', 'POST', { choice: 'once' });
mutate(approved._approvedPath, 'POST', {});
assert.ok(!state.forgeNoCommits.fp1);
assert.equal(mutate('/api/forge/projects/fp1/first-commit', 'POST', {})._status, 400);
assert.ok(mutate('/api/forge/sessions', 'POST', task).id);
const file = read(base + '/file?path=src/garden.js');
assert.equal(file.binary, false);
state.forgeEditorConflict = true;
assert.equal(mutate(base + '/file', 'PUT', { ...file, content: 'human' })._status, 409);
assert.equal(mutate(base + '/file', 'PUT', { ...file, content: 'human', overwrite: true }).content, 'human');
assert.equal(read(base + '/file?path=src/garden.js').content, 'human');
mutate(base + '/git', 'POST', { action: 'stage' });
assert.ok(read(base + '/git').files.every(f => f.staged));
mutate(base + '/git', 'POST', { action: 'commit', message: 'human' });
assert.equal(read(base + '/git').files.length, 0);
for (const action of ['push', 'merge']) {
  const ask = mutate(base + '/git', 'POST', { action });
  assert.ok(ask._appStart.permission);
  assert.ok(ask._approvedPath.endsWith('git-approved'));
  mutate(ask._approvedPath, 'POST', {});
}
assert.equal(read(base + '/git').unmerged, false);
const terminal = mutate(base + '/terminals', 'POST', { rows: 24, cols: 80 });
mutate(`${base}/terminals/${terminal.id}/input`, 'POST', { data: 'echo human\r\n' });
assert.ok(read(`${base}/terminals/${terminal.id}/output?after=1`).events[0].data.includes('human'));
mutate(`${base}/terminals/${terminal.id}/resize`, 'POST', { rows: 30, cols: 100 });
assert.equal(state.forgeTerminals[terminal.id].cols, 100);
mutate(base + '/end', 'POST', { option: 'keep-branch' });
assert.equal(state.forgeTerminals[terminal.id].ended, true);
assert.equal(read(base).forge.removed, true);
assert.equal(read('/api/forge/terminal/settings').forge_terminal_remote, false);
mutate('/api/forge/terminal/settings', 'PUT', { enabled: true });
assert.equal(read('/api/forge/terminal/settings').forge_terminal_remote, true);
const notices = fs.readFileSync(path.join(__dirname, '../static/js/vendor/forge-vendor.LICENSE.txt'), 'utf8');
for (const pkg of ['codemirror', '@codemirror/state', '@codemirror/view', '@lezer/highlight', '@xterm/xterm', '@xterm/addon-fit', '@xterm/addon-web-links']) assert.ok(notices.includes(pkg), pkg + ' license');
console.log('Forge fixtures and vendor notices pass.');
// Node --check cannot see syntax errors in strings sent to executeJavaScript.
const { Script } = require('node:vm');
const smokeWrites = [{ path: base + '/file', method: 'PUT', body: '{"overwrite":true}' },
  { path: base + '/terminals/t', method: 'DELETE', body: '{}' }];
require('./forge3-checks.cjs')({ js: async code => { new Script(code); return true; },
  waitFor: async code => { new Script(code); }, capture: async () => {},
  writes: { length: 0, slice: () => smokeWrites }, demoState: {}, label: 'syntax', forgeSessionId: 'fs1', width: 1440,
}).then(() => console.log('Forge smoke expressions parse.')).catch(error => { console.error(error); process.exitCode = 1; });
