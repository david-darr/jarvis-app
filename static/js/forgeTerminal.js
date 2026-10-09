import { api, el } from './api.js';
import { forgeVendor } from './forgeEditor.js';

export async function mountForgeTerminal(panel, tab, onNew) {
  let disposed = false, terminal, fit, retry, visible = true, resizing, pending = '', sending = Promise.resolve(), flushTimer;
  let cursor = 0, closed = false;
  const base = `/api/forge/sessions/${encodeURIComponent(tab.sessionId)}/terminals/${encodeURIComponent(tab.terminalId)}`;
  const controller = new AbortController();
  const status = el('p', { class: 'meta', role: 'status' });
  const host = el('div', { class: 'forge-terminal-host' });
  const toolbar = el('div', { class: 'forge-terminal-toolbar' }, [
    el('button', { class: 'btn quiet', text: 'New terminal', onclick: () => onNew(tab.sessionId) }),
    el('button', { class: 'btn quiet', text: 'Copy', onclick: async () => { try { await navigator.clipboard.writeText(terminal?.getSelection() || ''); } catch (error) { status.textContent = error.message; } } }),
    el('button', { class: 'btn quiet', text: 'Paste', onclick: async () => { try { terminal?.paste(await navigator.clipboard.readText()); } catch (error) { status.textContent = error.message; } } }),
    el('span', { class: 'meta forge-terminal-phone-note', text: 'A keyboard helps on a phone.' }),
  ]);
  panel.replaceChildren(toolbar, status, host);
  panel._cleanup = () => { disposed = true; controller.abort(); clearTimeout(retry); clearTimeout(resizing); clearTimeout(flushTimer); observer?.disconnect(); themeObserver?.disconnect(); terminal?.dispose(); };
  panel._closeTerminal = () => api(base, { method: 'DELETE' }).catch(error => { status.textContent = error.message; throw error; });
  let observer, themeObserver;
  if (!document.querySelector('[data-forge-xterm-style]')) document.head.append(el('link', { rel: 'stylesheet', href: '/static/js/vendor/xterm.css', 'data-forge-xterm-style': '' }));
  const { Terminal, FitAddon, WebLinksAddon } = await forgeVendor();
  if (disposed) return;
  function theme() {
    const css = getComputedStyle(panel), color = key => css.getPropertyValue(key).trim();
    return { background: color('--bg-panel-solid'), foreground: color('--text'),
      cursor: color('--text'), cursorAccent: color('--bg-panel-solid'), selectionBackground: color('--accent-dim') };
  }
  terminal = new Terminal({ cursorBlink: true, fontSize: 13, fontFamily: getComputedStyle(panel).getPropertyValue('--font-mono').trim() || 'monospace', theme: theme(), allowProposedApi: false });
  fit = new FitAddon(); terminal.loadAddon(fit);
  terminal.loadAddon(new WebLinksAddon((_event, url) => {
    if (!/^https?:\/\//i.test(url)) return;
    if (window.jarvis?.forgeTerminal) window.jarvis.forgeTerminal.openExternal(url).catch(error => { status.textContent = error.message; });
    else window.open(url, '_blank', 'noopener,noreferrer');
  }));
  terminal.open(host); panel._terminal = terminal;
  function resize() {
    if (disposed || !visible || !host.clientWidth || !host.clientHeight) return;
    fit.fit(); clearTimeout(resizing);
    resizing = setTimeout(() => {
      if (closed || disposed) return;
      api(`${base}/resize`, { method: 'POST', body: JSON.stringify({ rows: Math.max(2, Math.min(300, terminal.rows)), cols: Math.max(2, Math.min(500, terminal.cols)) }) }).catch(error => { if (!disposed) status.textContent = error.message; });
    }, 100);
  }
  observer = new ResizeObserver(resize); observer.observe(host);
  themeObserver = new MutationObserver(() => { terminal.options.theme = theme(); resize(); });
  themeObserver.observe(document.documentElement, { attributes: true });
  themeObserver.observe(document.body, { attributes: true });
  panel._setVisible = value => { visible = value; if (value) requestAnimationFrame(resize); };
  terminal.onData(data => {
    if (closed || disposed) return;
    pending += data;
    clearTimeout(flushTimer);
    flushTimer = setTimeout(() => {
      const data = pending; pending = '';
      // Keep input POSTs ordered. A paste is chunked to the server's byte cap.
      const chunks = []; let pendingChunk = '';
      for (const char of data) { pendingChunk += char; if (pendingChunk.length >= 8192) { chunks.push(pendingChunk); pendingChunk = ''; } }
      if (pendingChunk) chunks.push(pendingChunk);
      for (const chunk of chunks) {
        sending = sending.then(() => disposed ? null : api(`${base}/input`, { method: 'POST', body: JSON.stringify({ data: chunk }) }))
          .catch(error => { if (!disposed) status.textContent = error.message; });
      }
    }, 16);
  });
  resize(); terminal.focus(); connect();
  async function connect() {
    if (disposed || closed) return;
    let reader;
    try {
      const response = await fetch(`${base}/output?after=${cursor}`, { signal: controller.signal });
      if (!response.ok) { closed = true; throw new Error((await response.json()).detail || 'Terminal unavailable.'); }
      reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
      status.textContent = '';
      while (!disposed) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        let at;
        while ((at = buffer.indexOf('\n\n')) >= 0) {
          const packet = buffer.slice(0, at); buffer = buffer.slice(at + 2);
          const lines = packet.split('\n');
          const event = lines.find(l => l.startsWith('event: '))?.slice(7);
          if (event === 'reset') terminal.reset();
          if (event === 'closed') { closed = true; status.textContent = 'Terminal closed.'; }
          const data = lines.find(l => l.startsWith('data: '));
          if (data && !event) {
            const id = Number(lines.find(l => l.startsWith('id: '))?.slice(4));
            if (id > cursor) { terminal.write(JSON.parse(data.slice(6)).output); cursor = id; }
          }
        }
        if (done || closed) break;
      }
    } catch (error) { if (!disposed) status.textContent = closed ? error.message : 'Connection interrupted. Reconnecting...'; }
    finally { await reader?.cancel().catch(() => {}); reader?.releaseLock(); }
    if (!disposed && !closed) retry = setTimeout(connect, 1000);
  }
}
