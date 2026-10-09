import { el, toast } from './api.js';

// The composer's reference snapshot is the source of truth, including queue
// restores. Reviews belong to exact URLs, so version switches cannot move them.
let composer = null;
const listeners = new Set();
export const reviewKey = item => JSON.stringify([item.url, item.picks, item.comment]);
export function bindReviewComposer(value) {
  composer = value; reviewsChanged();
  return () => { if (composer === value) { composer = null; reviewsChanged(); } };
}
export function reviewsChanged() { for (const listener of listeners) listener(); }
const comments = sessionId => composer?.sessionId() === sessionId ? composer.getSelected().filter(r => r.kind === 'artifact_comment') : [];
export async function openReview(item, sessionId, opener) {
  const { openArtifactComment } = await import('./chatContent.js');
  await openArtifactComment(sessionId, item, opener);
}
export function shortLocator(picks) {
  const pick = picks[0];
  if (pick.startsWith('pptx:')) return `Slide ${pick.match(/slide=(\d+)/)[1]} · ${picks.length} item${picks.length === 1 ? '' : 's'}`;
  if (pick.startsWith('xlsx:')) { const [, sheet, cell] = pick.match(/^xlsx:sheet=(.+);cell=(.+)$/); return `${decodeURIComponent(sheet)}!${cell}${picks.length > 1 ? ` · ${picks.length} ranges` : ''}`; }
  if (pick.startsWith('pdf:')) return `Page ${pick.match(/page=(\d+)/)[1]}${pick.includes(';rect=') ? ' · area' : ''}`;
  if (pick.startsWith('image')) return 'Image · area';
  if (pick.startsWith('html:')) return `${picks.length} element${picks.length === 1 ? '' : 's'}`;
  if (/^(text|md):/.test(pick)) {
    const [, a, b] = pick.match(/=(\d+)(?:-(\d+))?$/);
    return (!b || a === b ? `line ${a}` : `lines ${a}–${b}`) + (picks.length > 1 ? ` · ${picks.length} ranges` : '');
  }
  return `${picks.length} block${picks.length === 1 ? '' : 's'}`;
}
const cellParts = pick => pick.match(/^xlsx:sheet=(.+);cell=([A-Z]+)(\d+)$/);
const column = letters => [...letters].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
const letters = n => { let s = ''; for (; n; n = Math.floor((n - 1) / 26)) s = String.fromCharCode(65 + (n - 1) % 26) + s; return s; };
function containsPick(pick, node) {
  const actual = node.dataset.pick;
  if (pick === actual) return true;
  const lines = pick.match(/^(?:text|md):lines=(\d+)-(\d+)$/), line = actual.match(/^(?:text:line=(\d+)|md:lines=(\d+)-(\d+))$/);
  if (lines && line) return Number(line[1] || line[2]) <= Number(lines[2]) && Number(line[1] || line[3]) >= Number(lines[1]);
  const range = pick.match(/^xlsx:sheet=(.+);cell=([A-Z]+)(\d+):([A-Z]+)(\d+)$/), cell = cellParts(actual);
  if (range && cell) return range[1] === cell[1] && column(cell[2]) >= column(range[2]) && column(cell[2]) <= column(range[4]) && +cell[3] >= +range[3] && +cell[3] <= +range[5];
  return false;
}

export function mountArtifactReview(tab) {
  const { container, content, actions } = tab;
  const lifetime = new AbortController();
  let root, rootEvents, enabled = false, picks = [], editing = null, anchor = null, drag = null, hover = null, frame = 0, focusPick = false;
  const toggle = el('button', { type: 'button', class: 'btn quiet artifact-select', text: 'Select', 'aria-label': 'Select parts of artifact', 'aria-pressed': 'false', onclick: () => setEnabled(!enabled) });
  const input = el('textarea', { placeholder: 'What should change?', 'aria-label': 'What should change?', maxlength: '4000', rows: '3' });
  const add = el('button', { type: 'button', class: 'btn primary', text: 'Add to message', onclick: commit });
  const savedComments = () => [...comments(tab.sessionId), ...(tab.foreign?.comments || [])];
  const box = el('div', { class: 'artifact-comment-box', role: 'group', 'aria-label': 'Artifact comment', hidden: true }, [input,
    el('div', { class: 'artifact-comment-actions' }, [add, el('button', { type: 'button', class: 'btn quiet', text: 'Cancel', onclick: cancel })])]);
  container.append(box); actions.prepend(toggle);
  function setEnabled(value) {
    enabled = value; container.dataset.selecting = String(value); toggle.setAttribute('aria-pressed', String(value));
    if (!value) cancel();
    if (root) root.dataset.selecting = String(value);
  }
  function cancel() { picks = []; editing = null; anchor = null; drag = null; hover?.classList.remove('artifact-pick-hover'); hover = null; focusPick = false; input.value = ''; box.hidden = true; paint(); }
  function commit() {
    if (!input.value.trim() || !picks.length) { input.focus(); return; }
    const item = { kind: 'artifact_comment', url: tab.url, comment: input.value.trim(), picks: [...picks], label: `${content.artifact?.filename || tab.name} · ${shortLocator(picks)}` };
    // An existing foreign draft remains origin-bound even after opening its
    // chat. New comments in that chat still use the ordinary composer chips.
    const foreignEdit = editing && tab.foreign?.comments.some(r => reviewKey(r) === reviewKey(editing));
    if (composer?.sessionId() === tab.sessionId && !foreignEdit) { if (composer.add(item, editing)) cancel(); }
    else if (tab.addForeignComment?.(item, editing)) cancel();
  }
  input.addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); commit(); } }, { signal: lifetime.signal });
  function position() {
    if (box.hidden) return;
    const bounds = container.getBoundingClientRect();
    let target = root?.querySelector('.artifact-picked') || root;
    let rect = target?.getBoundingClientRect();
    const iframe = content.querySelector('iframe');
    if (iframe && target?.ownerDocument !== document) {
      const frameBox = iframe.getBoundingClientRect(), scale = frameBox.width / iframe.offsetWidth;
      rect = { left: frameBox.left + rect.left * scale, right: frameBox.left + rect.right * scale, bottom: frameBox.top + rect.bottom * scale };
    }
    const right = (rect?.right || bounds.right) - bounds.left + 8;
    box.style.left = `${Math.max(8, Math.min(right, bounds.width - box.offsetWidth - 8))}px`;
    box.style.top = `${Math.max(8, Math.min((rect?.bottom || bounds.top) - bounds.top + 8, bounds.height - box.offsetHeight - 8))}px`;
  }
  function paint() {
    if (!root) return;
    observer.disconnect();
    const saved = savedComments().filter(r => r.url === tab.url);
    root.querySelectorAll('.artifact-review-rect').forEach(node => node.remove());
    const nodes = [...root.querySelectorAll('[data-pick]'), ...(root.matches?.('[data-pick]') ? [root] : [])];
    for (const node of nodes) {
      node.classList.toggle('artifact-picked', picks.some(p => containsPick(p, node)));
      node.classList.toggle('artifact-commented', saved.some(r => r.picks.some(p => containsPick(p, node))));
    }
    for (const [list, committed] of [[picks, false], [saved.flatMap(r => r.picks), true]]) for (const pick of list) {
      if (!pick.includes(';rect=')) continue;
      const [base, coordinates] = pick.split(';rect='), node = nodes.find(n => n.dataset.pick === base);
      if (!node) continue;
      const [x, y, w, h] = coordinates.split(',').map(Number);
      const overlay = el('div', { class: `artifact-review-rect ${committed ? 'artifact-commented' : 'artifact-picked'}`, 'aria-hidden': 'true' });
      // Image viewport includes margins and may scroll; use the actual image box.
      const target = base === 'image' ? node.querySelector('img') : node.querySelector('canvas');
      if (!target) continue;
      overlay.style.left = `${target.offsetLeft + x * target.offsetWidth}px`; overlay.style.top = `${target.offsetTop + y * target.offsetHeight}px`;
      overlay.style.width = `${w * target.offsetWidth}px`; overlay.style.height = `${h * target.offsetHeight}px`; node.append(overlay);
    }
    const spot = root.querySelector('.artifact-review-rect.artifact-picked') || root.querySelector('.artifact-picked');
    if (focusPick && spot) { spot.scrollIntoView({ block: 'center', inline: 'nearest' }); focusPick = false; }
    position();
    observer.observe(content, { childList: true, subtree: true });
  }
  function selection(next, event, node) {
    if (next.length > 300) { toast('This element is too deeply nested to comment on. Select its parent.', 'error'); return; }
    if (event.shiftKey && anchor?.startsWith('text:line=') && next.startsWith('text:line=')) {
      const a = +anchor.split('=')[1], b = +next.split('=')[1]; picks = [`text:lines=${Math.min(a,b)}-${Math.max(a,b)}`];
    } else if (event.shiftKey || event.ctrlKey || event.metaKey) {
      picks = picks.includes(next) ? picks.filter(p => p !== next) : [...picks, next];
    } else { picks = [next]; anchor = next; }
    if (picks.length > 50) { picks = picks.slice(0, 50); toast('Select at most 50 places per comment', 'error'); }
    box.hidden = !picks.length; paint();
  }
  const nearest = event => {
    let node = event.target.closest?.('[data-pick]');
    if (event.altKey && node?.dataset.group) node = root.querySelector(`[data-pick="${node.dataset.group}"]`) || node;
    return node && (node === root || root.contains(node)) ? node : null;
  };
  function bindRoot() {
    rootEvents?.abort(); rootEvents = new AbortController(); root = content.viewerRoot;
    if (!root) return;
    root.dataset.selecting = String(enabled);
    if (root.ownerDocument !== document) root.style.setProperty('--artifact-review-accent', getComputedStyle(container).getPropertyValue('--accent'));
    if (root.ownerDocument !== document && !root.ownerDocument.querySelector('#artifact-review-style')) {
      const style = root.ownerDocument.createElement('style'); style.id = 'artifact-review-style';
      style.textContent = '.artifact-pick-hover,.artifact-picked{outline:2px solid var(--artifact-review-accent);outline-offset:-2px}.artifact-commented{outline:2px dashed var(--artifact-review-accent);outline-offset:-2px}.artifact-picked.artifact-commented{outline-style:solid}[data-selecting="true"] [data-pick]{cursor:crosshair;touch-action:none;user-select:none}'; root.ownerDocument.head.append(style);
    }
    const options = { capture: true, signal: rootEvents.signal };
    root.addEventListener('pointermove', event => {
      if (!enabled) return;
      const node = nearest(event);
      hover?.classList.remove('artifact-pick-hover'); hover = node; hover?.classList.add('artifact-pick-hover');
      const cell = drag && cellParts(drag.pick) ? root.ownerDocument.elementFromPoint(event.clientX, event.clientY)?.closest('[data-pick]') : null;
      if (cell && cellParts(cell.dataset.pick)) drag.end = cell.dataset.pick;
      if (drag?.rect) { drag.x2 = event.clientX; drag.y2 = event.clientY; updateRect(); paint(); }
    }, options);
    root.addEventListener('pointerleave', () => { hover?.classList.remove('artifact-pick-hover'); hover = null; }, options);
    root.addEventListener('pointerdown', event => {
      if (!enabled || event.button !== 0) return;
      const node = nearest(event); if (!node) return;
      event.preventDefault(); event.stopPropagation();
      container.focus({ preventScroll: true });
      const pick = node.dataset.pick;
      if (cellParts(pick) || pick === 'image' || pick.startsWith('pdf:')) {
        const target = pick === 'image' ? node.querySelector('img') : node.querySelector('canvas');
        drag = { pick, end: pick, node, event, x: event.clientX, y: event.clientY, x2: event.clientX, y2: event.clientY, rect: target?.getBoundingClientRect(), before: [...picks] };
        box.hidden = true;
      }
    }, options);
    root.addEventListener('click', event => {
      if (!enabled) return;
      if (suppressClick) { suppressClick = false; event.preventDefault(); event.stopPropagation(); return; }
      const node = nearest(event); if (!node) return;
      event.preventDefault(); event.stopPropagation();
      selection(node.dataset.pick, event, node);
    }, options);
    root.ownerDocument.addEventListener('pointermove', event => {
      if (!drag || root.contains(event.target)) return;
      if (drag.rect) { drag.x2 = event.clientX; drag.y2 = event.clientY; updateRect(); paint(); }
    }, { capture: true, signal: rootEvents.signal });
    root.ownerDocument.addEventListener('pointerup', endDrag, { capture: true, signal: rootEvents.signal });
    root.ownerDocument.addEventListener('pointercancel', () => { drag = null; }, { signal: rootEvents.signal });
    if (root.ownerDocument !== document) root.ownerDocument.addEventListener('keydown', keydown, { signal: rootEvents.signal });
    paint();
  }
  let suppressClick = false;
  function updateRect() {
    const r = drag.rect, clamp = (v, size) => Math.max(0, Math.min(1, v / size));
    const x1 = clamp(drag.x-r.left,r.width), y1 = clamp(drag.y-r.top,r.height), x2 = clamp(drag.x2-r.left,r.width), y2 = clamp(drag.y2-r.top,r.height);
    const x = +Math.min(x1,x2).toFixed(4), y = +Math.min(y1,y2).toFixed(4);
    const values = [x, y, Math.min(1-x, Math.abs(x1-x2)), Math.min(1-y, Math.abs(y1-y2))].map(n => n.toFixed(4));
    if (+values[2] && +values[3]) picks = [...((drag.event.ctrlKey || drag.event.metaKey || drag.event.shiftKey) ? drag.before : []), `${drag.pick};rect=${values.join(',')}`];
  }
  function endDrag(event) {
    if (!drag) return;
    if (drag.rect) { drag.x2 = event.clientX; drag.y2 = event.clientY; updateRect(); }
    else {
      const a = cellParts(drag.pick), b = cellParts(drag.end);
      if (a[1] === b[1] && drag.pick !== drag.end) {
        const range = `xlsx:sheet=${a[1]};cell=${letters(Math.min(column(a[2]),column(b[2])))}${Math.min(+a[3],+b[3])}:${letters(Math.max(column(a[2]),column(b[2])))}${Math.max(+a[3],+b[3])}`;
        picks = [...((drag.event.ctrlKey || drag.event.metaKey || drag.event.shiftKey) ? drag.before : []), range];
      }
    }
    suppressClick = Math.abs(event.clientX-drag.x) + Math.abs(event.clientY-drag.y) > 3;
    setTimeout(() => { suppressClick = false; }, 0);
    drag = null; box.hidden = !picks.length; paint();
  }
  function keydown(event) {
    if (event.key === 'Escape' && enabled) { event.preventDefault(); event.stopPropagation(); setEnabled(false); return; }
    if (event.key.toLowerCase() === 's' && !event.ctrlKey && !event.metaKey && !event.altKey && !/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)) { event.preventDefault(); setEnabled(!enabled); }
  }
  container.tabIndex = 0;
  container.addEventListener('keydown', keydown, { signal: lifetime.signal });
  content.addEventListener('artifact-root', bindRoot, { signal: lifetime.signal });
  // Slide, sheet, PDF page and zoom renders replace descendants of a stable root.
  const observer = new MutationObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(paint); });
  observer.observe(content, { childList: true, subtree: true });
  const resize = new ResizeObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(paint); }); resize.observe(container);
  container.addEventListener('scroll', position, { capture: true, signal: lifetime.signal });
  container.addEventListener('artifact-version', () => { cancel(); actions.prepend(toggle); }, { signal: lifetime.signal });
  const changed = () => {
    add.textContent = composer?.sessionId() === tab.sessionId ? 'Add to message' : 'Add comment';
    if (editing && !savedComments().some(r => reviewKey(r) === reviewKey(editing))) cancel(); paint();
  };
  listeners.add(changed);
  changed();
  return {
    leave() { if (!enabled) return false; setEnabled(false); return true; },
    edit(item) {
      setEnabled(true); editing = item; picks = [...item.picks]; anchor = picks[0]; input.value = item.comment; box.hidden = false; focusPick = true;
      const pick = picks[0], slide = pick.match(/^pptx:slide=(\d+)/), page = pick.match(/^pdf:page=(\d+)/), sheet = pick.match(/^xlsx:sheet=(.+);cell=/);
      if (slide) content.querySelector(`[aria-label="Go to slide ${slide[1]}"]`)?.click();
      if (page) content.querySelector(`[aria-label="Go to page ${page[1]}"]`)?.click();
      if (sheet) [...content.querySelectorAll('.office-tab')].find(n => n.textContent === decodeURIComponent(sheet[1]))?.click();
      if (/^text:/.test(pick) && !content.querySelector('.artifact-lines')) [...actions.querySelectorAll('button')].find(n => n.textContent === 'Source')?.click();
      paint(); root?.querySelector('.artifact-picked')?.scrollIntoView({ block: 'center' }); position(); input.focus({ preventScroll: true });
    },
    dispose() { lifetime.abort(); rootEvents?.abort(); observer.disconnect(); resize.disconnect(); listeners.delete(changed); cancelAnimationFrame(frame); box.remove(); }
  };
}
