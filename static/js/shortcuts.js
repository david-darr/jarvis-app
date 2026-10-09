// One list for Settings and slash help. Reading the desktop preference never writes it.
export const DEFAULT_QUICK_ENTRY = 'CommandOrControl+Shift+6';

export function isMac(platform = navigator.userAgentData?.platform || navigator.platform || '') {
  return /mac/i.test(platform);
}

export function formatShortcut(value, mac = isMac()) {
  const names = { CommandOrControl: mac ? '⌘' : 'Ctrl', CmdOrCtrl: mac ? '⌘' : 'Ctrl',
    Command: '⌘', Cmd: '⌘', Control: 'Ctrl', Ctrl: 'Ctrl', Option: 'Alt', Super: mac ? '⌘' : 'Super' };
  return value.split('+').map(key => names[key] || key).join(' + ');
}

// null = not known yet (or no desktop bridge): show the default.
export async function readQuickEntry() {
  try { return (await window.jarvis?.screenGrab?.shortcut?.()) ?? null; } catch { return null; /* Browser or unavailable bridge. */ }
}

export async function getShortcutGroups() {
  return shortcutGroups(await readQuickEntry());
}

// Synchronous, so a panel can draw at once and never sits empty.
export function shortcutGroups(quickEntry = null) {
  const quickNote = quickEntry === '' ? 'This development copy has no global shortcut. Open Quick Entry from the tray.'
    : quickEntry ? 'You can change this in Settings.' : 'Default shortcut. You can change it in Settings.';
  const shortcut = (keys, description, details = '') => ({ keys, description, details });
  return [
    { title: 'Anywhere', items: [
      shortcut(formatShortcut('CommandOrControl+K'), 'Open the command palette'),
      shortcut(quickEntry === '' ? 'Tray menu' : formatShortcut(quickEntry || DEFAULT_QUICK_ENTRY), 'Open Quick Entry', quickNote),
      shortcut('Escape', 'Close the current menu or panel'),
    ] },
    { title: 'Composer', items: [
      shortcut('Enter', 'Send your message'),
      shortcut('Shift + Enter', 'Start a new line'),
      shortcut('/', 'See slash commands as you type'),
      shortcut('/help', 'Read command help', 'Use /help command for more detail, for example /help compact.'),
      shortcut('@', 'Choose a file, note, chat or agent to mention'),
      shortcut('↑ / ↓, Enter / Tab, Escape', 'Move through suggestions, insert a choice, or close the list'),
      shortcut('↑ / ↓', 'Recall earlier messages when the composer is empty'),
    ] },
    { title: 'Chat', items: [
      shortcut(formatShortcut('CommandOrControl+F'), 'Find text in the current chat'),
      shortcut('Enter / Shift + Enter', 'Go to the next or previous find match'),
      shortcut('Right-click a chat', 'Rename, star or delete the chat'),
    ] },
    { title: 'Documents', items: [
      shortcut(formatShortcut('CommandOrControl') + ' + click a chat file', 'Open the file in the background'),
      shortcut('← / → on the document divider', 'Resize the document pane', 'Focus the divider first.'),
    ] },
  ];
}

export function shortcutHelp(groups) {
  return ['**Keyboard shortcuts:**', '', ...groups.flatMap(group => [
    `**${group.title}:**`, '', ...group.items.map(item => `- ${item.keys}: ${item.description}.${item.details ? ' ' + item.details : ''}`), '',
  ])].join('\n');
}
