import { api, el } from './api.js';
import { suppressBrowser, releaseBrowser } from './browserPane.js';

// A small navigation rail over the saved transcript. The summary is requested
// only when the person opens it; it never changes the chat's model context.
export function mountChatTimeline(main, messages, getSessionId) {
  const rail = el('nav', { class: 'chat-timeline', 'aria-label': 'Chat timeline', hidden: true });
  const summaryButton = el('button', { type: 'button', class: 'chat-summary-trigger',
    text: 'Summary', title: 'Open chat summary', 'aria-label': 'Open chat summary', hidden: true });
  const track = el('div', { class: 'chat-timeline-track' });
  rail.append(track);
  main.querySelector('.chat-header-new').before(summaryButton);
  main.append(rail);
  let entries = [];
  let dialog = null;
  let generation = 0;
  let disposed = false;
  let renderedSessionId = null;
  let positionFrame = 0;
  let candidateCount = 0;
  let markSummaryStale = null;

  const closeSummary = () => {
    if (!dialog) return;
    dialog.remove();
    dialog = null;
    markSummaryStale = null;
    generation += 1;
    releaseBrowser();
    if (summaryButton.isConnected) summaryButton.focus();
  };

  function renderSummary(state, body, meta, refresh) {
    body.textContent = state.summary || 'No summary is available yet.';
    meta.textContent = state.summary
      ? `${state.stale ? 'This summary is from an earlier point in the chat. ' : ''}${state.summarized_count} messages summarized`
      : 'The selected model will summarize saved chat text. Images and files are represented only by text in the conversation.';
    refresh.textContent = state.summary ? 'Update summary' : 'Generate summary';
    refresh.hidden = false;
  }

  async function openSummary() {
    const sessionId = getSessionId();
    if (!sessionId || dialog) return;
    const ticket = ++generation;
    const backdrop = el('div', { class: 'chat-summary-backdrop', role: 'presentation' });
    const panel = el('section', { class: 'chat-summary-dialog', role: 'dialog',
      'aria-modal': 'true', 'aria-label': 'Chat summary' });
    const close = el('button', { type: 'button', class: 'btn quiet', text: 'Close', onclick: closeSummary });
    const body = el('div', { class: 'chat-summary-body', role: 'status', text: 'Loading summary…' });
    const meta = el('p', { class: 'chat-summary-meta' });
    const refresh = el('button', { type: 'button', class: 'btn quiet', text: 'Generate summary' });
    let shownState = null;
    markSummaryStale = messageCount => {
      if (!shownState?.summary || messageCount === shownState.summarized_count) return;
      shownState = { ...shownState, stale: true };
      renderSummary(shownState, body, meta, refresh);
    };
    const header = el('div', { class: 'chat-summary-header' }, [
      el('div', {}, [el('span', { class: 'chat-summary-eyebrow', text: 'THIS CONVERSATION' }),
        el('h2', { text: 'Chat summary' })]), close,
    ]);
    panel.append(header, meta, body, el('div', { class: 'chat-summary-footer' }, [refresh]));
    backdrop.append(panel);
    backdrop.addEventListener('click', event => { if (event.target === backdrop) closeSummary(); });
    const onKey = event => {
      if (event.key === 'Escape') { event.preventDefault(); closeSummary(); }
      if (event.key !== 'Tab') return;
      const controls = [close, refresh].filter(button => !button.hidden && !button.disabled);
      if (event.shiftKey && document.activeElement === controls[0]) {
        event.preventDefault(); controls.at(-1).focus();
      } else if (!event.shiftKey && document.activeElement === controls.at(-1)) {
        event.preventDefault(); controls[0].focus();
      }
    };
    backdrop.addEventListener('keydown', onKey);
    dialog = backdrop;
    suppressBrowser();
    document.body.append(backdrop);
    close.focus();

    const generate = async force => {
      refresh.disabled = true;
      if (!shownState?.summary) body.textContent = 'Generating a summary with the selected model…';
      meta.textContent = 'This can take a little while for a long chat.';
      try {
        const state = await api('/api/chat/summary', { method: 'POST',
          body: JSON.stringify({ session_id: sessionId, force }) });
        if (!disposed && ticket === generation && getSessionId() === sessionId) {
          shownState = state;
          renderSummary(state, body, meta, refresh);
        }
      } catch (error) {
        if (!disposed && ticket === generation) {
          if (!shownState?.summary) body.textContent = 'The summary could not be generated.';
          meta.textContent = error.message.replace(/^\d+: /, '');
          refresh.textContent = 'Try again';
          refresh.hidden = false;
        }
      } finally { refresh.disabled = false; }
    };
    refresh.addEventListener('click', () => generate(true));
    try {
      const state = await api(`/api/chat/summary?session_id=${encodeURIComponent(sessionId)}`);
      if (disposed || ticket !== generation || getSessionId() !== sessionId) return;
      if (state.summary) { shownState = state; renderSummary(state, body, meta, refresh); }
      else await generate(false);
    } catch (error) {
      if (!disposed && ticket === generation) {
        body.textContent = 'The summary could not be loaded.';
        meta.textContent = error.message.replace(/^\d+: /, '');
        refresh.hidden = false;
      }
    }
  }
  summaryButton.addEventListener('click', openSummary);

  function syncCurrent() {
    const position = messages.scrollTop + 64;
    let current = 0;
    entries.forEach((entry, index) => {
      const target = entry.card.getBoundingClientRect().top - messages.getBoundingClientRect().top + messages.scrollTop;
      if (target <= position) current = index;
    });
    if (messages.scrollHeight - messages.scrollTop - messages.clientHeight < 40) current = entries.length - 1;
    entries.forEach((entry, index) => {
      entry.button.classList.toggle('active', index === current);
      if (index === current) entry.button.setAttribute('aria-current', 'location');
      else entry.button.removeAttribute('aria-current');
    });
  }

  function positionPoints() {
    rail.hidden = candidateCount < 8 || messages.scrollHeight <= messages.clientHeight + 50;
    if (rail.hidden) return;
    const transcriptHeight = Math.max(1, messages.scrollHeight);
    entries.forEach(entry => {
      const top = entry.card.getBoundingClientRect().top - messages.getBoundingClientRect().top + messages.scrollTop;
      entry.button.style.top = `${Math.min(100, Math.max(0, top / transcriptHeight * 100))}%`;
    });
    syncCurrent();
  }

  function update(session) {
    cancelAnimationFrame(positionFrame);
    if (session?.id !== renderedSessionId) closeSummary();
    renderedSessionId = session?.id || null;
    if (dialog) markSummaryStale?.(session?.messages?.length || 0);
    const cards = [...messages.children].filter(node => node.classList.contains('msg'));
    const candidates = (session?.messages || []).map((message, index) => ({ message, card: cards[index], index }))
      .filter(entry => entry.message.role === 'user' && entry.card);
    candidateCount = candidates.length;
    summaryButton.hidden = !session?.id || !candidates.length;
    track.replaceChildren();
    entries = [];
    rail.hidden = candidates.length < 8;
    if (rail.hidden) return;
    const selected = candidates.filter((_, index) => index === 0 || index === candidates.length - 1 ||
      index % Math.max(1, Math.ceil(candidates.length / 16)) === 0);
    selected.forEach(entry => {
      const snippet = (entry.message.content || 'Message').replace(/\s+/g, ' ').slice(0, 72);
      const date = entry.message.ts ? new Date(entry.message.ts * 1000).toLocaleDateString() : '';
      const button = el('button', { type: 'button', class: 'chat-timeline-point',
        title: `${date} · ${snippet}`, 'aria-label': `Jump to message ${entry.index + 1}: ${snippet}` });
      button.append(el('span', { class: 'chat-timeline-preview', text: snippet }));
      button.addEventListener('click', () => {
        const top = entry.card.getBoundingClientRect().top - messages.getBoundingClientRect().top + messages.scrollTop - 20;
        messages.scrollTo({ top, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
      });
      track.append(button);
      entries.push({ card: entry.card, button });
    });
    positionFrame = requestAnimationFrame(positionPoints);
  }

  function reset() {
    cancelAnimationFrame(positionFrame);
    closeSummary();
    renderedSessionId = null;
    candidateCount = 0;
    summaryButton.hidden = true;
    rail.hidden = true;
    track.replaceChildren();
    entries = [];
  }
  messages.addEventListener('scroll', syncCurrent);
  window.addEventListener('resize', positionPoints);
  return { update, reset, dispose: () => {
    disposed = true;
    reset();
    messages.removeEventListener('scroll', syncCurrent);
    window.removeEventListener('resize', positionPoints);
    summaryButton.remove();
    rail.remove();
  } };
}
