import { el } from './api.js';

const DEFAULT_WIDTH = 520;
const MIN_WIDTH = 380;
const STORAGE_KEY = 'jarvis:chat-pane-width';
let current = null;

// Every right-hand pane registers here so even rapid opens close the native
// browser synchronously. The registration owns the divider and its listeners.
export function mountChatPane(host, panel, close, { onDrag } = {}) {
  current?.close();
  let preferred = DEFAULT_WIDTH;
  try {
    const saved = Number(localStorage.getItem(STORAGE_KEY));
    if (Number.isFinite(saved) && saved >= MIN_WIDTH) preferred = saved;
  } catch { /* Storage can be disabled on this device. */ }
  const main = host.querySelector('#chat-main');
  const handle = el('div', { class: 'chat-pane-resizer', tabindex: '0', role: 'separator',
    'aria-label': 'Resize chat column', 'aria-orientation': 'vertical', 'aria-controls': 'chat-main' });
  let pointer = null, startX = 0, startWidth = 0;
  let maximized = false;
  const inertBefore = new Map();
  const maximum = () => Math.max(MIN_WIDTH, Math.floor(host.clientWidth * 0.6));
  const clamp = width => Math.max(MIN_WIDTH, Math.min(maximum(), width));
  const sync = () => {
    const width = clamp(preferred);
    host.style.setProperty('--chat-pane-width', `${width}px`);
    handle.setAttribute('aria-valuemin', String(MIN_WIDTH));
    handle.setAttribute('aria-valuemax', String(maximum()));
    handle.setAttribute('aria-valuenow', String(Math.round(width)));
    handle.setAttribute('aria-valuetext', `${Math.round(width)} pixels`);
  };
  const save = () => {
    try { localStorage.setItem(STORAGE_KEY, String(preferred)); } catch { /* Best effort. */ }
  };
  const resize = width => { preferred = clamp(width); sync(); };
  const stopDrag = () => {
    if (pointer === null) return;
    const id = pointer;
    pointer = null;
    if (handle.hasPointerCapture(id)) handle.releasePointerCapture(id);
    host.classList.remove('pane-dragging');
    onDrag?.(false);
    save();
  };
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0 || window.innerWidth <= 1100) return;
    event.preventDefault();
    handle.focus({ preventScroll: true });
    pointer = event.pointerId; startX = event.clientX; startWidth = main.getBoundingClientRect().width;
    handle.setPointerCapture(pointer);
    host.classList.add('pane-dragging');
    onDrag?.(true);
  });
  handle.addEventListener('pointermove', event => {
    if (event.pointerId === pointer) resize(startWidth + event.clientX - startX);
  });
  handle.addEventListener('pointerup', stopDrag);
  handle.addEventListener('pointercancel', stopDrag);
  handle.addEventListener('lostpointercapture', stopDrag);
  handle.addEventListener('dblclick', () => { resize(DEFAULT_WIDTH); save(); });
  handle.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    resize(clamp(preferred) + (event.key === 'ArrowRight' ? 24 : -24)); save();
  });
  const setMaximized = value => {
    maximized = value && window.innerWidth > 1100;
    host.classList.toggle('pane-maximized', maximized);
    // Hidden conversations must also leave the keyboard/accessibility tree.
    for (const node of host.querySelectorAll('#chat-main, #chat-sessions, .side-chat')) {
      if (maximized) {
        if (!inertBefore.has(node)) inertBefore.set(node, node.inert);
        node.inert = true;
      }
    }
    if (!maximized) {
      for (const [node, inert] of inertBefore) node.inert = inert;
      inertBefore.clear();
    }
    panel.dispatchEvent(new CustomEvent('pane-maximize', { detail: maximized }));
  };
  const onWindowResize = () => {
    if (window.innerWidth <= 1100) { stopDrag(); setMaximized(false); }
    sync();
  };
  host.classList.add('has-pane');
  host.append(handle);
  if (host.moveBefore && panel.isConnected) host.moveBefore(panel, null);
  else host.append(panel);
  sync();
  const observer = new ResizeObserver(sync);
  observer.observe(host);
  window.addEventListener('resize', onWindowResize);
  const registration = current = { close };
  return {
    isMaximized: () => maximized,
    setMaximized,
    dispose() {
      stopDrag(); setMaximized(false);
      observer.disconnect(); window.removeEventListener('resize', onWindowResize);
      handle.remove();
      host.classList.remove('has-pane', 'pane-dragging');
      host.style.removeProperty('--chat-pane-width');
      if (current === registration) current = null;
    },
  };
}
