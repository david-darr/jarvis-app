// static/js/browserPane.js — the side browser's UI (David's ask 2026-09-15).
//
// One set of chrome (back / forward / reload / address / open-externally /
// close) over two very different engines:
//
//   - In the desktop app, the page is a native Chromium view owned by the
//     main process (electron/browser.js). This module never touches page
//     content; it positions the view and drives its controls. Because that
//     view is composited ABOVE the HTML, the panel here reserves an empty
//     "viewport" box and continuously reports its rectangle — and anything
//     that must appear over the page (Settings, modals, menus) hides the
//     view rather than trying to out-stack it with z-index, which cannot
//     work against a native layer.
//
//   - In an ordinary browser tab there is no such view, so it falls back to
//     an iframe. That fallback genuinely cannot show much of the web: any
//     site sending X-Frame-Options or a frame-ancestors CSP refuses to load
//     in a frame, and JavaScript cannot read either header cross-origin to
//     detect it. Rather than pretend, "Open in new tab" is always visible,
//     and a load that never arrives says plainly that the site refused.
//
// The pane shares the right-hand region with the file preview panel and only
// one is ever open, so a native layer and a DOM panel never fight over the
// same space.

import { el } from './api.js';

const bridge = () => (window.jarvis && window.jarvis.browser) || null;

let pane = null;
// Depth counter, not a boolean: Settings can open over a modal, and the
// first thing to close would otherwise reveal the page underneath.
let suppressDepth = 0;

export function isBrowserOpen() { return !!pane; }

// Called by anything that renders HTML over the chat area. Only meaningful
// for the native view; the iframe path obeys normal stacking and ignores it.
export function suppressBrowser() {
  suppressDepth += 1;
  if (pane && pane.native) bridge()?.setVisible(false);
}
export function releaseBrowser() {
  suppressDepth = Math.max(0, suppressDepth - 1);
  if (suppressDepth === 0 && pane && pane.native) bridge()?.setVisible(true);
}

export function closeBrowser() {
  if (!pane) return;
  const { panel, cleanups, native } = pane;
  pane = null;
  for (const fn of cleanups) { try { fn(); } catch { /* best effort */ } }
  if (native) bridge()?.close();
  panel.remove();
}

function normalizeForDisplay(url) {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'about:' ? '' : parsed.toString();
  } catch { return url || ''; }
}

export function openBrowser(initialUrl) {
  // One right-hand pane at a time. Imported lazily to avoid a static import
  // cycle: chatContent.js opens this pane for external links.
  import('./chatContent.js').then(m => m.closeArtifact()).catch(() => {});
  closeBrowser();

  const host = document.querySelector('.chat-layout');
  if (!host) return;
  const native = !!bridge();
  const cleanups = [];

  const back = el('button', { type: 'button', class: 'btn quiet', text: '←', 'aria-label': 'Back', disabled: true });
  const forward = el('button', { type: 'button', class: 'btn quiet', text: '→', 'aria-label': 'Forward', disabled: true });
  const reload = el('button', { type: 'button', class: 'btn quiet', text: '⟳', 'aria-label': 'Reload' });
  const address = el('input', {
    type: 'text', class: 'browser-address', spellcheck: 'false', autocomplete: 'off',
    'aria-label': 'Address', placeholder: 'Search or enter address',
  });
  const external = el('button', { type: 'button', class: 'btn quiet', text: 'Open externally', title: 'Open this page in your default browser' });
  const close = el('button', { type: 'button', class: 'btn quiet', text: '×', 'aria-label': 'Close browser' });
  const status = el('p', { class: 'browser-status', role: 'status' });
  const viewport = el('div', { class: 'browser-viewport' });

  const form = el('form', { class: 'browser-address-form', onsubmit: e => { e.preventDefault(); go(address.value); } }, [address]);
  const panel = el('aside', { class: 'artifact-panel browser-panel', role: 'region', 'aria-label': 'Web browser' }, [
    el('div', { class: 'browser-toolbar' }, [back, forward, reload, form, external, close]),
    status, viewport,
  ]);

  const onKey = event => {
    if (event.key === 'Escape' && !suppressDepth) { event.preventDefault(); closeBrowser(); }
  };
  document.addEventListener('keydown', onKey);
  cleanups.push(() => document.removeEventListener('keydown', onKey));

  host.append(panel);
  pane = { panel, cleanups, native };

  let go;
  if (native) {
    const api = bridge();
    // The native view is positioned in window coordinates, so its rectangle
    // has to be re-sent on every layout change — panel resize, window
    // resize, and the artifact/history panels opening beside it.
    const syncBounds = () => {
      if (!pane) return;
      const r = viewport.getBoundingClientRect();
      api.setBounds({ x: r.left, y: r.top, width: r.width, height: r.height });
    };
    const observer = new ResizeObserver(syncBounds);
    observer.observe(viewport);
    cleanups.push(() => observer.disconnect());
    window.addEventListener('resize', syncBounds);
    cleanups.push(() => window.removeEventListener('resize', syncBounds));
    cleanups.push(api.onHostResized(syncBounds));

    cleanups.push(api.onState(state => {
      if (!pane) return;
      back.disabled = !state.canGoBack;
      forward.disabled = !state.canGoForward;
      if (document.activeElement !== address) address.value = normalizeForDisplay(state.url);
      status.textContent = state.loading ? 'Loading…' : '';
      panel.setAttribute('aria-label', state.title ? `Web browser: ${state.title}` : 'Web browser');
    }));
    cleanups.push(api.onError(err => {
      if (pane) status.textContent = `This page didn't load. ${err.description || ''}`.trim();
    }));

    go = async value => {
      status.textContent = '';
      const result = await api.navigate(value);
      if (result && !result.ok && result.reason) status.textContent = result.reason;
    };
    back.onclick = () => api.back();
    forward.onclick = () => api.forward();
    reload.onclick = () => api.reload();
    external.onclick = () => api.openExternal();

    syncBounds();
    api.open(initialUrl, viewport.getBoundingClientRect()).then(result => {
      if (pane && result && !result.ok && result.reason) status.textContent = result.reason;
      else if (pane && result && result.ok) address.value = normalizeForDisplay(result.url);
      syncBounds();
    });
    if (suppressDepth) api.setVisible(false);
  } else {
    // -- plain-browser fallback. Same chrome, an iframe underneath, and no
    // pretending about what it can show.
    let current = '';
    let timer = null;
    let loading = false;
    const frame = el('iframe', {
      class: 'browser-frame', title: 'Embedded web page',
      referrerpolicy: 'no-referrer',
      // Scripts and same-origin are needed for real sites to function at
      // all, but allow-same-origin is safe here precisely because the frame
      // is cross-origin to the app: the browser's own origin rules keep it
      // away from JARVIS. allow-top-navigation is deliberately absent, so a
      // framed page cannot navigate the app out from under the user.
      sandbox: 'allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox',
    });
    viewport.append(frame);

    const armTimeout = () => {
      clearTimeout(timer);
      // A site that refuses framing usually produces no load event at all.
      // There is no header to read cross-origin, so elapsed time is the only
      // signal available — and the message says exactly that.
      loading = true;
      timer = setTimeout(() => {
        loading = false;
        if (pane) status.textContent = 'This site may not allow being displayed here. Use "Open in new tab" to view it.';
      }, 4000);
    };
    // Only a load this pane is still waiting on may clear the status. A
    // refused address writes its own message, and the page that was loading
    // before it must not wipe that message when it finally settles.
    frame.addEventListener('load', () => { clearTimeout(timer); if (!loading) return; loading = false; if (pane) status.textContent = ''; });
    const refuse = message => { clearTimeout(timer); loading = false; status.textContent = message; };
    cleanups.push(() => clearTimeout(timer));

    go = value => {
      const text = String(value || '').trim();
      if (!text) return;
      let target;
      try {
        target = /^[a-z][a-z0-9+.-]*:/i.test(text) ? new URL(text)
          : /^[^\s/]+\.[^\s/]{2,}(\/|$|\?|#)/i.test(text) ? new URL(`https://${text}`)
          : new URL(`https://duckduckgo.com/?q=${encodeURIComponent(text)}`);
      } catch { refuse("That address couldn't be opened."); return; }
      if (target.protocol !== 'http:' && target.protocol !== 'https:') {
        refuse('Only web addresses can be opened here.');
        return;
      }
      current = target.toString();
      address.value = current;
      status.textContent = 'Loading…';
      frame.src = current;
      armTimeout();
    };
    // History is not readable across origins, so these stay disabled rather
    // than offering controls that would silently do nothing.
    back.title = forward.title = 'Browsing history is only available in the desktop app';
    external.textContent = 'Open in new tab';
    external.onclick = () => { if (current) window.open(current, '_blank', 'noopener,noreferrer'); };
    reload.onclick = () => { if (current) { frame.src = current; armTimeout(); } };
    go(initialUrl);
  }

  close.onclick = closeBrowser;
  address.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); address.blur(); } });
  return panel;
}
