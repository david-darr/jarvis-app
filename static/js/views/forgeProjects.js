import { api, el, toast, openPanelDialog } from '../api.js';

const navigate = (tab, options = {}) => document.dispatchEvent(new CustomEvent('jarvis:navigate', { detail: { tab, ...options } }));
export function render(container) {
  container.replaceChildren();
  let disposed = false, dialog = null;
  const page = el('div', { class: 'view-constrained forge-projects' });
  const actions = el('div', { class: 'forge-project-actions' });
  const list = el('div', { class: 'forge-project-list' }, [el('p', { class: 'meta', text: 'Loading projects...' })]);
  page.append(el('header', { class: 'view-header' }, [el('div', {}, [el('h2', { text: 'Projects' }), el('p', { class: 'sub', text: 'Local folders for the work you build.' })]), actions]), list);
  container.append(page);
  async function refresh() {
    try {
      const projects = await api('/api/forge/projects');
      if (disposed) return;
      list.replaceChildren(...(projects.length ? projects.map(p => {
        const git = p.git || {};
        return el('article', { class: 'forge-project-card', 'data-forge-project': p.id }, [
          el('div', { class: 'forge-project-copy' }, [el('h3', { text: p.name }), el('p', { class: 'forge-path', text: p.path }),
            el('p', { class: 'meta', text: ['ok', 'empty'].includes(git.state) ? `${git.branch} · ${git.last_commit ? git.last_commit.subject + ' · ' + new Date(git.last_commit.at * 1000).toLocaleDateString() : 'No commits yet'}` : git.message || 'Repository unavailable.' })]),
          el('div', { class: 'forge-project-actions' }, [el('button', { type: 'button', class: 'btn', text: 'Open on Home', onclick: async () => {
            try { await api(`/api/forge/projects/${p.id}/opened`, { method: 'POST' }); if (!disposed) navigate('forgeHome', { projectId: p.id }); } catch { /* api displays the error. */ }
          } }), el('button', { type: 'button', class: 'btn quiet', text: 'Remove', onclick: async () => {
            try { await api(`/api/forge/projects/${p.id}`, { method: 'DELETE' }); toast('Removed from Forge. Your files stay in their folder.', 'success'); refresh(); } catch { /* api displays the error. */ }
          } })]),
        ]);
      }) : [el('p', { class: 'meta', text: 'Add a folder, clone a repository, or create your first project.' })]));
    } catch { if (!disposed) list.textContent = 'Projects could not load. Open Projects again to retry.'; }
  }
  async function form(kind) {
    const body = el('form', { class: 'forge-form', 'data-forge-form': kind });
    const status = el('p', { class: 'meta', role: 'status' });
    const name = el('input', { id: 'forge-project-name', maxlength: '120', 'aria-label': 'Project name' });
    const path = el('input', { id: 'forge-project-path', 'aria-label': 'Folder path', placeholder: 'Absolute folder path' });
    const url = el('input', { id: 'forge-clone-url', 'aria-label': 'Repository URL', placeholder: 'https://host/owner/repo or git@host:owner/repo.git' });
    const field = (label, input) => el('label', { class: 'field' }, [el('span', { text: label }), input]);
    body.append(field(kind === 'existing' ? 'Name (optional)' : 'Project name', name));
    if (kind === 'existing') {
      body.append(field('Folder path', path));
      const folders = el('div', { class: 'forge-folder-list' });
      const browse = async (target = '') => {
        try {
          const result = await api('/api/workspace/browse?path=' + encodeURIComponent(target));
          if (!body.isConnected) return;
          path.value = result.path;
          folders.replaceChildren(el('strong', { class: 'forge-path', text: result.path }),
            ...(result.parent ? [el('button', { type: 'button', class: 'btn quiet', text: 'Parent folder', onclick: () => browse(result.parent) })] : []),
            ...result.dirs.map(dir => el('button', { type: 'button', class: 'btn quiet', text: dir.name, onclick: () => browse(dir.path) })));
          if (result.truncated) folders.append(el('p', { class: 'meta', text: 'Showing the first 500 folders. Type a path to go further.' }));
        } catch (error) { status.textContent = error.message; }
      };
      body.append(el('button', { type: 'button', class: 'btn', text: 'Browse folders', onclick: () => browse(path.value) }), folders);
      api('/api/forge/workspaces/recent').then(rows => {
        if (!body.isConnected) return;
        const recent = el('div', { class: 'forge-folder-list' }, [el('strong', { text: 'Recent chat workspaces' })]);
        rows.forEach(row => recent.append(el('button', { type: 'button', class: 'btn quiet forge-path', text: row.path, onclick: () => { path.value = row.path; } })));
        if (!rows.length) recent.append(el('p', { class: 'meta', text: 'No recent workspace folders.' }));
        body.append(recent);
      }).catch(() => { status.textContent = 'Recent workspaces could not load. You can still type a path.'; });
    } else {
      if (kind === 'clone') body.append(field('Repository URL', url));
      const root = el('p', { class: 'meta forge-path', text: 'Reading Forge root...' }); body.append(root);
      api('/api/forge/root').then(value => { root.textContent = `New folder under ${value.path}`; }).catch(() => { root.textContent = 'Forge root unavailable. Check Settings.'; });
    }
    body.append(status);
    const submit = el('button', { type: 'button', class: 'btn primary', text: kind === 'existing' ? 'Add project' : kind === 'clone' ? 'Clone repository' : 'Create project' });
    let formDialog;
    const save = async () => {
      if (submit.disabled) return;
      const payload = kind === 'existing' ? { name: name.value, path: path.value } : kind === 'clone' ? { name: name.value, url: url.value } : { name: name.value };
      if (kind === 'existing' ? !path.value.trim() : kind === 'clone' ? !url.value.trim() : !name.value.trim()) { status.textContent = 'Fill in the required field.'; return; }
      submit.disabled = true; status.textContent = kind === 'clone' ? 'Cloning repository...' : 'Adding project...';
      try {
        if (kind === 'existing') {
          const vetted = await api('/api/workspace/vet?path=' + encodeURIComponent(path.value));
          if (!vetted.ok) throw new Error('Choose a usable workspace folder.');
          payload.path = vetted.path;
        }
        await api('/api/forge/projects' + (kind === 'existing' ? '' : '/' + kind), { method: 'POST', body: JSON.stringify(payload) });
        formDialog.close(); refresh();
      } catch (error) { status.textContent = error.message.replace(/^\d+: /, ''); }
      finally { submit.disabled = false; }
    };
    submit.onclick = save;
    body.onsubmit = event => { event.preventDefault(); if (!submit.disabled) save(); };
    formDialog = dialog = openPanelDialog({ title: kind === 'existing' ? 'Add existing folder' : kind === 'clone' ? 'Clone repository' : 'New project', body, footer: submit, owner: page, initialFocus: kind === 'existing' ? path : name, group: 'forge' });
  }
  for (const [kind, text] of [['existing', 'Add existing'], ['clone', 'Clone'], ['new', 'New']]) actions.append(el('button', { type: 'button', class: kind === 'new' ? 'btn primary' : 'btn', text, 'data-forge-add': kind, onclick: () => form(kind) }));
  refresh();
  return () => { disposed = true; dialog?.close(); };
}
