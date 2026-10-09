import { api, el, openPanelDialog } from './api.js';

export async function openProjectForm(kind, { owner, opener, onSaved } = {}) {
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
        formDialog.close(); onSaved?.();
      } catch (error) { status.textContent = error.message.replace(/^\d+: /, ''); }
      finally { submit.disabled = false; }
    };
    submit.onclick = save;
    body.onsubmit = event => { event.preventDefault(); if (!submit.disabled) save(); };
    formDialog = openPanelDialog({ title: kind === 'existing' ? 'Add existing folder' : kind === 'clone' ? 'Clone repository' : 'New project', body, footer: submit, owner, opener, initialFocus: kind === 'existing' ? path : name, group: 'forge' });
  }
