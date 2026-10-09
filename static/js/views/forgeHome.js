import { api, el, customSelect, openPanelDialog, toast } from '../api.js';
import { mountDither } from '../dither.js';
import { halftoneSource, halftoneFocus, halftonePalette, halftoneVersion } from '../appearance.js';
import { bars, donut, heatmap, statCard, rankedTable } from '../forgeCharts.js';

const navigate = (tab, options = {}) => document.dispatchEvent(new CustomEvent('jarvis:navigate', { detail: { tab, ...options } }));
const widgetNames = { working: 'Working now', activity: 'Git activity', recent: 'Recent projects', lifespan: 'Repo lifespan' };
const widgetsKey = 'kairos:forge-widgets';
function panel(title, body, control) {
  return el('section', { class: 'forge-panel' }, [el('header', { class: 'forge-panel-header' }, [el('h2', { text: title }), control]), body]);
}
function projectPicker(projects, label) {
  const picker = customSelect({}, projects.map(p => el('option', { value: p.id, text: p.name })));
  picker.querySelector('button').setAttribute('aria-label', label);
  return picker;
}

export function render(container, tabId, options = {}) {
  container.replaceChildren();
  const page = el('div', { class: 'view-constrained forge-home' });
  const banner = el('div', { class: 'forge-banner', 'aria-hidden': 'true' });
  const message = el('textarea', { id: 'forge-message', rows: '3', placeholder: 'Describe a task, a bug to fix, or an idea to try.', 'aria-label': 'Message your agent' });
  const pickers = el('div', { class: 'forge-composer-controls' });
  const send = el('button', { type: 'submit', class: 'btn primary', text: 'Start chat', disabled: true });
  const composer = el('form', { class: 'forge-composer' }, [message, el('div', { class: 'forge-composer-bottom' }, [pickers, send])]);
  const hero = el('section', { class: 'forge-hero' }, [banner, el('div', { class: 'forge-hero-copy' }, [
    el('div', { class: 'eyebrow' }, ['Forge ', el('small', { class: 'forge-preview', text: 'Preview' })]),
    el('h1', { text: 'What should your agents work on?' }), composer,
    el('p', { class: 'meta', text: 'Start a Kairos chat in your project folder.' }),
  ])]);
  const grid = el('div', { class: 'forge-widget-grid' });
  const customize = el('button', { type: 'button', class: 'btn quiet', text: 'Customize' });
  page.append(hero, el('div', { class: 'forge-toolbar' }, [el('span', { class: 'meta', text: 'Your work, in view' }), customize]), grid);
  container.append(page);
  let disposed = false, dialog = null, projectSelect = null, modelSelect = null, lifespanSelect = null, projects = [], busy = false;
  let hidden = [];
  try { hidden = JSON.parse(localStorage.getItem(widgetsKey) || '[]'); if (!Array.isArray(hidden)) hidden = []; } catch { /* Defaults remain visible. */ }
  const bodies = Object.fromEntries(Object.keys(widgetNames).map(key => [key, el('div', { class: 'forge-widget-body' }, [el('p', { class: 'meta', text: 'Loading...' })])]));
  const panels = Object.fromEntries(Object.entries(widgetNames).map(([key, title]) => [key, panel(title, bodies[key])]));
  panels.lifespan.classList.add('forge-lifespan');
  const arrange = () => grid.replaceChildren(...Object.keys(widgetNames).filter(key => !hidden.includes(key)).map(key => panels[key]));
  arrange();
  customize.onclick = () => {
    const body = el('div', { class: 'forge-form' });
    for (const [key, title] of Object.entries(widgetNames)) {
      const checkbox = el('input', { type: 'checkbox', checked: !hidden.includes(key), 'data-widget-toggle': key });
      checkbox.onchange = () => {
        hidden = checkbox.checked ? hidden.filter(k => k !== key) : [...new Set([...hidden, key])];
        try { localStorage.setItem(widgetsKey, JSON.stringify(hidden)); } catch { toast('Widget preferences could not be saved on this device.', 'error'); }
        arrange();
      };
      body.append(el('label', { class: 'workspace-row' }, [checkbox, title]));
    }
    dialog = openPanelDialog({ title: 'Customize Forge Home', body, owner: page, group: 'forge' });
  };
  function drawBanner() {
    banner.dataset.halftoneSrc = halftoneSource('home');
    const focus = halftoneFocus('home');
    return mountDither(banner, halftoneSource('home'), { cell: 4, fade: [.7, 1], focusX: focus?.x ?? .7, focusY: focus?.y ?? .4,
      palette: halftonePalette(), version: halftoneVersion('home') });
  }
  let disposeBanner = drawBanner();
  const appearance = () => { disposeBanner(); disposeBanner = drawBanner(); };
  document.addEventListener('kairos:appearance', appearance);
  let summaryVersion = 0;
  async function lifespan() {
    const version = ++summaryVersion, body = bodies.lifespan;
    if (!lifespanSelect?.value) { body.textContent = 'Add a project to see its repository history.'; return; }
    body.replaceChildren(el('p', { class: 'meta', text: 'Reading repository...' }));
    try {
      const result = await api(`/api/forge/projects/${lifespanSelect.value}/summary`);
      if (disposed || version !== summaryVersion) return;
      if (!['ok', 'empty'].includes(result.state)) { body.textContent = result.message || 'Repository unavailable.'; return; }
      const life = result.lifespan;
      body.replaceChildren(el('div', { class: 'forge-stats' }, [
        statCard('Commits', life.commits, life.delta.commits), statCard('Contributors', life.contributors, life.delta.contributors),
        statCard('Lines added', life.added, life.delta.added), statCard('Lines removed', life.removed, life.delta.removed), statCard('Age in days', life.age_days, life.delta.age_days),
      ]), el('p', { class: 'meta', text: life.first_commit ? `First commit ${new Date(life.first_commit * 1000).toLocaleDateString()} · Latest ${new Date(life.last_commit * 1000).toLocaleDateString()}` : 'No commits yet. Your repository is ready for its first commit.' }),
      el('h3', { text: `Commits over time · ${life.bucket}` }), bars(life.buckets, 'Repo lifespan'),
      el('div', { class: 'forge-analysis-grid' }, [panel('Languages', donut(result.languages)), panel('Commit rhythm', heatmap(result.rhythm)),
        panel('Most changed files', rankedTable(result.hotspots, 'path', 'Most changed files')), panel('Contributors', rankedTable(result.contributors, 'name', 'Contributors'))]));
    } catch (error) { if (!disposed && version === summaryVersion) body.textContent = `Could not read repository: ${error.message}`; }
  }
  async function refreshActivity() {
    if (disposed || busy) return;
    busy = true;
    try {
      const result = await api('/api/forge/activity');
      if (disposed) return;
      bodies.working.replaceChildren(...(result.working.length ? result.working.map(s => el('button', { type: 'button', class: 'dashboard-row', onclick: () => navigate('chat', { sessionId: s.id }) }, [
        el('span', { class: 'status-dot ok' }), el('span', { class: 'dashboard-row-copy' }, [el('strong', { text: s.title }), el('small', { class: 'meta', text: s.project_name })])])) : [el('p', { class: 'meta', text: 'No chat turns running in your projects.' })]));
      const totals = result.days.reduce((sum, row) => ({ commits: sum.commits + row.commits, added: sum.added + row.added, removed: sum.removed + row.removed }), { commits: 0, added: 0, removed: 0 });
      bodies.activity.replaceChildren(el('p', { class: 'forge-activity-total', text: `${totals.commits} commits · 14 days` }),
        el('p', { class: 'meta', text: `+${totals.added.toLocaleString()} / -${totals.removed.toLocaleString()} lines` }), bars(result.days, 'Git activity'));
      const unavailable = result.projects.filter(p => !['ok', 'empty'].includes(p.state));
      for (const p of unavailable) bodies.activity.append(el('p', { class: 'meta', text: `${p.name}: ${p.message || 'Repository unavailable.'}` }));
    } catch { if (!disposed) for (const key of ['working', 'activity']) bodies[key].textContent = 'Could not load this widget. Open Home again to retry.'; }
    finally { busy = false; }
  }
  async function load() {
    try {
      const results = await Promise.allSettled([api('/api/forge/projects'), api('/api/models/choices')]);
      if (disposed) return;
      if (results[0].status === 'rejected') throw results[0].reason;
      projects = results[0].value;
      const models = results[1].status === 'fulfilled' ? results[1].value : [];
      projectSelect = projectPicker(projects, 'Project for new chat');
      modelSelect = customSelect({}, models.map(m => el('option', { value: m.id, text: `${m.name} · ${m.model || 'CLI default'}` })));
      modelSelect.querySelector('button').setAttribute('aria-label', 'Model for new chat');
      projectSelect.disabled = !projects.length; modelSelect.disabled = !models.length;
      pickers.append(projectSelect, modelSelect);
      send.disabled = !projects.length || !models.length;
      if (!projects.length || !models.length) pickers.append(el('button', { type: 'button', class: 'btn quiet', text: !projects.length ? 'Add a project' : 'Add a model', onclick: () => navigate(!projects.length ? 'forgeProjects' : 'settings', { section: 'add-models' }) }));
      lifespanSelect = projectPicker(projects, 'Repository for lifespan');
      lifespanSelect.disabled = !projects.length;
      if (options.projectId && projects.some(p => p.id === options.projectId)) projectSelect.value = lifespanSelect.value = options.projectId;
      panels.lifespan.querySelector('header').append(lifespanSelect);
      lifespanSelect.addEventListener('change', lifespan);
      bodies.recent.replaceChildren(...(projects.length ? projects.slice(0, 5).map(p => el('button', { type: 'button', class: 'dashboard-row', onclick: async () => {
        try { await api(`/api/forge/projects/${p.id}/opened`, { method: 'POST' }); if (!disposed) { projectSelect.value = lifespanSelect.value = p.id; lifespan(); message.focus(); } } catch { /* api displays the error. */ }
      } }, [el('span', { class: 'dashboard-row-copy' }, [el('strong', { text: p.name }), el('small', { class: 'meta', text: p.path })])])) : [el('p', { class: 'meta', text: 'Add an existing folder, clone a repository, or start a new project.' }), el('button', { type: 'button', class: 'btn', text: 'Open Projects', onclick: () => navigate('forgeProjects') })]));
      lifespan(); refreshActivity();
    } catch { if (!disposed) grid.replaceChildren(el('p', { role: 'status', text: 'Forge could not load. Open Home again to retry.' })); }
  }
  composer.onsubmit = async event => {
    event.preventDefault();
    if (send.disabled || !message.value.trim()) return;
    const project = projects.find(p => p.id === projectSelect.value), text = message.value.trim(), model = modelSelect.value;
    if (!project || !model) return;
    send.disabled = true;
    let session;
    try {
      await api(`/api/forge/projects/${project.id}/opened`, { method: 'POST' });
      session = await api('/api/sessions', { method: 'POST', body: JSON.stringify({ title: text.slice(0, 60) }) });
      await api(`/api/sessions/${session.id}/workspace`, { method: 'POST', body: JSON.stringify({ path: project.path }) });
      await api(`/api/sessions/${session.id}/model`, { method: 'POST', body: JSON.stringify({ model_endpoint_id: model }) });
      sessionStorage.setItem('jarvis:pendingChatHandoff', JSON.stringify({ sessionId: session.id, message: text }));
      navigate('chat');
    } catch (error) {
      toast(session ? 'Chat created, but setup did not finish. Your message is still here.' : 'Could not start chat. Your message is still here.', 'error');
    } finally { send.disabled = false; }
  };
  message.onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); } };
  load();
  const timer = setInterval(() => { if (!document.hidden) { refreshActivity(); lifespan(); } }, 30000);
  return () => { disposed = true; ++summaryVersion; clearInterval(timer); disposeBanner(); dialog?.close(); document.removeEventListener('kairos:appearance', appearance); };
}
