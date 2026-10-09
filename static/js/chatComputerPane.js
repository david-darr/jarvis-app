import { mountChatPane } from './chatPaneLayout.js';
import { el } from './api.js';
import { mountComputerPanel } from './computerPanel.js';

let pane = null;

export function closeChatComputer(dismissed = true) {
  if (!pane) return;
  if (dismissed) pane.onDismiss?.();
  document.removeEventListener('keydown', pane.onKey);
  pane.view.dispose();
  pane.layout.dispose(); pane.panel.remove();
  pane = null;
}

export function openChatComputer(sessionId, info, { onDismiss } = {}) {
  if (!info) return;
  if (pane?.sessionId === sessionId) { pane.view.update(info); return; }
  closeChatComputer(false);
  const host = document.querySelector('.chat-layout');
  if (!host) return;
  const body = el('div', { class: 'chat-computer-body' });
  const close = el('button', { type: 'button', class: 'btn quiet', text: '×',
    'aria-label': 'Close computer pane', onclick: () => closeChatComputer() });
  const panel = el('aside', { class: 'artifact-panel chat-computer-pane', role: 'region',
    'aria-label': 'Computer in this chat' }, [
    el('header', { class: 'artifact-header' }, [el('h2', { text: 'Computer' }), close]), body,
  ]);
  const onKey = event => { if (event.key === 'Escape' && event.target !== body.querySelector('.computer-stage')) closeChatComputer(); };
  const layout = mountChatPane(host, panel, closeChatComputer);
  pane = { panel, layout, sessionId, onKey, onDismiss, view: mountComputerPanel(body, `chat:${sessionId}`, info) };
  document.addEventListener('keydown', onKey);
}

export function updateChatComputer(sessionId, info) {
  if (pane?.sessionId === sessionId) pane.view.update(info);
}
