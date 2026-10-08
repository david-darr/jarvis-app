// Shared by every chat surface. Cards finish after the sending turn ends,
// so saved progress and replies are refreshed independently of that stream.
import { api, el, toast } from './api.js';
import { renderMessageBody } from './chatContent.js';
import * as chatStream from './chatStream.js';

const STATUS = { queued: 'Queued', working: 'Working', needs_you: 'Needs you', done: 'Done', failed: 'Failed' };

export function agentAvatar(name, color) {
  const avatar = el('span', { class: 'handoff-avatar', 'aria-hidden': 'true', text: (name || '?').slice(0, 1).toUpperCase() });
  // Agent colors come from the backend's fixed identity palette.
  if (color) avatar.style.backgroundColor = color;
  return avatar;
}

export function renderAgentHandoff(message, sessionId) {
  if (message.type !== 'handoff' && !message.agent_id) return null;
  const key = message.type === 'handoff' ? message.id : message.handoff_message_id;
  const card = el('div', { class: 'msg assistant agent-message', 'data-handoff-key': key || '',
    'data-handoff-signature': JSON.stringify(message) });
  const name = message.agent_name || 'Agent';
  const heading = el('div', { class: 'agent-message-heading' }, [agentAvatar(name, message.agent_color),
    el('span', { text: message.type === 'handoff' ? `Handed to ${name}` : name })]);
  card.append(heading);
  if (message.type === 'handoff') {
    card.classList.add('agent-handoff');
    card.dataset.status = message.handoff_status;
    heading.append(el('span', { class: 'agent-handoff-status', role: 'status', 'data-status': message.handoff_status || 'queued',
      text: STATUS[message.handoff_status] || 'Queued' }));
    if (message.note) card.append(el('div', { class: 'meta agent-handoff-note', text: message.note }));
  } else {
    const body = el('div', { class: 'msg-body' });
    renderMessageBody(body, message.content || '', sessionId, true);
    card.append(body);
    if (message.inbox_item_id && message.question_status === 'open') {
      const form = el('form', { class: 'agent-handoff-answer' });
      const input = el('textarea', { rows: '2', placeholder: `Answer ${name}`, 'aria-label': `Answer ${name}`, required: true });
      const send = el('button', { type: 'submit', class: 'btn', text: 'Answer' });
      form.append(input, send);
      form.addEventListener('submit', async event => {
        event.preventDefault();
        if (!input.value.trim()) return;
        send.disabled = true;
        try {
          await api(`/api/agents/inbox/${encodeURIComponent(message.inbox_item_id)}/answer`, { method: 'POST',
            body: JSON.stringify({ choice: 'reply', text: input.value.trim() }) });
          form.replaceWith(el('div', { class: 'meta', text: 'Answered' }));
        } catch (problem) { toast(problem.message, 'error'); send.disabled = false; }
      });
      card.append(form);
    } else if (message.inbox_item_id) card.append(el('div', { class: 'meta', text: message.question_status === 'dismissed' ? 'Dismissed' : 'Answered' }));
  }
  return card;
}

export function watchAgentHandoffs(messages, getSessionId, onChange = () => {}) {
  let disposed = false;
  let loading = false;
  const refresh = async () => {
    const id = getSessionId();
    if (disposed || loading || !id || !messages.isConnected) return;
    loading = true;
    try {
      const session = await api(`/api/sessions/${encodeURIComponent(id)}`);
      if (disposed || id !== getSessionId() || !messages.isConnected) return;
      for (const message of session.messages || []) {
        const key = message.type === 'handoff' ? message.id : message.handoff_message_id;
        if (!key) continue;
        const existing = [...messages.children].find(node => node.dataset.handoffKey === key);
        if (existing?.dataset.handoffSignature === JSON.stringify(message)) continue;
        // Keep an answer draft while another part of the run progresses.
        if (existing?.contains(document.activeElement) && message.question_status === 'open') continue;
        const node = renderAgentHandoff(message, id);
        if (!node) continue;
        const follow = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
        if (existing) existing.replaceWith(node);
        else {
          messages.querySelector('.agent-chat-empty')?.remove();
          messages.insertBefore(node, messages.querySelector(':scope > .msg[aria-busy="true"]'));
        }
        onChange();
        if (follow) messages.scrollTop = messages.scrollHeight;
      }
    } catch { /* the saved transcript is recovered on the next refresh */ }
    finally { loading = false; }
  };
  const timer = setInterval(refresh, 2500);
  const unsubscribe = chatStream.subscribeAll((id, entry) => {
    if (id === getSessionId() && entry?.status !== 'processing') refresh();
  });
  return { refresh, dispose() { disposed = true; clearInterval(timer); unsubscribe(); } };
}
