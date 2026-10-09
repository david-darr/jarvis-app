import { api, confirmDialog } from './api.js';
import { getShortcutGroups, shortcutHelp } from './shortcuts.js';

function navigate(tab, options = {}) {
  document.dispatchEvent(new CustomEvent('jarvis:navigate', { detail: { tab, ...options } }));
}
function toolCommand(tab, label) {
  return { category: 'Go to', help: `Open ${label}.`, usage: `/${tab}`, handler: () => {
    navigate(tab); return `Opened ${label}.`;
  } };
}
const NEED_CHAT = 'Open or create a chat first, then try this command again.';
export const NO_MODEL = 'No model chosen. Pick one from the menu above the chat box, or type /setup.';
function chatAction(action, output) {
  return async (_args, ctx) => {
    if (!ctx.sessionId()) return NEED_CHAT;
    return (await ctx[action]()) || output;
  };
}

export const COMMANDS = {
  help: {
    category: 'Getting started', help: 'See commands and keyboard shortcuts.', usage: '/help [command]', example: '/help compact',
    details: 'Type /help for the full list, or add a command name to learn more about it.',
    handler: (args, ctx) => renderHelp(args[0], ctx),
  },
  demo: {
    category: 'Getting started', help: 'Read a short tour of Kairos.', usage: '/demo',
    handler: (_args, ctx) => [
      'A quick tour of Kairos:',
      'Chats: ask questions, attach files, choose a model, and keep separate conversations.',
      'Agents: give a helper ongoing work and review its questions and results.',
      ...(ctx.isAdmin?.() ? ['Forge: work on software projects with an agent.'] : []),
      'Notes: keep your task list, with due dates that also appear on Calendar.',
      'Tasks: schedule jobs for an agent to run.',
      'Calendar: see events and due dates together.',
      'Email: connect an account to read and compose messages.',
      'Library: find documents, chat files and notes in your Vault folder.',
      'Tool Store: add saved Skills and connected tools.',
      'Cookbook: download and run a model on this computer.',
      'Model setup: type /setup for help connecting your first model.',
      'Computer use: ask the agent to open a website, then type /computer to see its computer. You can take over when needed.',
    ].map((line, index) => index ? `- ${line}` : line + '\n').join('\n'),
  },
  setup: {
    category: 'Getting started', help: 'Open the guide to connecting a model.', usage: '/setup',
    details: 'Choose a Claude or ChatGPT subscription, an API key (a private code from a model provider), or a model on this computer.',
    handler: (_args, ctx) => { ctx.setup(); return 'Opened the model setup guide.'; },
  },
  shortcuts: {
    category: 'Getting started', help: 'Open the keyboard shortcuts in Settings.', usage: '/shortcuts',
    handler: () => { navigate('settings', { section: 'shortcuts' }); return 'Opened Settings > Shortcuts.'; },
  },
  new: {
    category: 'Chats', help: 'Create a new chat.', usage: '/new',
    handler: async (_args, ctx) => { await ctx.createSession(); return 'New chat created.'; },
  },
  rename: {
    category: 'Chats', help: 'Give the current chat a new title.', usage: '/rename New title', example: '/rename Weekend plans',
    handler: async (args, ctx) => {
      if (!ctx.sessionId()) return NEED_CHAT;
      const title = args.join(' ').trim();
      if (!title) return 'Usage: /rename New title';
      await api(`/api/sessions/${ctx.sessionId()}`, { method: 'PATCH', body: JSON.stringify({ title }) });
      await ctx.refreshSessions(); return `Renamed to "${title}".`;
    },
  },
  star: {
    category: 'Chats', help: 'Star the current chat or remove its star.', usage: '/star',
    handler: async (_args, ctx) => {
      if (!ctx.sessionId()) return NEED_CHAT;
      const session = await api(`/api/sessions/${ctx.sessionId()}`);
      await api(`/api/sessions/${ctx.sessionId()}/star`, { method: 'POST', body: JSON.stringify({ starred: !session.starred }) });
      await ctx.refreshSessions(); return session.starred ? 'Star removed.' : 'Chat starred.';
    },
  },
  delete: {
    category: 'Chats', help: 'Delete the current chat after you confirm.', usage: '/delete',
    details: 'Kairos asks you to confirm first. Deleting a chat removes its full message history and cannot be undone.',
    handler: async (_args, ctx) => {
      if (!ctx.sessionId()) return NEED_CHAT;
      const id = ctx.sessionId();
      const ok = await confirmDialog({ title: 'Delete this chat?', message: "This conversation and its full message history will be permanently deleted. This can't be undone.", confirmLabel: 'Delete chat' });
      if (!ok) return 'Cancelled. Your chat was kept.';
      await api(`/api/sessions/${id}`, { method: 'DELETE' });
      ctx.onCurrentSessionDeleted(); await ctx.refreshSessions(); return 'Chat deleted.';
    },
  },
  terminal: {
    category: 'Chats', help: 'Switch between standard and terminal chat styles.', usage: '/terminal',
    details: 'Terminal style gives the chat a text-window look. Type /terminal again to switch back.',
    handler: async () => {
      const { getAppearance, updateAppearance } = await import('./appearance.js');
      const next = getAppearance().chatStyle === 'terminal' ? 'standard' : 'terminal';
      updateAppearance({ chatStyle: next });
      return next === 'terminal' ? 'Terminal style on. Type /terminal again to switch back.' : 'Standard style on.';
    },
  },
  compact: {
    category: 'Chats', help: 'Summarize earlier messages to make room in this chat.', usage: '/compact',
    details: 'Uses the same Compact action as the + menu. Earlier messages are summarized for the model, and the full conversation stays visible.',
    handler: chatAction('compact', 'Chat compacted. The full conversation is still above.'),
  },
  find: {
    category: 'Chats', help: 'Open find to search this chat.', usage: '/find',
    details: 'Enter a search in the find bar. Enter moves to the next match, Shift + Enter to the previous one, and Escape closes the bar.',
    handler: chatAction('find', 'Opened find in chat.'),
  },
  notes: toolCommand('notes', 'Notes'),
  tasks: toolCommand('tasks', 'Tasks'),
  calendar: toolCommand('calendar', 'Calendar'),
  email: toolCommand('email', 'Email'),
  vault: {
    category: 'Go to', help: 'Open your Vault notes in Library.', usage: '/vault',
    handler: () => { navigate('library', { section: 'vault' }); return 'Opened Vault in Library.'; },
  },
  store: {
    category: 'Go to', help: 'Open Tool Store to browse Skills and tools.', usage: '/store',
    handler: () => { navigate('tool-store'); return 'Opened Tool Store.'; },
  },
  cookbook: toolCommand('cookbook', 'Cookbook'),
  settings: {
    category: 'Go to', help: 'Open Settings.', usage: '/settings',
    handler: () => { navigate('settings'); return 'Opened Settings.'; },
  },
  home: toolCommand('home', 'Home'),
  library: toolCommand('library', 'Library'),
  agents: toolCommand('agents', 'Agents'),
  forge: {
    category: 'Go to', help: 'Open Forge to work on software projects.', usage: '/forge', adminOnly: true,
    details: 'Forge is available to admins. It opens the same Forge view as the sidebar mode switch.',
    handler: async () => { const { switchMode } = await import('./app.js'); await switchMode('forge'); return 'Opened Forge.'; },
  },
  capture: {
    category: 'Agent', help: 'Capture a screen image to attach to your next message.', usage: '/capture',
    details: 'Opens the same screen capture as the + menu. Choose what to capture, then send the image with your next message.',
    handler: async (_args, ctx) => { await ctx.capture(); return 'Choose what to capture, then send it with your next message.'; },
  },
  computer: {
    category: 'Agent', help: "Show the computer used by this chat's agent.", usage: '/computer',
    details: 'Opens the same computer pane as the + menu. Ask the agent to open a website first if this chat has no computer yet.',
    handler: chatAction('computer', 'Opened the computer for this chat.'),
  },
  workspace: {
    category: 'Agent', help: "Clear this chat's chosen workspace folder.", usage: '/workspace clear',
    details: 'A workspace is the folder the agent uses for this chat. Use the + menu to choose a folder, or /workspace clear to remove it.',
    handler: async (args, ctx) => {
      if (!ctx.sessionId()) return NEED_CHAT;
      if (args[0] !== 'clear') return 'Usage: /workspace clear. Use the + menu to pick a folder.';
      await api(`/api/sessions/${ctx.sessionId()}/workspace`, { method: 'POST', body: JSON.stringify({ path: null }) });
      ctx.onWorkspaceCleared(); return 'Workspace cleared.';
    },
  },
  model: {
    category: 'Models', help: 'Show the model connection chosen for this chat.', usage: '/model',
    details: 'Shows the connection name and the exact model if you chose a different version for this chat. Use the menu above the chat box to change it.',
    handler: async (_args, ctx) => {
      if (!ctx.sessionId()) return NO_MODEL;
      const session = await api(`/api/sessions/${ctx.sessionId()}`);
      if (!session.model_endpoint_id) return NO_MODEL;
      const connection = (await api('/api/models/choices')).find(item => item.id === session.model_endpoint_id);
      if (!connection) return NO_MODEL;
      const model = session.model_override ?? connection.model;
      return `Model connection: ${connection.name}.${model ? `\nModel: ${model}` : "\nModel: the connection's default."}`;
    },
  },
  models: {
    category: 'Models', help: 'List your saved model connections.', usage: '/models',
    details: 'Choose a connection from the menu above the chat box. Type /setup for help adding one.',
    handler: async () => {
      const endpoints = await api('/api/models/choices');
      if (!endpoints.length) return 'No model connections yet. Type /setup to add one.';
      return endpoints.map(item => `${item.name}: ${item.model || "the connection's default model"}`).join('\n') + '\nPick one from the menu above the chat box, or type /setup to add another.';
    },
  },
  memory: {
    category: 'Memory', help: 'List saved Skills that Kairos can use again.', usage: '/memory list',
    details: 'Skills are saved instructions and knowledge for recurring work. This lists the same saved Skills as /skills list.',
    handler: () => listSkills(),
  },
  skills: {
    category: 'Memory', help: 'List saved Skills or read one by name.', usage: '/skills list | /skills view name', example: '/skills view meeting-notes',
    details: 'Type /skills list to see saved Skills, then /skills view followed by a name to read its instructions.',
    handler: async args => {
      if (args[0] === 'view' && args[1]) {
        const skill = await api(`/api/skills/${encodeURIComponent(args[1])}`).catch(() => null);
        if (!skill) return `No skill named "${args[1]}".`;
        return `${skill.slug}\n${skill.description}\n\n${skill.body}`;
      }
      return listSkills();
    },
  },
};

async function listSkills() {
  const skills = await api('/api/skills');
  if (!skills.length) return 'No Skills saved yet. Add one in Tool Store.';
  return skills.map(skill => `/${skill.slug}${skill.description ? ': ' + skill.description : ''}`).join('\n');
}
export const CATEGORY_ORDER = ['Getting started', 'Chats', 'Go to', 'Agent', 'Models', 'Memory'];
export function visibleCommands(ctx = {}) {
  return Object.entries(COMMANDS).filter(([, command]) => !command.adminOnly || ctx.isAdmin?.());
}
export async function renderHelp(name, ctx = {}) {
  const commands = visibleCommands(ctx);
  if (name) {
    const normalized = name.replace(/^\//, '').toLowerCase();
    const command = commands.find(([key]) => key === normalized)?.[1];
    if (!command) return `Unknown command: /${normalized}. Try /help.`;
    return [`/${normalized}: ${command.help}`, `Usage: ${command.usage}`,
      `Example: ${command.example || command.usage}`, command.details || command.help].join('\n\n');
  }
  const lines = [];
  for (const category of CATEGORY_ORDER) {
    lines.push(`**${category}:**`, '');
    for (const [name, command] of commands.filter(([, command]) => command.category === category)) {
      lines.push(`- /${name}: ${command.help} Example: ${command.example || command.usage}`);
    }
    lines.push('');
  }
  lines.push(shortcutHelp(await getShortcutGroups()), '', 'Type / to see commands as you type.');
  return lines.join('\n');
}
export async function runSlashCommand(text, ctx = {}) {
  if (!text.startsWith('/')) return { handled: false };
  const [token, ...args] = text.slice(1).trim().split(/\s+/);
  const command = COMMANDS[(token || '').toLowerCase()];
  if (!command || (command.adminOnly && !ctx.isAdmin?.())) return { handled: true, output: `Unknown command: /${token}. Try /help.` };
  try { return { handled: true, output: await command.handler(args, ctx) || 'Done.' }; }
  catch (error) { return { handled: true, output: `Couldn't run /${token}: ${error.message}` }; }
}

let pickerNumber = 0;
export function mountSlashSuggestions(input, inputTop, ctx) {
  const list = document.createElement('div');
  list.className = 'chat-reference-picker slash-command-picker';
  list.id = `slash-command-picker-${++pickerNumber}`;
  list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', 'Slash commands');
  list.hidden = true; inputTop.append(list);
  input.setAttribute('aria-controls', [input.getAttribute('aria-controls'), list.id].filter(Boolean).join(' '));
  let results = [], active = 0;
  const hide = () => {
    const wasOpen = !list.hidden;
    list.hidden = true; list.replaceChildren();
    if (input.getAttribute('aria-activedescendant')?.startsWith(list.id)) input.removeAttribute('aria-activedescendant');
    if (wasOpen) input.setAttribute('aria-expanded', 'false');
  };
  const choose = name => {
    input.value = `/${name} `; input.setSelectionRange(input.value.length, input.value.length);
    hide(); input.dispatchEvent(new Event('input', { bubbles: true })); input.focus();
  };
  const paint = () => {
    list.replaceChildren();
    results.forEach(([name, command], index) => {
      const option = document.createElement('button'); option.type = 'button'; option.tabIndex = -1;
      option.id = `${list.id}-option-${index}`; option.className = 'chat-reference-option slash-command-option';
      option.setAttribute('role', 'option'); option.setAttribute('aria-selected', String(index === active));
      for (const [className, text] of [['chat-reference-kind', `/${name}`], ['chat-reference-name', command.help], ['chat-reference-detail', command.usage]]) {
        const span = document.createElement('span'); span.className = className; span.textContent = text; option.append(span);
      }
      option.addEventListener('mousedown', event => event.preventDefault()); option.addEventListener('click', () => choose(name)); list.append(option);
    });
    list.hidden = false; input.setAttribute('aria-expanded', 'true');
    input.setAttribute('aria-activedescendant', `${list.id}-option-${active}`);
  };
  const update = () => {
    const match = /^\/([^\s/]*)$/.exec(input.value);
    if (!match || input.selectionStart !== input.selectionEnd) { hide(); return; }
    results = visibleCommands(ctx).filter(([name]) => name.startsWith(match[1].toLowerCase())); active = 0;
    if (!results.length) { hide(); return; } paint();
  };
  const onKey = event => {
    if (list.hidden || event.isComposing) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); hide(); }
    else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); event.stopImmediatePropagation();
      active = (active + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
      paint(); list.children[active]?.scrollIntoView({ block: 'nearest' });
    } else if (event.key === 'Enter' && !event.shiftKey && input.value.slice(1).toLowerCase() === results[active][0]) {
      hide(); // Already typed in full: let Enter send it, so /new runs on the first Enter.
    } else if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
      event.preventDefault(); event.stopImmediatePropagation(); choose(results[active][0]);
    }
  };
  const outside = event => { if (!inputTop.contains(event.target)) hide(); };
  input.addEventListener('input', update); input.addEventListener('click', update); input.addEventListener('keydown', onKey, true);
  document.addEventListener('pointerdown', outside);
  return { hide, update, dispose() {
    hide(); input.removeEventListener('input', update); input.removeEventListener('click', update); input.removeEventListener('keydown', onKey, true);
    document.removeEventListener('pointerdown', outside);
    const remaining = (input.getAttribute('aria-controls') || '').split(' ').filter(id => id !== list.id).join(' ');
    if (remaining) input.setAttribute('aria-controls', remaining); else input.removeAttribute('aria-controls'); list.remove();
  } };
}
