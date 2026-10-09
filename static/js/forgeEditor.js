import { api, el, toast, confirmDialog, openPanelDialog } from './api.js';

let vendor;
export const forgeVendor = () => vendor ||= import('./vendor/forge-vendor.js');

export async function mountForgeEditor(panel, tab, changed) {
  let disposed = false, view, dialog, saving = false;
  panel._cleanup = () => { disposed = true; view?.destroy(); dialog?.close(); };
  const [cm, latest] = await Promise.all([forgeVendor(), api(`${tab.base}/file?${new URLSearchParams({ path: tab.path })}`)]);
  if (disposed) return;
  let file = tab.draft != null ? tab.opened : latest;
  tab.opened = file;
  if (file.binary) { panel.textContent = `${file.path}: Binary file. Read only.`; return; }
  const status = el('span', { role: 'status', class: 'meta' });
  const save = el('button', { class: 'btn', text: 'Save', 'aria-label': 'Save file', onclick: () => store() });
  const editor = el('div', { class: 'forge-editor-host' });
  panel.replaceChildren(el('div', { class: 'forge-file-title forge-editor-toolbar' }, [el('span', { text: file.path }), status, save]), editor);
  const ext = file.path.split('.').pop().toLowerCase();
  const languages = { c: cm.cpp, h: cm.cpp, cpp: cm.cpp, hpp: cm.cpp, css: cm.css, go: cm.go, html: cm.html, svg: cm.html,
    java: cm.java, js: cm.javascript, cjs: cm.javascript, mjs: cm.javascript, jsx: () => cm.javascript({ jsx: true }),
    ts: () => cm.javascript({ typescript: true }), tsx: () => cm.javascript({ typescript: true, jsx: true }),
    json: cm.json, md: cm.markdown, py: cm.python, rs: cm.rust, sql: cm.sql, yaml: cm.yaml, yml: cm.yaml };
  const normal = text => text.replace(/\r\n/g, '\n');
  const update = () => {
    tab.dirty = view.state.doc.toString() !== normal(file.content);
    tab.draft = tab.dirty ? view.state.doc.toString() : null;
    if (tab.dirty) tab.preview = false;
    save.disabled = saving || !tab.dirty; status.textContent = tab.dirty ? 'Unsaved' : 'Saved'; changed();
  };
  view = new cm.EditorView({ parent: editor, state: cm.EditorState.create({ doc: tab.draft ?? file.content,
    extensions: [cm.basicSetup, languages[ext]?.() || [],
      cm.keymap.of([{ key: 'Mod-s', run: () => { store(); return true; } }]),
      cm.EditorView.theme({ '&': { height: '100%', color: 'var(--text)', backgroundColor: 'var(--bg-panel-solid)' },
        '.cm-scroller': { overflow: 'auto', fontFamily: 'var(--font-mono, monospace)' },
        '.cm-content': { caretColor: 'var(--text)' }, '.cm-cursor': { borderLeftColor: 'var(--text)' },
        '.cm-gutters': { backgroundColor: 'var(--surface-2)', color: 'var(--text-dim)', borderColor: 'var(--border)' },
        '.cm-activeLine, .cm-activeLineGutter': { backgroundColor: 'var(--accent-dim)' },
        '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, ::selection': { backgroundColor: 'var(--accent-dim)' },
        '.cm-panels, .cm-tooltip': { backgroundColor: 'var(--bg-panel-solid)', color: 'var(--text)' } }),
      cm.syntaxHighlighting(cm.HighlightStyle.define([
        { tag: [cm.tags.keyword, cm.tags.operator], color: 'var(--accent)' },
        { tag: [cm.tags.string, cm.tags.number], color: 'var(--text)' },
        { tag: cm.tags.comment, color: 'var(--text-dim)', fontStyle: 'italic' },
      ])), cm.EditorView.updateListener.of(event => { if (event.docChanged) update(); })] }) });
  panel._editor = view;
  panel._beforeClose = () => !tab.dirty || confirmDialog({ title: 'Close unsaved file?', message: `Lose your unsaved edits to ${tab.path}?`, confirmLabel: 'Close without saving' });
  update();
  async function store(overwrite = false) {
    if (disposed || saving || !tab.dirty) return;
    saving = true; save.disabled = true;
    const text = view.state.doc.toString(), content = file.content.includes('\r\n') ? text.replace(/\n/g, '\r\n') : text;
    try {
      const result = await api(`${tab.base}/file`, { method: 'PUT', toast: false, body: JSON.stringify({ path: tab.path, content, hash: file.hash, mtime: file.mtime, overwrite }) });
      if (disposed) return;
      file = result; tab.opened = result; update();
      document.dispatchEvent(new Event('kairos:forge-review'));
    } catch (error) {
      if (disposed) return;
      status.textContent = error.message;
      const conflict = error.message.startsWith('409:') && /file changed/i.test(error.message);
      if (!conflict) toast(error.message.replace(/^\d+: /, ''), 'error');
      if (conflict) {
        dialog = openPanelDialog({ title: 'File changed on disk', owner: panel, body: el('p', { text: 'The file changed after you opened it. Overwrite it with your edits, or reload and lose your edits.' }),
          footer: el('div', {}, [el('button', { class: 'btn danger', text: 'Overwrite', onclick: () => { dialog.close(); store(true); } }),
            el('button', { class: 'btn', text: 'Reload', onclick: async () => {
              try { const result = await api(`${tab.base}/file?${new URLSearchParams({ path: tab.path })}`); if (disposed) return;
                if (result.binary) throw new Error('The file is now binary. Close and reopen it.');
                file = result; tab.opened = result; view.dispatch({ changes: { from: 0, to: view.state.doc.length, insert: result.content } }); update(); dialog.close();
              } catch (problem) { status.textContent = problem.message; }
            } })]) });
      }
    } finally { saving = false; if (!disposed) save.disabled = !tab.dirty; }
  }
}
