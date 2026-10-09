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
import { mountChatReferences } from './chatReferences.js';
import { renderAgentHandoff, watchAgentHandoffs } from './agentHandoff.js';

const CLI_KINDS = ['claude_cli', 'codex_cli'];
const queues = new Map();
const pausedQueues = new Set();

// options:
//   sessionId     the chat to show, or null to create it on first send
//   createSession async () => id, used when sessionId is null
//   title         the chat's name, used for the in-flight indicator
//   placeholder, emptyTitle, emptyText
//   modelPicker   show connection and model choices for this chat
//   openInChats   show a button that opens the chat in Chats
//   onTurnEnd     called once after each reply finishes
//   initialMessage sends the first task after loading a new session
//   queue         editable follow-ups; paused after Stop or leaving the view
//   readOnly      show history with sending disabled
//   onModelChange called after saving a model connection
// Returns a cleanup function.
export async function mountSessionChat(host, options = {}) {
  let sessionId = options.sessionId || null;
  let session = null;
  let unsubscribe = () => {};
  let shownPermission = null;
  let disposed = false, submitting = false;
  let modelSaving = false, modelControls = [];
  const label = options.title || 'Chat';
  // Forge supplies document rendering/chrome; transport, queues and model
  // changes remain the same controller used by the embedded chats.
  const transcript = options.transcript;

  const messages = transcript?.messages || el('div', { class: 'side-messages session-chat-messages', role: 'log', 'aria-label': label });
  const input = el('textarea', { class: 'side-chat-input', rows: '1', placeholder: options.placeholder || 'Message Kairos', 'aria-label': options.placeholder || 'Message Kairos' });
  const inputTop = el('div', { class: 'side-chat-input-top' }, [input]);
  const references = transcript ? { getSelected: () => [], clear() {}, dispose() {} } : mountChatReferences(input, inputTop, () => sessionId);
  const send = el('button', { type: 'button', class: 'side-chat-send btn', text: 'Send' });
  const queueButton = el('button', { type: 'button', class: 'btn quiet', text: 'Queue', hidden: true });
  const queueList = el('div', { class: 'session-chat-queue', 'aria-label': 'Queued messages', hidden: true });
  const tools = el('div', { class: 'session-chat-tools' });
  const status = el('span', { class: 'meta session-chat-status', role: 'status' });
  if (transcript) transcript.mount(host, { input, inputTop, send, queueButton, queueList, tools, status });
  else host.replaceChildren(el('div', { class: 'session-chat' }, [
    tools, messages, queueList, el('div', { class: 'side-chat-composer glass' }, [inputTop, queueButton, send]),
  ]));
  tools.hidden = !options.modelPicker && !options.openInChats;

  const card = (role, text, message = null) => {
    if (transcript) return transcript.card(role, text, message);
    const agent = message && renderAgentHandoff(message, sessionId);
    if (agent) return agent;
    const body = el('div', { class: 'msg-body' });
    renderMessageBody(body, text, sessionId, role === 'assistant');
    return el('div', { class: `msg ${role}` }, [body]);
  };
  const busy = () => !!sessionId && chatStream.getInFlight(sessionId)?.status === 'processing';
  const syncSend = () => {
    send.textContent = busy() ? 'Stop' : 'Send'; send.dataset.mode = busy() ? 'stop' : 'send';
    queueButton.hidden = !options.queue || !busy() || options.readOnly;
    input.disabled = send.disabled = !!options.readOnly;
    for (const control of modelControls) control.disabled = busy() || modelSaving || !!options.readOnly;
    transcript?.setBusy(busy());
  };
  const drawQueue = () => {
    const queued = queues.get(sessionId) || [];
    queueList.hidden = !queued.length;
    queueList.replaceChildren(...queued.map((item, index) => {
      const edit = el('textarea', { rows: '1', value: item.text, 'aria-label': `Queued message ${index + 1}` });
      edit.value = item.text;
      edit.oninput = () => { item.text = edit.value; };
      return el('div', { class: 'session-chat-queue-row' }, [edit, el('button', { class: 'btn quiet', text: 'Remove', onclick: () => { queued.splice(index, 1); drawQueue(); } })]);
    }));
    if (queued.length && pausedQueues.has(sessionId)) queueList.append(el('button', { class: 'btn quiet', text: 'Resume queue', disabled: busy() || !!options.readOnly, onclick: () => { pausedQueues.delete(sessionId); sendNext(); } }));
  };
  const sendNext = () => {
    if (disposed || !host.isConnected || busy() || pausedQueues.has(sessionId) || options.readOnly) return;
    const queued = queues.get(sessionId) || [];
    const next = queued.shift(); drawQueue();
    if (next) { if (next.text.trim()) runTurn(next.text.trim(), next.references); else sendNext(); }
  };
  const handoffs = transcript ? { refresh() {}, dispose() {} } : watchAgentHandoffs(messages, () => sessionId);

  const attach = (replyBody) => {
    unsubscribe();
    const id = sessionId;
    let finished = false;
    const paint = (entry) => {
      if (!host.isConnected || sessionId !== id) return;
      renderMessageBody(replyBody, entry.text || (entry.status === 'processing' ? '' : '(no reply)'), id, true);
      transcript?.paint(replyBody, entry);
      if (entry.permission && entry.permission.id !== shownPermission) {
        shownPermission = entry.permission.id;
        if (!transcript) showPermissionPrompt(entry.permission, () => { entry.permission = null; });
      }
      const replyCard = replyBody.closest('.msg');
      if (entry.status === 'done' && entry.handoffs) { replyCard?.remove(); handoffs.refresh(); }
      if (entry.status === 'failed' && replyCard && !replyCard.classList.contains('msg-failed')) {
        replyCard.classList.add('msg-failed');
        replyCard.append(el('div', { class: 'msg-interrupted', text: entry.error || 'Response interrupted. Try again.' }));
      }
      replyCard?.setAttribute('aria-busy', String(entry.status === 'processing'));
      syncSend();
      if (transcript) transcript.follow(); else messages.scrollTop = messages.scrollHeight;
      if (entry.status !== 'processing' && !finished) {
        finished = true;
        if (entry.status !== 'done') pausedQueues.add(id);
        Promise.resolve(options.onTurnEnd?.()).catch(() => {}).finally(() => { drawQueue(); sendNext(); });
      }
    };
    unsubscribe = chatStream.subscribe(id, paint);
    const current = chatStream.getInFlight(id);
    if (current) paint(current);
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
    messages.replaceChildren(...shown.map((m) => card(m.role, m.content, m)));
    if (transcript) await transcript.history(session);
    if (disposed) return;
    const current = transcript && chatStream.getInFlight(sessionId);
    // History may finish loading after the stream completes. Reattach the
    // retained entry, using a saved reply when present, so switches neither
    // duplicate this turn nor lose a reply that raced the history request.
    if (current) {
      const userIndex = shown.findLastIndex(m => m.role === 'user' && m.content === current.prompt && m.ts >= current.startedAt - 1);
      const savedIndex = shown.findIndex((m, index) => m.role === 'assistant' &&
        (current.runId ? m.run_id === current.runId : userIndex >= 0 && index === userIndex + 1));
      if (savedIndex >= 0) attach(messages.children[savedIndex].querySelector('.forge-prose'));
      else {
        if (userIndex < 0 && current.prompt) messages.append(card('user', current.prompt));
        const reply = card('assistant', ''); messages.append(reply); attach(reply.querySelector('.forge-prose'));
      }
    } else if (busy()) {
      const reply = card('assistant', '');
      messages.append(reply);
      attach(reply.querySelector('.msg-body, .forge-prose'));
    }
    if (!shown.length && !busy() && !current) showEmpty();
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
      modelControls = [connection, variantNode]; syncSend();
    };
    const save = async (override) => {
      if (busy() || options.readOnly) { status.textContent = 'Wait for the current reply before changing the model.'; return; }
      if (modelSaving) return;
      modelSaving = true; syncSend();
      try {
        await api(`/api/sessions/${sessionId}/model`, { method: 'POST',
          body: JSON.stringify({ model_endpoint_id: connection.value || null, model_override: override }) });
        status.textContent = '';
        current.model_endpoint_id = connection.value || null; current.model_override = override;
        options.onModelChange?.(endpoints.find(e => e.id === connection.value));
      } catch (problem) { connection.value = current.model_endpoint_id || ''; variantNode.value = current.model_override || ''; status.textContent = problem.message; }
      finally { modelSaving = false; syncSend(); }
    };
    connection.addEventListener('change', async () => { await save(null); await loadVariants(null); });
    tools.prepend(el('div', { class: 'session-chat-models', role: 'group', 'aria-label': 'Model for this chat' }, [connection, variantNode]));
    modelControls = [connection, variantNode]; syncSend();
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

  const runTurn = (text, selected = []) => {
    messages.querySelector('.agent-chat-empty')?.remove();
    messages.append(card('user', text));
    const reply = card('assistant', ''); messages.append(reply);
    try { chatStream.startTurn(sessionId, session?.title || label, text, [], selected); }
    catch (error) { toast(error.message, 'error'); reply.remove(); pausedQueues.add(sessionId); return; }
    attach(reply.querySelector('.msg-body, .forge-prose')); syncSend(); messages.scrollTop = messages.scrollHeight;
  };
  const submit = async ({ fromKeyboard = false, enqueue = false } = {}) => {
    if (options.readOnly || submitting || disposed) return;
    if (busy()) {
      if (options.queue && (fromKeyboard || enqueue)) {
        if (!input.value.trim()) return;
        const queued = queues.get(sessionId) || []; queues.set(sessionId, queued);
        queued.push({ text: input.value.trim(), references: references.getSelected() });
        input.value = ''; references.clear(); drawQueue();
      } else if (fromKeyboard) toast('A reply is still running. Wait for it, or press Stop.', 'error');
      else chatStream.stopTurn(sessionId);
      return;
    }
    const text = input.value.trim();
    if (!text) return;
    if (!sessionId) {
      submitting = true;
      try { sessionId = await options.createSession(); }
      catch (problem) { toast(problem.message, 'error'); return; }
      finally { submitting = false; }
      if (disposed) return;
    }
    const selected = references.getSelected();
    references.clear();
    input.value = ''; input.style.height = 'auto';
    runTurn(text, selected);
  };

  send.addEventListener('click', () => submit());
  queueButton.addEventListener('click', () => submit({ enqueue: true }));
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !event.defaultPrevented) { event.preventDefault(); submit({ fromKeyboard: true }); }
  });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 160) + 'px'; });

  await load();
  await drawModelPicker();
  syncSend();
  if (queues.get(sessionId)?.length && !busy()) pausedQueues.add(sessionId);
  drawQueue();
  if (options.initialMessage && session && !session.messages.length && !disposed && host.isConnected) { input.value = options.initialMessage; await submit(); }
  else if (options.initialMessage && !session) input.value = options.initialMessage;
  return () => { disposed = true; if (queues.get(sessionId)?.length) pausedQueues.add(sessionId); unsubscribe(); references.dispose(); handoffs.dispose(); transcript?.dispose(); };
}
