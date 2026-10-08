import { api, el } from './api.js';
import { closeArtifact, openArtifact } from './chatContent.js';
import { closeBrowser } from './browserPane.js';

let pane = null;

export function closeChatFiles() {
  if (!pane) return;
  document.removeEventListener('keydown', pane.onKey);
  pane.panel.remove();
  pane = null;
}

function sizeLabel(size) {
  if (size == null) return '';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadFiles(current) {
  const body = current.body;
  body.replaceChildren(el('div', { class: 'meta', text: 'Loading files…' }));
  try {
    const files = await api(`/api/chat/files?session_id=${encodeURIComponent(current.sessionId)}`);
    if (pane !== current) return;
    body.replaceChildren();
    if (!files.length) {
      body.append(el('div', { class: 'empty-state', text: 'No files in this chat yet' }));
      return;
    }
    for (const [origin, heading] of [['attachment', 'Sent to this chat'], ['created', 'Created by a model'], ['generated', 'Generated files']]) {
      const group = files.filter(file => file.origin === origin);
      if (!group.length) continue;
      body.append(el('h3', { class: 'chat-files-heading', text: heading }));
      for (const file of group) {
        const button = el('button', { type: 'button', class: 'chat-file-row', disabled: !file.exists,
          title: file.exists ? `Preview ${file.name}` : `${file.name} is no longer available` }, [
          el('span', { class: 'chat-file-name', text: file.name }),
          el('span', { class: 'meta', text: file.exists ? sizeLabel(file.size) : 'No longer available' }),
        ]);
        button.onclick = () => {
          closeChatFiles();
          openArtifact(current.sessionId, file.url, file.name, button);
        };
        body.append(button);
      }
    }
  } catch (error) {
    if (pane === current) body.replaceChildren(el('div', { class: 'meta', text: `Could not load files: ${error.message}` }));
  }
}

export function refreshChatFiles(sessionId) {
  if (pane?.sessionId === sessionId) loadFiles(pane);
}

export function openChatFiles(sessionId) {
  if (!sessionId) return;
  import('./chatComputerPane.js').then(m => m.closeChatComputer()).catch(() => {});
  closeChatFiles();
  closeArtifact();
  closeBrowser();
  const host = document.querySelector('.chat-layout');
  if (!host) return;
  const body = el('div', { class: 'chat-files-body' });
  const refresh = el('button', { type: 'button', class: 'btn quiet', text: 'Refresh' });
  const close = el('button', { type: 'button', class: 'btn quiet', text: '×', 'aria-label': 'Close chat files', onclick: closeChatFiles });
  const panel = el('aside', { class: 'artifact-panel chat-files-panel', role: 'region', 'aria-label': 'Files in this chat' }, [
    el('header', { class: 'artifact-header' }, [el('h2', { text: 'Chat files' }), el('div', { class: 'card-row' }, [refresh, close])]),
    body,
  ]);
  const onKey = event => { if (event.key === 'Escape') { event.preventDefault(); closeChatFiles(); } };
  pane = { panel, body, sessionId, onKey };
  refresh.onclick = () => loadFiles(pane);
  document.addEventListener('keydown', onKey);
  host.append(panel);
  loadFiles(pane);
}
