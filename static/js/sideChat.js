// Side-by-side chats (David's ask 2026-09-25, after Hermes Desktop's split
// panes): a second chat open next to the main one. Drag a chat from the list
// onto the right half of the chat area, or pick "Open side by side" from its
// menu. The pane has its own history, composer, stop button and permission
// prompts, and it streams through the same chatStream.js registry as the main
// chat, so both can be answering at once. It is deliberately lighter than the
// main chat: no attachments, workspace or model picker; "Make main" swaps the
// two when those are needed. Desktop widths only: under the breakpoint there
// is no room for two readable columns.
import { api, el, toast } from './api.js';
import * as chatStream from './chatStream.js';
import { renderMessageBody } from './chatContent.js';
import { showPermissionPrompt } from './permissionPrompt.js';

export const SESSION_MIME = 'application/x-jarvis-session';
const STORAGE_KEY = 'jarvis:side-chat';
const WIDTH_KEY = 'jarvis:side-chat-width';
const wide = matchMedia('(min-width: 1000px)');

let pane = null;          // the mounted pane element, or null
let layout = null;        // the .chat-layout it lives in
let hooks = {};           // { mainSessionId(), openInMain(id), onClosed() }
let sessionId = null;
let unsubscribe = () => {};
let shownPermission = null;

function remember(id) {
  try { id ? localStorage.setItem(STORAGE_KEY, id) : localStorage.removeItem(STORAGE_KEY); } catch { /* private mode */ }
}

export function sideChatSessionId() { return sessionId; }
export function canSplit() { return wide.matches; }

function card(role, text) {
  const body = el('div', { class: 'msg-body' });
  renderMessageBody(body, text, sessionId, role === 'assistant');
  return el('div', { class: `msg ${role}` }, [body]);
}

// Mount into a freshly rendered chat layout (chat.js's render runs on every
// visit to the Chats tab). Reopens the side chat that was open last time.
export function mountSideChat(chatLayout, options) {
  layout = chatLayout; hooks = options; pane = null; sessionId = null;
  unsubscribe(); unsubscribe = () => {};
  wireDropZone(chatLayout.querySelector('#chat-main'));
  let saved = null;
  try { saved = localStorage.getItem(STORAGE_KEY); } catch { /* private mode */ }
  if (saved && wide.matches) openSideChat(saved, { quiet: true });
}

export async function openSideChat(id, { quiet = false } = {}) {
  if (!layout?.isConnected) return;
  if (!wide.matches) { if (!quiet) toast('Side by side needs a wider window', 'error'); return; }
  if (id === hooks.mainSessionId?.()) { if (!quiet) toast('That chat is already open', 'error'); return; }
  let session;
  try { session = await api(`/api/sessions/${id}`); }
  catch { remember(null); if (!quiet) toast('That chat could not be opened', 'error'); return; }
  unsubscribe(); unsubscribe = () => {};
  shownPermission = null;
  sessionId = id;
  remember(id);
  build(session);
}

export function closeSideChat() {
  unsubscribe(); unsubscribe = () => {};
  sessionId = null;
  remember(null);
  pane?.remove(); pane = null;
  layout?.classList.remove('has-side-chat');
  hooks.onClosed?.();
}

function build(session) {
  pane?.remove();
  const messages = el('div', { class: 'side-messages', role: 'log', 'aria-label': `Messages in ${session.title}` });
  const input = el('textarea', { class: 'side-chat-input', rows: '1', placeholder: 'Message this chat', 'aria-label': `Message ${session.title}` });
  const send = el('button', { type: 'button', class: 'side-chat-send btn', text: 'Send' });
  const title = el('div', { class: 'side-chat-title', text: session.title, title: session.title });
  const makeMain = el('button', { type: 'button', class: 'btn side-chat-promote', text: 'Make main', title: 'Swap this chat with the main one' });
  const close = el('button', { type: 'button', class: 'input-icon-btn side-chat-close', 'aria-label': 'Close side chat', title: 'Close', text: '×' });
  const divider = el('div', { class: 'side-chat-divider', role: 'separator', 'aria-orientation': 'vertical', 'aria-label': 'Resize side chat', tabindex: '0' });
  pane = el('section', { class: 'side-chat', 'aria-label': `Side chat: ${session.title}`, 'data-session-id': session.id }, [
    divider,
    el('div', { class: 'side-chat-inner' }, [
      el('header', { class: 'side-chat-header' }, [title, el('div', { class: 'side-chat-actions' }, [makeMain, close])]),
      messages,
      el('div', { class: 'side-chat-composer glass' }, [input, send]),
    ]),
  ]);
  for (const msg of session.messages) messages.append(card(msg.role, msg.content));
  layout.append(pane);
  layout.classList.add('has-side-chat');
  applyWidth();
  messages.scrollTop = messages.scrollHeight;
  wireResize(divider);

  close.addEventListener('click', closeSideChat);
  makeMain.addEventListener('click', () => {
    // A swap: the main chat's conversation moves into this pane.
    const previousMain = hooks.mainSessionId?.();
    const id = sessionId;
    closeSideChat();
    hooks.openInMain?.(id);
    if (previousMain) setTimeout(() => openSideChat(previousMain, { quiet: true }), 0);
  });

  const busy = () => chatStream.getInFlight(sessionId)?.status === 'processing';
  const syncSend = () => { send.textContent = busy() ? 'Stop' : 'Send'; send.dataset.mode = busy() ? 'stop' : 'send'; };
  const attach = (replyBody) => {
    unsubscribe();
    const id = sessionId;
    unsubscribe = chatStream.subscribe(id, (entry) => {
      if (!pane?.isConnected || sessionId !== id) return;
      renderMessageBody(replyBody, entry.text || (entry.status === 'processing' ? '' : '(no reply)'), id, true);
      if (entry.permission && entry.permission.id !== shownPermission) {
        shownPermission = entry.permission.id;
        showPermissionPrompt(entry.permission, () => { entry.permission = null; });
      }
      const replyCard = replyBody.closest('.msg');
      if (entry.status === 'failed' && replyCard && !replyCard.classList.contains('msg-failed')) {
        replyCard.classList.add('msg-failed');
        replyCard.append(el('div', { class: 'msg-interrupted', text: entry.error || 'Response interrupted. Try again.' }));
      }
      replyBody.closest('.msg')?.setAttribute('aria-busy', String(entry.status === 'processing'));
      syncSend();
      messages.scrollTop = messages.scrollHeight;
    });
  };
  // Reattach to a reply still being written, as the main chat does.
  if (busy()) {
    const reply = card('assistant', '');
    messages.append(reply);
    attach(reply.querySelector('.msg-body'));
  }
  syncSend();

  const submit = () => {
    if (busy()) { chatStream.stopTurn(sessionId); return; }
    const text = input.value.trim();
    if (!text) return;
    if (text.startsWith('/')) { toast('Slash commands work in the main chat', 'error'); return; }
    input.value = ''; input.style.height = 'auto';
    messages.append(card('user', text));
    const reply = card('assistant', '');
    messages.append(reply);
    try { chatStream.startTurn(sessionId, session.title, text, []); }
    catch (error) { toast(error.message, 'error'); reply.remove(); return; }
    attach(reply.querySelector('.msg-body'));
    syncSend();
    messages.scrollTop = messages.scrollHeight;
  };
  send.addEventListener('click', submit);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit(); }
  });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 160) + 'px'; });
}

function applyWidth() {
  let width = 44;
  try { width = Number(localStorage.getItem(WIDTH_KEY)) || 44; } catch { /* private mode */ }
  layout?.style.setProperty('--side-chat-width', `${Math.max(28, Math.min(62, width))}%`);
}

function wireResize(divider) {
  const setWidth = (percent) => {
    const value = Math.max(28, Math.min(62, percent));
    layout.style.setProperty('--side-chat-width', `${value}%`);
    try { localStorage.setItem(WIDTH_KEY, String(Math.round(value))); } catch { /* private mode */ }
  };
  divider.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    divider.setPointerCapture(event.pointerId);
    const move = (e) => {
      const box = layout.getBoundingClientRect();
      setWidth(((box.right - e.clientX) / box.width) * 100);
    };
    const stop = () => { divider.removeEventListener('pointermove', move); divider.removeEventListener('pointerup', stop); };
    divider.addEventListener('pointermove', move);
    divider.addEventListener('pointerup', stop);
  });
  divider.addEventListener('keydown', (event) => {
    const current = parseFloat(layout.style.getPropertyValue('--side-chat-width')) || 44;
    if (event.key === 'ArrowLeft') { event.preventDefault(); setWidth(current + 3); }
    if (event.key === 'ArrowRight') { event.preventDefault(); setWidth(current - 3); }
  });
}

// Dropping a chat dragged from the list onto the right half of the chat
// area opens it here; the left half leaves it alone, so a drag that changes
// its mind has somewhere to go.
function wireDropZone(main) {
  if (!main) return;
  const hint = el('div', { class: 'side-chat-drop-hint', 'aria-hidden': 'true' }, [el('span', { text: 'Drop to open side by side' })]);
  main.append(hint);
  const carriesSession = (event) => [...(event.dataTransfer?.types || [])].includes(SESSION_MIME);
  const onRight = (event) => {
    const box = main.getBoundingClientRect();
    return event.clientX > box.left + box.width / 2;
  };
  main.addEventListener('dragover', (event) => {
    if (!carriesSession(event) || !wide.matches) return;
    const right = onRight(event);
    hint.classList.toggle('active', right);
    if (right) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; }
  });
  main.addEventListener('dragleave', (event) => { if (!main.contains(event.relatedTarget)) hint.classList.remove('active'); });
  main.addEventListener('drop', (event) => {
    hint.classList.remove('active');
    if (!carriesSession(event) || !onRight(event)) return;
    event.preventDefault();
    openSideChat(event.dataTransfer.getData(SESSION_MIME));
  });
}

// The main chat calls this when it opens a chat: one chat is never open in
// both panes at once.
export function mainOpened(id) {
  if (id && id === sessionId) closeSideChat();
}
