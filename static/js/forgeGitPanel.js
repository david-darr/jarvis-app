import { api, el, customSelect } from './api.js';
import { permissionAction } from './forgeAppPreview.js';
import { forgeRunning } from './forgeUi.js';

export function mountGitPanel(panel, id, { onChanged, onMerged } = {}) {
  const base = `/api/forge/sessions/${encodeURIComponent(id)}/git`;
  const controller = new AbortController();
  let disposed = false, busy = false, version = 0, commitDraft = '', branchDraft = '';
  const status = el('p', { class: 'meta', role: 'status' });
  const content = el('div', { class: 'forge-git-content' });
  panel.replaceChildren(status, content);
  const button = (text, action, extra = {}) => el('button', { class: 'forge-small-button', text, disabled: busy || forgeRunning(id), onclick: () => run(action, extra) });
  async function run(action, extra) {
    if (busy || disposed || forgeRunning(id)) return;
    busy = true; content.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      if (['push', 'merge'].includes(action)) await permissionAction(base, { body: { action, ...extra }, signal: controller.signal, owner: panel });
      else await api(base, { method: 'POST', body: JSON.stringify({ action, ...extra }) });
      if (disposed) return;
      if (action === 'commit') commitDraft = '';
      if (action === 'create-branch') branchDraft = '';
      status.textContent = action === 'merge' ? 'Merged back. You can now end the session and remove its worktree.' : 'Git updated.';
      onChanged?.(); if (action === 'merge') onMerged?.();
    } catch (error) { if (!disposed) status.textContent = error.message.replace(/^\d+: /, ''); }
    finally { busy = false; if (!disposed) refresh(); }
  }
  async function refresh() {
    if (disposed || busy) return;
    if (content.contains(document.activeElement) && ['INPUT', 'TEXTAREA'].includes(document.activeElement.tagName)) return;
    const current = ++version;
    try {
      const state = await api(base);
      if (disposed || current !== version) return;
      const files = title => el('section', { class: 'forge-git-files' }, [el('strong', { text: title }),
        ...state.files.filter(f => title === 'Staged' ? f.staged : f.unstaged).map(f => el('div', { class: 'forge-git-file' }, [
          el('code', { text: f.path }), button(title === 'Staged' ? 'Unstage' : 'Stage', title === 'Staged' ? 'unstage' : 'stage', { path: f.path }),
        ]))]);
      const message = el('textarea', { placeholder: 'Commit message', 'aria-label': 'Commit message', rows: '3' });
      message.value = commitDraft;
      const commit = button(state.files.some(f => f.staged) ? 'Commit staged files' : 'Commit all changes', 'commit');
      commit.disabled = !commitDraft.trim() || !state.files.length || busy || forgeRunning(id);
      message.oninput = () => { commitDraft = message.value; commit.disabled = !message.value.trim() || !state.files.length || busy || forgeRunning(id); };
      commit.onclick = () => run('commit', { message: message.value });
      const branches = customSelect({}, state.branches.map(name => el('option', { value: name, text: name })));
      branches.value = state.branch; branches.querySelector('button').setAttribute('aria-label', 'Git branch');
      branches.addEventListener('change', () => run('switch', { branch: branches.value }));
      const branch = el('input', { placeholder: 'New branch name', 'aria-label': 'New branch name' });
      branch.value = branchDraft; branch.oninput = () => { branchDraft = branch.value; };
      const create = button('Create branch', 'create-branch'); create.onclick = () => run('create-branch', { branch: branch.value });
      content.replaceChildren(el('p', { class: 'forge-branch', text: `${state.branch} · ${state.ahead} ahead, ${state.behind} behind${state.upstream ? ` · ${state.upstream}` : ' · No upstream'}` }),
        el('div', { class: 'forge-git-actions' }, [button('Refresh', 'refresh'), button('Stage all', 'stage'), button('Unstage all', 'unstage')]),
        files('Staged'), files('Unstaged'), message, commit,
        el('div', { class: 'forge-git-actions' }, [button('Push', 'push'), button('Pull', 'pull'), button('Merge back', 'merge')]),
        branches, branch, create);
      content.querySelector('button').onclick = refresh;
    } catch (error) { if (!disposed && current === version) status.textContent = error.message; }
  }
  document.addEventListener('kairos:forge-review', refresh);
  const timer = setInterval(() => { if (!panel.hidden && !document.hidden) refresh(); }, 30000);
  refresh();
  return () => { disposed = true; ++version; controller.abort(); clearInterval(timer); document.removeEventListener('kairos:forge-review', refresh); };
}
