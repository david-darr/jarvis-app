import { api, el, openPanelDialog, toast } from './api.js';

const base = id => `/api/forge/sessions/${encodeURIComponent(id)}/app`;
const changed = () => document.dispatchEvent(new Event('kairos:forge-apps'));
const bridge = () => window.jarvis?.forgePreview;
let activeNative = null;

export async function launchApp(id, action = 'start', { signal, owner } = {}) {
  const result = await permissionAction(`${base(id)}/${action}`, { signal, owner });
  changed(); return result;
}

export async function permissionAction(url, { signal, owner, body } = {}) {
  const response = await fetch(url, { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
  if (!response.ok) throw new Error(`${response.status}: ${(await response.json()).detail}`);
  const reader = response.body.getReader(), decoder = new TextDecoder();
  let pending = '', result = null, prompt = null;
  const abort = () => prompt?.close();
  signal?.addEventListener('abort', abort, { once: true });
  try {
    while (true) {
      const { done, value } = await reader.read();
      pending += decoder.decode(value || new Uint8Array(), { stream: !done });
      let at;
      while ((at = pending.indexOf('\n\n')) >= 0) {
        const text = pending.slice(0, at); pending = pending.slice(at + 2);
        if (!text.startsWith('data: ')) continue;
        const packet = JSON.parse(text.slice(6));
        if (packet.error) throw new Error(packet.error);
        if (packet.status) { result = packet.status; }
        if (packet.permission) {
          const request = packet.permission;
          const buttons = el('div');
          let answered = false;
          prompt = openPanelDialog({ title: request.title, owner, group: 'forge-app-approval',
            className: 'forge-app-approval', body: el('pre', { class: 'forge-app-command', text: request.description }), footer: buttons,
            onClose: () => { if (!answered) api(`/api/permissions/${encodeURIComponent(request.id)}/answer`, { method: 'POST', body: JSON.stringify({ choice: 'reject' }) }).catch(() => {}); } });
          for (const choice of request.choices) buttons.append(el('button', { class: `btn${choice.behavior === 'allow' ? ' primary' : ''}`, text: choice.label,
            onclick: async () => {
              buttons.querySelectorAll('button').forEach(b => { b.disabled = true; });
              try {
                await api(`/api/permissions/${encodeURIComponent(request.id)}/answer`, { method: 'POST', body: JSON.stringify({ choice: choice.id }) });
                answered = true; prompt.close(); prompt = null;
              } catch (error) { toast(error.message, 'error'); buttons.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
            } }));
        }
      }
      if (done) break;
    }
    if (!result) throw new Error('The action ended before a result arrived.');
    return result;
  } finally { signal?.removeEventListener('abort', abort); prompt?.close(); reader.releaseLock(); }
}

export function configureApp(session, project, owner, onStarted) {
  let dialog, disposed = false;
  const command = el('input', { class: 'forge-app-command-input', 'aria-label': 'App command', placeholder: 'npm run dev', spellcheck: 'false' });
  const suggestions = el('div', { class: 'forge-app-suggestions' });
  const status = el('p', { role: 'alert', class: 'meta' });
  const run = el('button', { class: 'btn primary', text: 'Save and run', disabled: true });
  dialog = openPanelDialog({ title: 'Run app', owner, group: 'forge-app-command', onClose: () => { disposed = true; },
    body: el('div', { class: 'forge-form' }, [el('label', {}, ['Command', command]),
      el('p', { class: 'meta', text: 'Runs in this session\'s worktree. Use {port} for its free port. Shell syntax is not supported.' }), suggestions, status]), footer: run });
  command.oninput = () => { run.disabled = !command.value.trim(); };
  api(`${base(session.id)}/suggest`).then(result => {
    if (disposed) return;
    command.value = result.command || result.suggestions[0] || '';
    run.disabled = !command.value.trim();
    suggestions.replaceChildren(...result.suggestions.map(value => el('button', { class: 'btn quiet', text: value, onclick: () => { command.value = value; run.disabled = false; } })));
  }).catch(error => { if (!disposed) status.textContent = error.message; });
  run.onclick = async () => {
    run.disabled = true;
    try {
      await api(`/api/forge/projects/${encodeURIComponent(project.id)}/app/command`, { method: 'PUT', body: JSON.stringify({ command: command.value }) });
      if (disposed) return;
      dialog.close(); onStarted();
    } catch (error) { status.textContent = error.message; run.disabled = false; }
  };
  return () => dialog.close();
}

export async function mountAppPreview(panel, id) {
  let disposed = false, running = null, nativeOpen = false, live = false, syncing = false, loadedUrl = null, suspended = false;
  const controller = new AbortController();
  const native = bridge();
  const status = el('p', { class: 'forge-preview-status', role: 'status' });
  const path = el('input', { value: '/', class: 'forge-preview-path', 'aria-label': 'Preview path', placeholder: '/', spellcheck: 'false' });
  const origin = el('span', { class: 'forge-preview-origin' });
  const viewport = el('div', { class: 'forge-preview-viewport' });
  const frame = native ? null : el('iframe', { title: 'App Preview iframe fallback', sandbox: 'allow-scripts allow-forms allow-same-origin', referrerpolicy: 'no-referrer' });
  if (frame) viewport.append(frame);
  const fallback = native ? null : el('p', { class: 'meta forge-preview-fallback', text: 'Web client preview uses a sandboxed iframe of localhost, so it shows the app only on the computer running Kairos, not over remote access. Open in browser if the app refuses framing.' });
  const back = el('button', { class: 'btn quiet', text: 'Back', disabled: true });
  const forward = el('button', { class: 'btn quiet', text: 'Forward', disabled: true });
  const reload = el('button', { class: 'btn quiet', text: 'Reload' });
  const external = el('button', { class: 'btn quiet', text: 'Open in browser' });
  const widths = el('div', { class: 'forge-preview-widths', role: 'group', 'aria-label': 'Preview width' });
  let history = [], index = -1, width = 'desktop';
  const logText = el('pre', { class: 'forge-app-logs', 'aria-label': 'App logs' });
  const drawer = el('details', { class: 'forge-preview-logs' }, [el('summary', { text: 'Logs' }),
    el('div', { class: 'forge-preview-log-controls' }, [el('button', { class: 'btn quiet', text: 'Stop', onclick: () => control('stop') }),
      el('button', { class: 'btn quiet', text: 'Restart', onclick: () => control('restart') })]), logText]);
  panel.replaceChildren(el('div', { class: 'forge-preview-toolbar' }, [back, forward, reload,
    el('form', { class: 'forge-preview-address', onsubmit: event => { event.preventDefault(); go(path.value); } }, [origin, path]), external]),
  el('div', { class: 'forge-preview-options' }, [widths]), ...[fallback, status, viewport, drawer].filter(Boolean));
  for (const [value, label] of [['desktop', 'Desktop'], ['tablet', 'Tablet'], ['phone', 'Phone']]) widths.append(el('button', {
    class: 'forge-small-button', text: label, 'aria-pressed': String(value === width), 'data-preview-width': value,
    onclick: () => { width = value; widths.querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.previewWidth === value)));
      viewport.dataset.width = value; native?.setWidth(value); syncBounds(); } }));
  function ownUrl(raw) {
    try { const url = new URL(raw); return running?.running && url.origin === new URL(running.url).origin && !url.username && !url.password ? url : null; } catch { return null; }
  }
  async function go(value, remember = true) {
    if (!running?.ready) return;
    if (!value.startsWith('/') || value.startsWith('//') || value.includes('\\')) { status.textContent = 'Enter a path beginning with / on this app.'; return; }
    const url = ownUrl(new URL(value, running.url).href);
    if (!url) { status.textContent = 'Choose a path on this app.'; return; }
    if (native) { const result = await native.navigate(url.href); if (!result.ok) status.textContent = result.reason || 'Preview unavailable.'; }
    else {
      // Site demo only: its fixtures supply a sample page (demo/fixtures.js); the backend never does.
      if (running.fixture && typeof running.fixture_html === 'string') frame.srcdoc = running.fixture_html;
      else frame.src = url.href;
      if (remember) { history = history.slice(0, index + 1); history.push(url.href); index++; }
      back.disabled = index <= 0; forward.disabled = index >= history.length - 1;
    }
    path.value = url.pathname + url.search + url.hash;
  }
  back.onclick = () => { if (native) native.back(); else if (index > 0) { index--; go(new URL(history[index]).pathname + new URL(history[index]).search, false); } };
  forward.onclick = () => { if (native) native.forward(); else if (index + 1 < history.length) { index++; go(new URL(history[index]).pathname + new URL(history[index]).search, false); } };
  reload.onclick = () => native ? native.reload() : go(path.value, false);
  external.onclick = () => { if (native) native.openExternal(); else { const url = ownUrl(new URL(path.value, running?.url).href); if (url) window.open(url.href, '_blank', 'noopener,noreferrer'); } };
  function suppressed() {
    return suspended || !!panel.closest('[inert]') || [...document.querySelectorAll('.modal-backdrop, .custom-select-menu, .artifact-panel, .settings-window, .chat-image-preview')]
      .some(node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden');
  }
  function syncBounds() {
    if (!native || !nativeOpen || disposed) return;
    const rect = viewport.getBoundingClientRect();
    native.setBounds({ x: rect.x, y: rect.y, width: rect.width, height: rect.height });
    native.setVisible(live && rect.width > 0 && rect.height > 0 && !suppressed());
  }
  async function syncNative() {
    if (!native || syncing || disposed || !live || !running?.ready || nativeOpen) return;
    syncing = true;
    if (activeNative && activeNative !== panel) activeNative._setVisible(false);
    activeNative = panel;
    let result;
    const rect = viewport.getBoundingClientRect();
    try { result = await native.open(id, running.url, { x: rect.x, y: rect.y, width: rect.width, height: rect.height }); }
    catch (error) { result = { ok: false, reason: error.message }; }
    syncing = false;
    if (disposed || !live || activeNative !== panel) { if (activeNative === panel) { native.close(); activeNative = null; } return; }
    nativeOpen = result.ok;
    loadedUrl = result.ok ? running.url : null;
    if (!result.ok) status.textContent = result.reason;
    native.setWidth(width); syncBounds();
  }
  panel._setVisible = visible => {
    live = visible;
    if (!visible && native && activeNative === panel) { native.close(); activeNative = null; nativeOpen = false; }
    if (visible) syncNative(); syncBounds();
  };
  panel._suspend = value => { suspended = value; syncBounds(); };
  async function refresh() {
    try {
      const result = await api(`${base(id)}/status`);
      if (disposed) return;
      running = result;
      external.disabled = reload.disabled = path.disabled = !result.ready;
      origin.textContent = result.url ? new URL(result.url).host : 'Stopped';
      status.textContent = result.ready ? '' : result.running ? 'Starting app...' : 'App stopped. Restart it from Logs.';
      if (!result.running) { if (activeNative === panel) { native.close(); activeNative = null; nativeOpen = false; } if (frame) { frame.removeAttribute('src'); frame.removeAttribute('srcdoc'); } loadedUrl = null; }
      else if (result.ready) {
        if (nativeOpen && loadedUrl !== result.url && activeNative === panel) { native.close(); activeNative = null; nativeOpen = false; }
        if (native) await syncNative();
        else if (loadedUrl !== result.url) { loadedUrl = result.url; await go('/'); }
      }
      if (drawer.open) logText.textContent = (await api(`${base(id)}/logs?limit=2000`)).lines.join('\n');
    } catch (error) { if (!disposed) status.textContent = error.message; }
  }
  async function control(action) {
    drawer.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try { if (action === 'restart') await launchApp(id, 'restart', { signal: controller.signal, owner: panel });
      else { await api(`${base(id)}/stop`, { method: 'POST' }); changed(); }
      await refresh();
    } catch (error) { if (!disposed) status.textContent = error.message; }
    finally { drawer.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
  }
  drawer.ontoggle = () => { syncBounds(); if (drawer.open) refresh(); };
  const unsubscribe = native?.onState(state => {
    if (disposed || activeNative !== panel) return;
    if (!state.open) { nativeOpen = false; return; }
    const url = ownUrl(state.url); if (url) path.value = url.pathname + url.search + url.hash;
    back.disabled = !state.canGoBack; forward.disabled = !state.canGoForward;
  });
  const unsubscribeError = native?.onError(error => { if (activeNative === panel) status.textContent = error.description; });
  const observer = new ResizeObserver(syncBounds); observer.observe(viewport);
  const layers = new MutationObserver(syncBounds); layers.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'hidden', 'inert'] });
  window.addEventListener('resize', syncBounds); document.addEventListener('kairos:forge-apps', refresh);
  const timer = setInterval(refresh, 2000);
  panel._cleanup = () => { disposed = true; controller.abort(); clearInterval(timer); observer.disconnect(); layers.disconnect(); unsubscribe?.(); unsubscribeError?.();
    if (activeNative === panel) { native.close(); activeNative = null; }
    window.removeEventListener('resize', syncBounds); document.removeEventListener('kairos:forge-apps', refresh); };
  await refresh(); panel._setVisible(!!panel.closest('.forge-pane') && !panel.closest('[hidden]'));
}
