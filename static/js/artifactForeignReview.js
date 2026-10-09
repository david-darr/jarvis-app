import { el, toast } from './api.js';
import { getInFlight, startTurn, subscribeAll } from './chatStream.js';
import { reviewsChanged, reviewKey } from './artifactReview.js';

// A tab owns its draft and submission. Neither the composer nor the currently
// selected chat can change the target of these references.
export function mountForeignReview(tab, { title, openChat, save, completed }, saved = {}) {
  let armed = false;
  const state = tab.foreign = { comments: saved.comments || [], note: saved.note || '', queued: !!saved.queued, submission: null, status: saved.status || '', error: '' };
  const heading = el('strong');
  const list = el('div', { class: 'artifact-foreign-comments' });
  const note = el('input', { type: 'text', placeholder: 'Note for the chat', 'aria-label': 'Note for the chat', maxlength: '16000', value: state.note });
  const status = el('span', { class: 'meta', role: 'status' });
  const send = el('button', { type: 'button', class: 'btn primary', text: 'Send', onclick: submit });
  const cancel = el('button', { type: 'button', class: 'btn quiet', text: 'Cancel queued send', onclick: () => { state.queued = false; state.status = ''; update(); save(); } });
  const open = el('button', { type: 'button', class: 'btn quiet', text: 'Open chat', onclick: openChat });
  const bar = tab.foreignBar = el('div', { class: 'artifact-foreign-bar', hidden: true }, [heading, list, note,
    el('div', { class: 'artifact-foreign-actions' }, [status, send, cancel, open])]);
  tab.container.append(bar);
  note.addEventListener('input', () => { state.note = note.value; save(); });
  function update() {
    const entry = getInFlight(tab.sessionId);
    const busy = state.submission && entry?.status === 'processing';
    const approval = (busy || state.queued) && entry?.status === 'processing' && !!entry.permission;
    bar.hidden = !state.comments.length && !state.status && !state.queued;
    heading.textContent = `${state.comments.length} comment${state.comments.length === 1 ? '' : 's'} for ${title()}`;
    heading.hidden = !state.comments.length;
    list.replaceChildren(...state.comments.map(item => el('div', { class: 'artifact-foreign-comment' }, [
      el('button', { type: 'button', class: 'btn quiet', text: `${item.label}: ${item.comment}`, disabled: state.queued, onclick: () => tab.review.edit(item) }),
      el('button', { type: 'button', class: 'btn quiet', text: '×', 'aria-label': 'Remove comment', disabled: state.queued, onclick: () => {
        state.comments = state.comments.filter(other => other !== item); update(); reviewsChanged(); save();
      } }),
    ])));
    note.hidden = !state.comments.length;
    note.disabled = state.queued;
    note.value = state.note;
    status.textContent = state.queued ? `${title()} is busy; your comments will send when it finishes${approval ? ' · Needs approval' : ''}` : approval ? 'Needs approval' : busy ? 'Working…' : state.status === 'failed' ? `Failed: ${state.error}` : state.status;
    send.hidden = !state.comments.length || state.queued;
    send.disabled = !!busy;
    send.textContent = state.status === 'failed' ? 'Retry' : 'Send';
    cancel.hidden = !state.queued;
    open.hidden = !approval;
  }
  function submit() {
    if (!state.comments.length || state.submission) return;
    if (getInFlight(tab.sessionId)?.status === 'processing') {
      state.queued = true; update(); save(); return;
    }
    // Set the submission before startTurn's synchronous notification. Copies
    // retain the exact URL/picks even if the tab subsequently changes version.
    const submission = state.submission = { comments: [...state.comments], note: state.note };
    state.queued = false; state.status = 'Working…';
    try {
      startTurn(tab.sessionId, title(), state.note.trim() || 'Please apply these review comments.', [], submission.comments);
      state.comments = []; state.note = ''; reviewsChanged(); save(); update();
    } catch (error) {
      state.submission = null; state.status = 'failed'; state.error = error.message; update(); save();
    }
  }
  const detach = subscribeAll((id, entry) => {
    if (id !== tab.sessionId) return;
    if (entry && entry.status !== 'processing') {
      if (state.submission) {
        const submission = state.submission; state.submission = null;
        if (entry.status === 'done') state.status = 'Done';
        else {
          state.comments = [...submission.comments, ...state.comments];
          state.note = submission.note; state.status = 'failed'; state.error = entry.error || 'The turn was stopped.';
        }
        reviewsChanged(); save();
      }
      completed(entry);
    }
    update();
    // Defer until all listeners have observed the completed entry. Recheck
    // the registry: another tab/composer may have claimed the next turn.
    if (armed && state.queued && entry?.status !== 'processing') queueMicrotask(() => { if (armed && state.queued) submit(); });
  });
  tab.addForeignComment = (item, editing) => {
    if (state.queued) { toast('Cancel the queued send to edit its comments', 'error'); return false; }
    const index = editing ? state.comments.findIndex(other => reviewKey(other) === reviewKey(editing)) : -1;
    if (index < 0 && state.comments.length + (state.submission?.comments.length || 0) >= 10) { toast('Add at most 10 comments per message', 'error'); return false; }
    if (index >= 0) state.comments[index] = item; else state.comments.push(item);
    state.status = ''; update(); reviewsChanged(); save(); return true;
  };
  update();
  return {
    update,
    resume() { armed = true; if (state.queued) queueMicrotask(() => { if (armed && state.queued) submit(); }); },
    dispose() { armed = false; state.queued = false; detach(); bar.remove(); },
  };
}
