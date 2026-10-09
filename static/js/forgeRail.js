import { api, el, openOptionMenu, confirmDialog, toast } from './api.js';
import { openProjectForm } from './forgeProjectForms.js';
import { projectSessions, navigateForge, sessionStatus, changeTotals, diffCounts } from './forgeUi.js';
import { subscribeAll } from './chatStream.js';

export function mountForgeRail(nav) {
  let disposed = false, version = 0, groups = [], selected = null;
  const add = el('button', { type: 'button', class: 'forge-icon-button', text: '+', 'aria-label': 'Add project' });
  const list = el('div', { class: 'forge-rail-projects' });
  const section = el('section', { class: 'forge-project-section', 'aria-label': 'Projects' }, [
    el('header', {}, [el('span', { text: 'Projects' }), add]), list,
  ]);
  nav.append(section);
  add.onclick = () => {
    const menu = openOptionMenu(add, ['Open folder', 'Clone repo', 'New repo'].map((text, i) => ({ text, value: ['existing', 'clone', 'new'][i] })), kind =>
      openProjectForm(kind, { owner: section, opener: add, onSaved: () => document.dispatchEvent(new Event('kairos:forge-projects')) }));
    menu.classList.add('forge-project-menu');
  };
  function draw() {
    if (disposed) return;
    list.replaceChildren(...groups.map(({ project, sessions }) => {
      const busy = sessions.some(s => sessionStatus(s).key === 'working');
      const totals = sessions.filter(s => !s.forge.removed).reduce((sum, s) => {
        const counts = changeTotals(s._changes || []); sum.added += counts.added; sum.removed += counts.removed; return sum;
      }, { added: 0, removed: 0 });
      const row = el('button', { type: 'button', class: `forge-project-row${busy ? ' busy' : ''}`, title: project.path,
        'data-forge-project': project.id, 'aria-label': project.name, 'aria-pressed': String(selected === project.id),
        onclick: () => { selected = project.id; draw(); navigateForge('forgeShell', { projectId: project.id, revealProject: true }); } }, [
        el('span', { class: 'forge-project-mark', text: project.name.slice(0, 1).toUpperCase(), 'aria-hidden': 'true' }),
        el('span', { class: 'forge-project-name', text: project.name }), diffCounts(totals),
      ]);
      const context = () => openOptionMenu(row, [
        { value: 'home', text: 'Open on Home' }, { value: 'path', text: 'Copy path' }, { value: 'remove', text: 'Remove from list' },
      ], async action => {
        try {
          if (action === 'home') { await api(`/api/forge/projects/${encodeURIComponent(project.id)}/opened`, { method: 'POST' }); navigateForge('forgeHome', { projectId: project.id }); }
          if (action === 'path') { await navigator.clipboard.writeText(project.path); toast('Project path copied.', 'success'); }
          if (action === 'remove' && await confirmDialog({ title: 'Remove project from list?', message: `Remove ${project.name} from Forge? Its files stay in their folder.`, confirmLabel: 'Remove from list' })) {
            if (disposed) return;
            await api(`/api/forge/projects/${encodeURIComponent(project.id)}`, { method: 'DELETE' });
            document.dispatchEvent(new CustomEvent('kairos:forge-projects', { detail: { removed: project.id } }));
          }
        } catch (error) { toast(error.message, 'error'); }
      });
      row.oncontextmenu = event => { event.preventDefault(); context(); };
      row.onkeydown = event => { if (event.key === 'ContextMenu' || (event.shiftKey && event.key === 'F10')) { event.preventDefault(); context(); } };
      return row;
    }));
    if (!groups.length) list.append(el('p', { class: 'meta', text: 'Open a folder to begin.' }));
  }
  async function refresh() {
    const at = ++version;
    try {
      const next = await projectSessions(await api('/api/forge/projects'));
      await Promise.allSettled(next.flatMap(g => g.sessions.filter(s => !s.forge.removed).map(async s => {
        s._changes = (await api(`/api/forge/sessions/${encodeURIComponent(s.id)}/changes`)).files;
      })));
      if (disposed || at !== version) return;
      groups = next; draw();
    } catch { if (!disposed) list.textContent = 'Projects could not load.'; }
  }
  const selection = event => { selected = event.detail.projectId; draw(); };
  const unsubscribe = subscribeAll(() => { draw(); });
  document.addEventListener('kairos:forge-project-selected', selection);
  document.addEventListener('kairos:forge-projects', refresh);
  document.addEventListener('kairos:forge-review', refresh);
  const timer = setInterval(() => { if (!document.hidden) refresh(); }, 30000);
  refresh();
  return () => { disposed = true; ++version; clearInterval(timer); unsubscribe(); document.removeEventListener('kairos:forge-project-selected', selection); document.removeEventListener('kairos:forge-projects', refresh); document.removeEventListener('kairos:forge-review', refresh); section.remove(); };
}
