// Chatting with an agent inside the Agents tab (David's ask 2026-10-05):
// the agent's own conversations, kept out of the Chats list. A list of its
// chats beside one open conversation, streaming through the same
// chatStream.js registry as every other chat, so a reply keeps running if
// you leave the tab and is there when you come back. Modelled on
// sideChat.js, but an instance per mount rather than one module-wide pane.
import { api, el, toast, confirmDialog } from './api.js';
import * as chatStream from './chatStream.js';
import { renderMessageBody } from './chatContent.js';
import { showPermissionPrompt } from './permissionPrompt.js';

const LAST_KEY = 'jarvis:agent-chat:';

function remembered(agentId) {
  try { return localStorage.getItem(LAST_KEY + agentId); } catch { return null; }
}
function remember(agentId, sessionId) {
  try { sessionId ? localStorage.setItem(LAST_KEY + agentId, sessionId) : localStorage.removeItem(LAST_KEY + agentId); } catch { /* private mode */ }
}

export async function mountAgentChat(host, agent) {
  let sessionId = null;
  let session = null;
  let unsubscribe = () => {};
  let shownPermission = null;

  const list = el('div', { class: 'agent-chat-list', role: 'list', 'aria-label': `Chats with ${agent.name}` });
  const messages = el('div', { class: 'side-messages agent-chat-messages', role: 'log', 'aria-label': `Messages with ${agent.name}` });
  const input = el('textarea', { class: 'side-chat-input', rows: '1', placeholder: `Message ${agent.name}`, 'aria-label': `Message ${agent.name}` });
  const send = el('button', { type: 'button', class: 'side-chat-send btn', text: 'Send' });
  const newChat = el('button', { type: 'button', class: 'btn agent-chat-new', text: '+ New chat' });
  host.replaceChildren(el('div', { class: 'agent-chat' }, [
    el('aside', { class: 'agent-chat-side' }, [newChat, list]),
    el('section', { class: 'agent-chat-main' }, [messages, el('div', { class: 'side-chat-composer glass' }, [input, send])]),
  ]));

  const card = (role, text) => {
    const body = el('div', { class: 'msg-body' });
    renderMessageBody(body, text, sessionId, role === 'assistant');
    return el('div', { class: `msg ${role}` }, [body]);
  };
  const busy = () => !!sessionId && chatStream.getInFlight(sessionId)?.status === 'processing';
  const syncSend = () => { send.textContent = busy() ? 'Stop' : 'Send'; send.dataset.mode = busy() ? 'stop' : 'send'; };

  const attach = (replyBody) => {
    unsubscribe();
    const id = sessionId;
    unsubscribe = chatStream.subscribe(id, (entry) => {
      if (!host.isConnected || sessionId !== id) return;
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
      replyCard?.setAttribute('aria-busy', String(entry.status === 'processing'));
      syncSend();
      messages.scrollTop = messages.scrollHeight;
      if (entry.status !== 'processing') refreshList();
    });
  };

  const showEmpty = () => {
    messages.replaceChildren(el('div', { class: 'agent-chat-empty' }, [
      el('div', { class: 'title', text: `Talk with ${agent.name}` }),
      el('div', { class: 'meta', text: 'It knows its role, its notes and its tools. Anything it learns here it can keep in its memory.' }),
    ]));
  };

  const open = async (id) => {
    unsubscribe(); unsubscribe = () => {};
    shownPermission = null;
    try { session = await api(`/api/sessions/${id}`); }
    catch { remember(agent.id, null); sessionId = null; session = null; showEmpty(); refreshList(); return; }
    if (!host.isConnected) return;
    sessionId = id;
    remember(agent.id, id);
    messages.replaceChildren(...session.messages.filter((m) => m.role === 'user' || m.role === 'assistant').map((m) => card(m.role, m.content)));
    if (busy()) {
      const reply = card('assistant', '');
      messages.append(reply);
      attach(reply.querySelector('.msg-body'));
    }
    if (!session.messages.length) showEmpty();
    syncSend();
    messages.scrollTop = messages.scrollHeight;
    markActive();
  };

  const markActive = () => list.querySelectorAll('.agent-chat-item').forEach((item) => {
    item.classList.toggle('active', item.dataset.session === sessionId);
    item.setAttribute('aria-current', String(item.dataset.session === sessionId));
  });

  const refreshList = async () => {
    const chats = await api(`/api/sessions?agent_id=${encodeURIComponent(agent.id)}`).catch(() => []);
    if (!host.isConnected) return chats;
    list.replaceChildren(...chats.map((chat) => el('div', { class: 'agent-chat-item', role: 'listitem', 'data-session': chat.id }, [
      el('button', { type: 'button', class: 'agent-chat-open', onclick: () => open(chat.id) }, [
        el('span', { class: 'agent-chat-title', text: chat.title || 'Untitled' }),
        el('span', { class: 'meta', text: new Date(chat.updated_at * 1000).toLocaleDateString() }),
      ]),
      el('button', { type: 'button', class: 'input-icon-btn agent-chat-delete', 'aria-label': `Delete ${chat.title}`, title: 'Delete chat', text: '×',
        onclick: async () => {
          if (!await confirmDialog({ title: 'Delete this chat?', message: `"${chat.title}" will be deleted.`, confirmLabel: 'Delete chat' })) return;
          await api(`/api/sessions/${chat.id}`, { method: 'DELETE' }).catch((problem) => toast(problem.message, 'error'));
          if (chat.id === sessionId) { sessionId = null; session = null; remember(agent.id, null); showEmpty(); }
          refreshList();
        } }),
    ])));
    if (!chats.length) list.append(el('div', { class: 'meta agent-chat-none', text: 'No chats yet.' }));
    markActive();
    return chats;
  };

  const start = async () => {
    const created = await api(`/api/agents/${agent.id}/chat`, { method: 'POST' });
    await refreshList();
    await open(created.id);
    return created.id;
  };

  const submit = async ({ fromKeyboard = false } = {}) => {
    if (busy()) {
      if (fromKeyboard) toast('A reply is still running. Wait for it, or press Stop.', 'error');
      else chatStream.stopTurn(sessionId);
      return;
    }
    const text = input.value.trim();
    if (!text) return;
    if (!sessionId) {
      try { await start(); } catch (problem) { toast(problem.message, 'error'); return; }
    }
    input.value = ''; input.style.height = 'auto';
    messages.querySelector('.agent-chat-empty')?.remove();
    messages.append(card('user', text));
    const reply = card('assistant', '');
    messages.append(reply);
    try { chatStream.startTurn(sessionId, session?.title || `Chat with ${agent.name}`, text, []); }
    catch (error) { toast(error.message, 'error'); reply.remove(); return; }
    attach(reply.querySelector('.msg-body'));
    syncSend();
    messages.scrollTop = messages.scrollHeight;
  };

  newChat.addEventListener('click', async () => {
    try { await start(); input.focus(); } catch (problem) { toast(problem.message, 'error'); }
  });
  send.addEventListener('click', () => submit());
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit({ fromKeyboard: true }); }
  });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 160) + 'px'; });

  const chats = await refreshList();
  const last = remembered(agent.id);
  if (last && chats.some((c) => c.id === last)) await open(last);
  else if (chats.length) await open(chats[0].id);
  else showEmpty();
  syncSend();
  return () => unsubscribe();
}
