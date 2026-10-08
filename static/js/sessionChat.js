// One embedded conversation (2026-10-08): the Agents tab's chat pane and a
// tab's own chat (CRM task chats, School course chats) share this. It streams
// through the same chatStream.js registry as every other chat, so a reply
// keeps running if you leave the view and is there when you come back, and
// it shows the app's permission prompts. A tab that only read the stream's
// text would leave a turn waiting on a question nobody can see.
//
// Tabs import it as "/static/js/sessionChat.js"; mountSessionChat is part of
// the tab view contract (skill_templates/build-custom-tab/SKILL.md).
import { api, el, toast, customSelect } from './api.js';
import * as chatStream from './chatStream.js';
import { renderMessageBody } from './chatContent.js';
import { showPermissionPrompt } from './permissionPrompt.js';

const CLI_KINDS = ['claude_cli', 'codex_cli'];

// options:
//   sessionId     the chat to show, or null to create it on first send
//   createSession async () => id, used when sessionId is null
//   title         the chat's name, used for the in-flight indicator
//   placeholder, emptyTitle, emptyText
//   modelPicker   show connection and model choices for this chat
//   openInChats   show a button that opens the chat in Chats
//   onTurnEnd     called after each reply finishes
// Returns a cleanup function.
export async function mountSessionChat(host, options = {}) {
  let sessionId = options.sessionId || null;
  let session = null;
  let unsubscribe = () => {};
  let shownPermission = null;
  const label = options.title || 'Chat';

  const messages = el('div', { class: 'side-messages session-chat-messages', role: 'log', 'aria-label': label });
  const input = el('textarea', { class: 'side-chat-input', rows: '1', placeholder: options.placeholder || 'Message Kairos', 'aria-label': options.placeholder || 'Message Kairos' });
  const send = el('button', { type: 'button', class: 'side-chat-send btn', text: 'Send' });
  const tools = el('div', { class: 'session-chat-tools' });
  const status = el('span', { class: 'meta session-chat-status', role: 'status' });
  host.replaceChildren(el('div', { class: 'session-chat' }, [
    tools, messages, el('div', { class: 'side-chat-composer glass' }, [input, send]),
  ]));
  tools.hidden = !options.modelPicker && !options.openInChats;

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
      if (entry.status !== 'processing') options.onTurnEnd?.();
    });
  };

  const showEmpty = () => {
    messages.replaceChildren(el('div', { class: 'agent-chat-empty' }, [
      el('div', { class: 'title', text: options.emptyTitle || 'Start the conversation' }),
      el('div', { class: 'meta', text: options.emptyText || '' }),
    ]));
  };

  const load = async () => {
    if (!sessionId) { showEmpty(); return; }
    try { session = await api(`/api/sessions/${sessionId}`); }
    catch (problem) {
      messages.replaceChildren(el('div', { class: 'agent-chat-empty' }, [
        el('div', { class: 'title', text: "This chat couldn't load" }),
        el('div', { class: 'meta', text: `${problem.message}. Reopen it to try again.` }),
      ]));
      return;
    }
    if (!host.isConnected) return;
    const shown = session.messages.filter((m) => m.role === 'user' || m.role === 'assistant');
    messages.replaceChildren(...shown.map((m) => card(m.role, m.content)));
    if (busy()) {
      const reply = card('assistant', '');
      messages.append(reply);
      attach(reply.querySelector('.msg-body'));
    }
    if (!shown.length) showEmpty();
    messages.scrollTop = messages.scrollHeight;
  };

  // Connection and model for this chat, the same choices as the chat
  // composer's picker. Saved on the chat, so Chats shows the same choice.
  const drawModelPicker = async () => {
    if (!options.modelPicker || !sessionId) return;
    let endpoints;
    try { endpoints = await api('/api/models/choices'); }
    catch { status.textContent = "Models couldn't load."; return; }
    if (!host.isConnected) return;
    const current = session || {};
    const connection = customSelect({}, [el('option', { value: '', text: endpoints.length ? 'Choose a model' : 'Add a model in Settings' }),
      ...endpoints.map((e) => el('option', { value: e.id, text: e.name }))]);
    connection.value = current.model_endpoint_id || '';
    let variantNode = customSelect({}, [el('option', { value: '', text: 'Default model' })]);
    variantNode.hidden = true;
    const loadVariants = async (selected) => {
      const endpoint = endpoints.find((e) => e.id === connection.value);
      if (!endpoint || ![...CLI_KINDS, 'api'].includes(endpoint.kind)) { variantNode.hidden = true; return; }
      let choices = [];
      try { choices = await api(`/api/models/${encodeURIComponent(endpoint.id)}/catalog`); } catch { /* default only */ }
      if (!host.isConnected) return;
      const next = customSelect({}, [el('option', { value: '', text: 'Default model' }),
        ...choices.map((m) => el('option', { value: m.id, text: m.display_name || m.id }))]);
      next.value = choices.some((m) => m.id === selected) ? selected : '';
      next.addEventListener('change', () => save(next.value || null));
      variantNode.replaceWith(next);
      variantNode = next;
    };
    const save = async (override) => {
      try {
        await api(`/api/sessions/${sessionId}/model`, { method: 'POST',
          body: JSON.stringify({ model_endpoint_id: connection.value || null, model_override: override }) });
        status.textContent = '';
      } catch (problem) { status.textContent = problem.message; }
    };
    connection.addEventListener('change', async () => { await save(null); await loadVariants(null); });
    tools.prepend(el('div', { class: 'session-chat-models', role: 'group', 'aria-label': 'Model for this chat' }, [connection, variantNode]));
    await loadVariants(current.model_override || null);
  };

  if (options.openInChats) {
    tools.append(el('button', { type: 'button', class: 'btn quiet', text: 'Open in Chats', onclick: async () => {
      if (!sessionId) return;
      const app = await import('./app.js');
      await app.switchTab('chat');
      (await import('./views/chat.js')).openSessionById(sessionId);
    } }));
  }
  tools.append(status);

  const submit = async ({ fromKeyboard = false } = {}) => {
    if (busy()) {
      if (fromKeyboard) toast('A reply is still running. Wait for it, or press Stop.', 'error');
      else chatStream.stopTurn(sessionId);
      return;
    }
    const text = input.value.trim();
    if (!text) return;
    if (!sessionId) {
      try { sessionId = await options.createSession(); }
      catch (problem) { toast(problem.message, 'error'); return; }
    }
    input.value = ''; input.style.height = 'auto';
    messages.querySelector('.agent-chat-empty')?.remove();
    messages.append(card('user', text));
    const reply = card('assistant', '');
    messages.append(reply);
    try { chatStream.startTurn(sessionId, session?.title || label, text, []); }
    catch (error) { toast(error.message, 'error'); reply.remove(); return; }
    attach(reply.querySelector('.msg-body'));
    syncSend();
    messages.scrollTop = messages.scrollHeight;
  };

  send.addEventListener('click', () => submit());
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit({ fromKeyboard: true }); }
  });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 160) + 'px'; });

  await load();
  await drawModelPicker();
  syncSend();
  return () => unsubscribe();
}
