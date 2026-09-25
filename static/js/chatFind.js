// Find in chat (Hermes Desktop's Cmd/Ctrl+F, 2026-09-25): a small bar over
// the chat that searches the rendered transcript. Matches are painted with
// the CSS Custom Highlight API, so the message HTML is never rewritten (no
// wrapper elements for a later render to clobber, nothing injected into
// Markdown). Enter / Shift+Enter step through matches; Esc closes.
import { el } from './api.js';

const supported = typeof CSS !== 'undefined' && 'highlights' in CSS && typeof Highlight === 'function';

export function mountChatFind(main, messages) {
  const input = el('input', { type: 'search', class: 'chat-find-input', placeholder: 'Find in chat', 'aria-label': 'Find in chat' });
  const count = el('span', { class: 'chat-find-count', 'aria-live': 'polite' });
  const prev = el('button', { type: 'button', class: 'input-icon-btn', 'aria-label': 'Previous match', title: 'Previous (Shift+Enter)', text: '↑' });
  const next = el('button', { type: 'button', class: 'input-icon-btn', 'aria-label': 'Next match', title: 'Next (Enter)', text: '↓' });
  const close = el('button', { type: 'button', class: 'input-icon-btn', 'aria-label': 'Close find', title: 'Close (Esc)', text: '×' });
  const bar = el('div', { class: 'chat-find', role: 'search', hidden: true }, [input, count, prev, next, close]);
  main.append(bar);

  let ranges = [], current = -1;

  function clear() {
    if (supported) { CSS.highlights.delete('chat-find'); CSS.highlights.delete('chat-find-current'); }
    ranges = []; current = -1; count.textContent = '';
  }

  function search() {
    clear();
    const query = input.value.trim().toLowerCase();
    if (!query) return;
    const walker = document.createTreeWalker(messages, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => node.parentElement?.closest('.msg-actions, .chat-activity, script, style')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const text = node.nodeValue.toLowerCase();
      for (let at = text.indexOf(query); at !== -1; at = text.indexOf(query, at + query.length)) {
        const range = new Range();
        range.setStart(node, at); range.setEnd(node, at + query.length);
        ranges.push(range);
      }
    }
    if (!ranges.length) { count.textContent = 'No matches'; return; }
    if (supported) CSS.highlights.set('chat-find', new Highlight(...ranges));
    // Start from the latest match: the end of a chat is where one usually is.
    go(ranges.length - 1);
  }

  function go(index) {
    if (!ranges.length) return;
    current = (index + ranges.length) % ranges.length;
    count.textContent = `${current + 1} of ${ranges.length}`;
    if (supported) CSS.highlights.set('chat-find-current', new Highlight(ranges[current]));
    const target = ranges[current].startContainer.parentElement;
    target?.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
  }

  function open() {
    bar.hidden = false;
    input.focus(); input.select();
    if (input.value) search();
  }

  function hide() {
    bar.hidden = true;
    clear();
    document.getElementById('chat-input')?.focus();
  }

  let pending = null;
  input.addEventListener('input', () => { clearTimeout(pending); pending = setTimeout(search, 120); });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); clearTimeout(pending); if (!ranges.length) search(); else go(current + (event.shiftKey ? -1 : 1)); }
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); hide(); }
  });
  prev.addEventListener('click', () => go(current - 1));
  next.addEventListener('click', () => go(current + 1));
  close.addEventListener('click', hide);

  // Ctrl/Cmd+F while the chat is on screen and nothing modal is open.
  const onKey = (event) => {
    if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== 'f' || event.shiftKey || event.altKey) return;
    if (!main.isConnected || document.querySelector('.modal-backdrop:not(.hidden)')) return;
    event.preventDefault();
    open();
  };
  document.addEventListener('keydown', onKey);

  return () => { document.removeEventListener('keydown', onKey); clearTimeout(pending); clear(); };
}
