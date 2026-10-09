// Composer @ picker. Selected IDs stay separate from the visible draft and
// are resolved again by the server when the message is sent.
import { api, toast } from './api.js';
import { agentAvatar } from './agentHandoff.js';
import { bindReviewComposer, reviewsChanged, reviewKey, openReview } from './artifactReview.js';

const MAX_REFERENCES = 5;
const KINDS = { file: 'File', note: 'Vault note', chat: 'Chat', agent: 'Agent', artifact_comment: 'Comment' };
let pickerNumber = 0;

export function mountChatReferences(input, inputTop, getSessionId) {
  const list = document.createElement('div');
  list.className = 'chat-reference-picker';
  list.id = `chat-reference-picker-${++pickerNumber}`;
  list.setAttribute('role', 'listbox');
  list.setAttribute('aria-label', 'References');
  list.hidden = true;
  inputTop.append(list);
  input.setAttribute('aria-controls', list.id);

  const chips = document.createElement('div');
  chips.className = 'chat-reference-chips';
  chips.setAttribute('aria-label', 'Selected references');
  inputTop.append(chips);

  let selected = [];
  let results = [];
  let active = 0;
  let range = null;
  let timer = null;
  let requestNumber = 0;

  const key = item => item.kind === 'artifact_comment' ? reviewKey(item) : `${item.kind}:${item.session_id || ''}:${item.id}`;
  const getSelected = () => selected.map(item => item.kind === 'artifact_comment'
    ? { kind: item.kind, url: item.url, comment: item.comment, picks: [...item.picks], label: item.label }
    : { kind: item.kind, id: item.id, session_id: item.session_id, label: item.label, color: item.color });
  const hide = () => {
    clearTimeout(timer);
    requestNumber++;
    list.hidden = true;
    list.replaceChildren();
    results = [];
    range = null;
    input.removeAttribute('aria-activedescendant');
    input.setAttribute('aria-expanded', 'false');
  };
  const paintChips = () => {
    chips.replaceChildren();
    for (const item of selected) {
      const chip = document.createElement('span');
      chip.className = 'chat-reference-chip';
      if (item.kind === 'agent') chip.append(agentAvatar(item.label, item.color));
      const label = document.createElement(item.kind === 'artifact_comment' ? 'button' : 'span');
      if (item.kind === 'artifact_comment') {
        chip.classList.add('artifact-comment-chip'); label.type = 'button'; label.className = 'artifact-comment-open';
        label.title = `${item.label}\n${item.comment}`;
        const name = document.createElement('span'), comment = document.createElement('span');
        name.textContent = item.label; comment.textContent = item.comment;
        label.append(name, comment);
        label.addEventListener('click', () => openReview(item, getSessionId(), label).catch(error => toast(error.message, 'error')));
      } else label.textContent = `${KINDS[item.kind]} · ${item.label}`;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.setAttribute('aria-label', `Remove ${item.label}`);
      remove.addEventListener('click', () => {
        selected = selected.filter(ref => key(ref) !== key(item));
        paintChips();
        reviewsChanged();
        input.focus();
      });
      chip.append(label, remove);
      chips.append(chip);
    }
  };
  const clear = () => { selected = []; paintChips(); hide(); reviewsChanged(); };
  const add = (item, previous = null) => {
    if (!KINDS[item.kind]) return false;
    const index = previous ? selected.findIndex(ref => key(ref) === key(previous)) : -1;
    if (index < 0 && selected.some(ref => key(ref) === key(item))) return true;
    const isComment = item.kind === 'artifact_comment';
    if (index < 0 && selected.filter(ref => (ref.kind === 'artifact_comment') === isComment).length >= (isComment ? 10 : MAX_REFERENCES)) {
      toast(isComment ? 'Add at most 10 artifact comments per message' : 'Select at most 5 references', 'error'); return false;
    }
    if (index < 0) selected.push(item); else selected[index] = item;
    paintChips();
    reviewsChanged(); return true;
  };
  const choose = item => {
    if (!range) return;
    const before = input.value.slice(0, range.start);
    const after = input.value.slice(range.end);
    input.value = before + after;
    input.setSelectionRange(before.length, before.length);
    add(item);
    hide();
    input.dispatchEvent(new Event('input'));
    input.focus();
  };
  const paintResults = () => {
    list.replaceChildren();
    if (!results.length) {
      const empty = document.createElement('div');
      empty.className = 'chat-reference-empty';
      empty.textContent = 'No matching files, vault notes, or chats';
      list.append(empty);
    }
    results.forEach((item, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.id = `${list.id}-option-${index}`;
      button.className = 'chat-reference-option';
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', String(index === active));
      const kind = document.createElement('span');
      kind.className = 'chat-reference-kind';
      kind.textContent = KINDS[item.kind];
      if (item.kind === 'agent') kind.prepend(agentAvatar(item.label, item.color));
      const name = document.createElement('span');
      name.className = 'chat-reference-name';
      name.textContent = item.label;
      const detail = document.createElement('span');
      detail.className = 'chat-reference-detail';
      detail.textContent = item.detail || '';
      button.append(kind, name, detail);
      button.addEventListener('mousedown', event => event.preventDefault());
      button.addEventListener('click', () => choose(item));
      list.append(button);
    });
    list.hidden = false;
    input.setAttribute('aria-expanded', 'true');
    if (results.length) input.setAttribute('aria-activedescendant', `${list.id}-option-${active}`);
    else input.removeAttribute('aria-activedescendant');
  };
  const update = () => {
    clearTimeout(timer);
    const before = input.value.slice(0, input.selectionStart);
    const match = /(^|\s)@([^@\n]{0,80})$/.exec(before);
    if (!match || selected.filter(ref => ref.kind !== 'artifact_comment').length >= MAX_REFERENCES || input.selectionStart !== input.selectionEnd) {
      requestNumber++;
      hide();
      return;
    }
    range = { start: before.length - match[0].length + match[1].length, end: input.selectionStart };
    const query = match[2].trim();
    const thisRequest = ++requestNumber;
    timer = setTimeout(async () => {
      try {
        const matches = await api(`/api/chat/references?q=${encodeURIComponent(query)}&session_id=${encodeURIComponent(getSessionId() || '')}`);
        if (thisRequest !== requestNumber) return;
        results = matches.filter(item => !selected.some(ref => key(ref) === key(item))).slice(0, 12);
        active = 0;
        paintResults();
      } catch {
        if (thisRequest === requestNumber) hide();
      }
    }, 180);
  };
  const onKeydown = event => {
    if (list.hidden) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); requestNumber++; hide(); }
    else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      event.stopPropagation();
      if (!results.length) return;
      active = (active + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
      paintResults();
      list.querySelector(`#${list.id}-option-${active}`)?.scrollIntoView({ block: 'nearest' });
    } else if ((event.key === 'Enter' || event.key === 'Tab') && results.length) {
      event.preventDefault();
      event.stopPropagation();
      choose(results[active]);
    }
  };
  input.addEventListener('input', update);
  input.addEventListener('click', update);
  input.addEventListener('keydown', onKeydown, true);
  const onOutside = event => { if (!inputTop.contains(event.target) && !list.contains(event.target)) hide(); };
  document.addEventListener('pointerdown', onOutside);
  // The artifact pane belongs to the main chat, not an independently mounted
  // side/agent composer. Opening one must not steal the main review draft.
  const releaseReview = input.id === 'chat-input' ? bindReviewComposer({ add, getSelected, sessionId: getSessionId }) : () => {};
  return {
    getSelected, add, clear, hide,
    dispose() {
      releaseReview();
      clearTimeout(timer);
      requestNumber++;
      document.removeEventListener('pointerdown', onOutside);
      input.removeEventListener('input', update);
      input.removeEventListener('click', update);
      input.removeEventListener('keydown', onKeydown, true);
      input.removeAttribute('aria-controls');
      input.removeAttribute('aria-activedescendant');
      input.removeAttribute('aria-expanded');
      list.remove();
      chips.remove();
    },
  };
}
