// Run: node --experimental-vm-modules scripts/test-slash-help.cjs
// Exercise the real modules offline with only app/API boundaries substituted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const calls = [], navigation = [];
let session = { model_endpoint_id: null }, connections = [], confirmed = false, style = 'standard';
const browser = {};
const platform = { platform: 'Win32' };
const context = vm.createContext({ navigator: platform, window: browser,
  document: { dispatchEvent: event => navigation.push(event.detail) },
  CustomEvent: class { constructor(_type, { detail }) { this.detail = detail; } },
});
const fakeApi = async (route, options) => {
  calls.push({ route, options });
  if (options) return { ok: true };
  if (route === '/api/models/choices') return connections;
  if (route === '/api/skills') return [{ slug: 'meeting-notes', description: 'Keep meeting notes.' }];
  if (route === '/api/skills/meeting-notes') return { slug: 'meeting-notes', description: 'Keep meeting notes.', body: 'Write the decisions.' };
  if (route === '/api/sessions/s1') return session;
  throw new Error('Unexpected route: ' + route);
};
const mocks = {
  'api.js': { api: fakeApi, confirmDialog: async () => confirmed },
  'appearance.js': { getAppearance: () => ({ chatStyle: style }), updateAppearance: value => { style = value.chatStyle; } },
  'app.js': { switchMode: async mode => navigation.push(mode) },
};
const modules = new Map();
async function load(name) {
  if (!modules.has(name)) {
    const module = mocks[name] ? new vm.SyntheticModule(Object.keys(mocks[name]), function () {
      for (const [key, value] of Object.entries(mocks[name])) this.setExport(key, value);
    }, { context }) : new vm.SourceTextModule(fs.readFileSync(path.join(root, 'static/js', name), 'utf8'), {
      context, identifier: name, importModuleDynamically: async specifier => {
        const imported = await load(path.basename(specifier)); await imported.evaluate(); return imported;
      },
    });
    modules.set(name, module);
    await module.link(specifier => load(path.basename(specifier)));
  }
  return modules.get(name);
}

(async () => {
  const module = await load('slashCommands.js'); await module.evaluate();
  const { COMMANDS, CATEGORY_ORDER, runSlashCommand: run, renderHelp, visibleCommands, NO_MODEL } = module.namespace;
  const shortcuts = modules.get('shortcuts.js').namespace;
  let sid = null, refreshed = 0, deleted = 0, cleared = 0, created = 0;
  const actions = [];
  const ctx = { sessionId: () => sid, isAdmin: () => false,
    refreshSessions: async () => { refreshed++; }, onCurrentSessionDeleted: () => { deleted++; },
    onWorkspaceCleared: () => { cleared++; }, createSession: async () => { created++; },
    ...Object.fromEntries(['setup', 'compact', 'capture', 'computer', 'find'].map(name => [name, () => { actions.push(name); }])),
  };
  const expected = 'help demo setup shortcuts new rename star delete terminal compact find notes tasks calendar email vault store cookbook settings home library agents forge capture computer workspace model models memory skills'.split(' ');
  assert.deepEqual(Object.keys(COMMANDS), expected);
  for (const [name, command] of Object.entries(COMMANDS)) {
    assert.ok(CATEGORY_ORDER.includes(command.category), name);
    assert.ok(command.help.endsWith('.') && !command.help.includes('—'), name);
    assert.ok(command.usage.startsWith('/' + name), name);
    assert.equal(typeof command.handler, 'function', name);
  }
  assert.equal((await run('hello', ctx)).handled, false);
  assert.equal((await run('/model', ctx)).output, NO_MODEL);
  for (const name of ['rename', 'star', 'delete', 'compact', 'computer', 'find', 'workspace']) {
    assert.match((await run('/' + name, ctx)).output, /Open or create a chat first/);
  }
  await run('/capture', ctx); await run('/setup', ctx);
  assert.deepEqual(actions, ['capture', 'setup']);
  sid = 's1';
  assert.equal((await run('/model', ctx)).output, NO_MODEL);
  connections = [{ id: 'm1', name: 'My connection', model: 'configured-model' }]; session.model_endpoint_id = 'm1';
  assert.equal((await run('/model', ctx)).output, 'Model connection: My connection.\nModel: configured-model');
  session.model_override = 'exact-override';
  assert.equal((await run('/MODEL', ctx)).output, 'Model connection: My connection.\nModel: exact-override');
  session.model_override = '';
  assert.match((await run('/model', ctx)).output, /connection's default/);
  session.model_endpoint_id = 'removed'; assert.equal((await run('/model', ctx)).output, NO_MODEL);
  connections = []; assert.match((await run('/models', ctx)).output, /\/setup/);
  for (const name of ['compact', 'computer', 'find']) await run('/' + name, ctx);
  assert.deepEqual(actions, ['capture', 'setup', 'compact', 'computer', 'find']);
  assert.ok(!visibleCommands(ctx).some(([name]) => name === 'forge'));
  assert.match((await run('/forge', ctx)).output, /Unknown command/);
  const help = await renderHelp(null, ctx);
  for (const category of CATEGORY_ORDER) assert.ok(help.includes(category + ':'));
  assert.ok(!help.includes('/forge') && !help.includes('—') && help.endsWith('Type / to see commands as you type.'));
  assert.ok(help.includes('Example: /rename Weekend plans') && help.includes('Keyboard shortcuts:'));
  assert.match(await renderHelp('/compact', ctx), /Usage: \/compact[\s\S]*full conversation stays visible/);
  assert.match(await renderHelp('forge', ctx), /Unknown command/);
  const admin = { ...ctx, isAdmin: () => true };
  assert.ok((await renderHelp(null, admin)).includes('/forge'));
  await run('/forge', admin); assert.equal(navigation.at(-1), 'forge');
  assert.ok(!(await run('/demo', admin)).output.includes('Odysseus'));
  for (const text of ['Chats:', 'Agents:', 'Forge:', 'Cookbook:', 'Model setup:', 'Computer use:']) assert.ok((await run('/demo', admin)).output.includes(text));
  await run('/rename A new title', ctx);
  assert.equal(JSON.parse(calls.at(-1).options.body).title, 'A new title');
  session.starred = false; await run('/star', ctx); assert.equal(JSON.parse(calls.at(-1).options.body).starred, true);
  await run('/workspace clear', ctx); assert.equal(cleared, 1);
  await run('/new', ctx); assert.equal(created, 1);
  const beforeCancel = calls.length; await run('/delete', ctx); assert.equal(calls.length, beforeCancel);
  confirmed = true; await run('/delete', ctx); assert.equal(deleted, 1); assert.equal(refreshed, 3);
  await run('/terminal', ctx); assert.equal(style, 'terminal'); await run('/terminal', ctx); assert.equal(style, 'standard');
  assert.match((await run('/memory list', ctx)).output, /meeting-notes/);
  assert.match((await run('/skills view meeting-notes', ctx)).output, /Write the decisions/);
  await run('/shortcuts', ctx); assert.equal(navigation.at(-1).section, 'shortcuts');
  await run('/agents', ctx); assert.equal(navigation.at(-1).tab, 'agents');
  for (const [os, mod] of [['MacIntel', '⌘'], ['Win32', 'Ctrl']]) {
    platform.platform = os;
    const groups = await shortcuts.getShortcutGroups();
    assert.deepEqual(Array.from(groups, group => group.title), ['Anywhere', 'Composer', 'Chat', 'Documents']);
    assert.equal(groups[0].items[0].keys, mod + ' + K');
    assert.equal(groups[0].items[1].keys, mod + ' + Shift + 6');
    assert.match(groups[0].items[1].details, /Default shortcut/);
    browser.jarvis = { screenGrab: { shortcut: async () => 'CommandOrControl+Alt+9' } };
    assert.equal((await shortcuts.getShortcutGroups())[0].items[1].keys, mod + ' + Alt + 9');
    browser.jarvis.screenGrab.shortcut = async () => '';
    assert.equal((await shortcuts.getShortcutGroups())[0].items[1].keys, 'Tray menu');
    browser.jarvis.screenGrab.shortcut = async () => { throw new Error('Unavailable'); };
    assert.equal((await shortcuts.getShortcutGroups())[0].items[1].keys, mod + ' + Shift + 6');
    delete browser.jarvis;
  }
  platform.userAgentData = { platform: 'macOS' }; assert.equal(shortcuts.isMac(), true);
  // The shared fixtures retain an exact override for slash help after a picker save.
  const state = {};
  const fixtures = require('../demo/fixtures.js')({ state });
  fixtures.mutate('/api/sessions/s1/model', 'POST', { model_endpoint_id: 'm1', model_override: 'exact-model-id' });
  assert.equal(fixtures.fixture(new URL('http://demo/api/sessions/s1')).model_override, 'exact-model-id');
  console.log('PASS: command metadata, help/admin access, model choices/overrides, shared actions, existing commands, platform shortcuts and fixtures.');
})().catch(error => { console.error(error); process.exitCode = 1; });
