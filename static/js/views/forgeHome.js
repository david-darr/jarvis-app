import { api, el, customSelect, openPanelDialog, toast } from '../api.js';
import { mountDither } from '../dither.js';
import { halftoneSource, halftoneFocus, halftonePalette, halftoneVersion } from '../appearance.js';
import { bars, donut, heatmap, statCard, rankedTable } from '../forgeCharts.js';
import { projectSessions, sessionRow, forgeRunning } from '../forgeUi.js';
import { openProjectForm } from '../forgeProjectForms.js';
import { subscribeAll } from '../chatStream.js';
import { permissionAction } from '../forgeAppPreview.js';

const navigate = (tab, options = {}) => document.dispatchEvent(new CustomEvent('jarvis:navigate', { detail: { tab, ...options } }));
const widgetNames = { working: 'Working now', services: 'Running services', activity: 'Git activity', recent: 'Recent projects', lifespan: 'Repo lifespan' };
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
  const send = el('button', { type: 'submit', class: 'btn primary', text: 'Start session', disabled: true });
  const locationNote = el('p', { class: 'meta forge-location-note', role: 'status' });
  const firstCommitNote = el('p', { class: 'meta', role: 'status' });
  const firstCommitButton = el('button', { type: 'button', class: 'btn', text: 'Make the first commit' });
  const recovery = el('div', { class: 'forge-first-commit forge-warning', hidden: true }, [firstCommitNote, firstCommitButton]);
  const composer = el('form', { class: 'forge-composer' }, [message, pickers, locationNote, recovery, el('div', { class: 'forge-composer-bottom' }, [send])]);
  const hero = el('section', { class: 'forge-hero' }, [banner, el('div', { class: 'forge-hero-copy' }, [
    el('div', { class: 'eyebrow' }, ['Forge ', el('small', { class: 'forge-preview', text: 'Preview' })]),
    el('h1', { text: 'What should your agents work on?' }), composer,
    el('p', { class: 'meta', text: 'Build in an isolated worktree, or plan before changing files.' }),
  ])]);
  const grid = el('div', { class: 'forge-widget-grid' });
  const customize = el('button', { type: 'button', class: 'btn quiet', text: 'Customize' });
  page.append(hero, el('div', { class: 'forge-toolbar' }, [el('span', { class: 'meta', text: 'Your work, in view' }), customize]), grid);
  container.append(page);
  let disposed = false, dialog = null, projectSelect = null, modelSelect = null, lifespanSelect = null, projects = [], busy = false;
  // The exact model for the chosen agent (e.g. Sonnet 5, gpt-6.1-sol), from the
  // same catalog as Chat's model picker; hidden for agents without one.
  let models = [], variantSelect = null, variantVersion = 0;
  const variantField = el('label', { class: 'forge-picker-field', hidden: true });
  let isolationSelect, modeSelect, branchSelect, branchField, branchVersion = 0, starting = false, branchesReady = false, sessionGroups = [];
  let pendingStart = null;
  const approvalController = new AbortController();
  const drawWorking = () => {
    if (disposed) return;
    const running = sessionGroups.flatMap(({ project, sessions }) => sessions.filter(s => !s.forge.removed && forgeRunning(s.id)).map(s => sessionRow(s, project)));
    bodies.working.replaceChildren(...(running.length ? running : [el('p', { class: 'meta', text: 'No Forge sessions running.' })]));
  };
  const unsubscribe = subscribeAll(drawWorking);
  const updateSend = () => {
    send.disabled = starting || !projectSelect?.value || !modelSelect?.value || (isolationSelect?.value === 'existing_branch' && (!branchesReady || !branchSelect?.value));
    firstCommitButton.disabled = starting; message.readOnly = starting; pickers.inert = starting;
  };
  const clearRecovery = () => { if (!starting) { pendingStart = null; recovery.hidden = true; } };
  composer.addEventListener('input', clearRecovery);
  composer.addEventListener('change', clearRecovery, true);
  const field = (label, picker) => el('label', { class: 'forge-picker-field' }, [el('span', { class: 'meta', text: label }), picker]);
  async function locationChanged() {
    const version = ++branchVersion;
    const isolation = isolationSelect.value;
    branchField.hidden = isolation !== 'existing_branch';
    locationNote.classList.toggle('forge-warning', isolation === 'in_place');
    locationNote.textContent = isolation === 'in_place' ? 'In place changes your project folder directly. There is no isolated worktree.' : isolation === 'existing_branch' ? 'Use an available branch in a separate worktree.' : 'A new branch and worktree keep your project folder untouched.';
    branchesReady = false; updateSend();
    if (isolation !== 'existing_branch') return;
    branchField.replaceChildren(el('span', { class: 'meta', text: 'Branch' }), el('span', { class: 'meta', text: 'Loading branches...' }));
    try {
      const branches = await api(`/api/forge/projects/${encodeURIComponent(projectSelect.value)}/branches`);
      if (disposed || version !== branchVersion) return;
      branchSelect = customSelect({}, [el('option', { value: '', text: 'Choose an available branch' }), ...branches.map(b => el('option', { value: b.name, text: b.worktree ? `${b.name} (already checked out)` : b.name, disabled: !!b.worktree }))]);
      branchSelect.querySelector('button').setAttribute('aria-label', 'Branch for new session');
      branchField.replaceChildren(el('span', { class: 'meta', text: 'Branch' }), branchSelect);
      branchesReady = true; branchSelect.addEventListener('change', updateSend); updateSend();
    } catch { if (!disposed && version === branchVersion) branchField.textContent = 'Branches could not load. Choose a different location to retry.'; }
  }
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
      const [result, groups, apps] = await Promise.all([api('/api/forge/activity'), projectSessions(projects), api('/api/forge/apps/running')]);
      if (disposed) return;
      sessionGroups = groups; drawWorking();
      bodies.services.replaceChildren(...(apps.length ? apps.map(app => {
        const group = groups.find(g => g.project.id === app.project_id), session = group?.sessions.find(s => s.id === app.session_id);
        return el('button', { class: 'dashboard-row forge-running-service', 'data-app-session': app.session_id,
          onclick: () => navigate('forgeShell', { sessionId: app.session_id, previewSessionId: app.session_id }) }, [
          el('span', { class: 'status-dot ok' }), el('span', { class: 'dashboard-row-copy' }, [
            el('strong', { text: `:${app.port} ${session?.title || 'Session'}` }), el('small', { class: 'meta', text: app.command })])]);
      }) : [el('p', { class: 'meta', text: 'No apps running.' })]));
      if (groups.some(g => g.error)) bodies.working.append(el('p', { class: 'meta', text: 'Some project sessions could not load.' }));
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
      models = results[1].status === 'fulfilled' ? results[1].value : [];
      projectSelect = projectPicker(projects, 'Project for new session');
      modelSelect = customSelect({}, models.map(m => el('option', { value: m.id, text: `${m.name} · ${m.model || 'CLI default'}` })));
      modelSelect.querySelector('button').setAttribute('aria-label', 'Agent for new session');
      projectSelect.disabled = !projects.length; modelSelect.disabled = !models.length;
      isolationSelect = customSelect({}, [el('option', { value: 'new_worktree', text: 'New worktree' }), el('option', { value: 'existing_branch', text: 'Existing branch' }), el('option', { value: 'in_place', text: 'In place' })]);
      isolationSelect.querySelector('button').setAttribute('aria-label', 'Where it runs');
      modeSelect = customSelect({}, [el('option', { value: 'build', text: 'Build' }), el('option', { value: 'plan', text: 'Plan' })]);
      modeSelect.querySelector('button').setAttribute('aria-label', 'Mode for new session');
      branchField = el('label', { class: 'forge-picker-field', hidden: true });
      pickers.append(field('Project', projectSelect), field('Where it runs', isolationSelect), field('Agent', modelSelect), variantField, field('Mode', modeSelect), branchField);
      projectSelect.addEventListener('change', locationChanged); isolationSelect.addEventListener('change', locationChanged);
      modelSelect.addEventListener('change', () => { updateSend(); loadVariants(); });
      loadVariants();
      if (options.projectId && projects.some(p => p.id === options.projectId)) projectSelect.value = options.projectId;
      locationChanged();
      if (!projects.length || !models.length) pickers.append(el('button', { type: 'button', class: 'btn quiet', text: !projects.length ? 'Add a project' : 'Add a model', onclick: event => !projects.length ? openProjectForm('existing', { owner: page, opener: event.currentTarget, onSaved: () => { document.dispatchEvent(new Event('kairos:forge-projects')); navigate('forgeHome'); } }) : navigate('settings', { section: 'add-models' }) }));
      lifespanSelect = projectPicker(projects, 'Repository for lifespan');
      lifespanSelect.disabled = !projects.length;
      if (options.projectId && projects.some(p => p.id === options.projectId)) projectSelect.value = lifespanSelect.value = options.projectId;
      panels.lifespan.querySelector('header').append(lifespanSelect);
      lifespanSelect.addEventListener('change', lifespan);
      bodies.recent.replaceChildren(...(projects.length ? projects.slice(0, 5).map(p => el('button', { type: 'button', class: 'dashboard-row', onclick: async () => {
        try { await api(`/api/forge/projects/${p.id}/opened`, { method: 'POST' }); if (!disposed) { projectSelect.value = lifespanSelect.value = p.id; locationChanged(); lifespan(); message.focus(); } } catch { /* api displays the error. */ }
      } }, [el('span', { class: 'dashboard-row-copy' }, [el('strong', { text: p.name }), el('small', { class: 'meta', text: p.path })])])) : [el('p', { class: 'meta', text: 'Add an existing folder, clone a repository, or start a new project.' }), el('button', { type: 'button', class: 'btn', text: 'Open folder', onclick: event => openProjectForm('existing', { owner: page, opener: event.currentTarget, onSaved: () => { document.dispatchEvent(new Event('kairos:forge-projects')); navigate('forgeHome'); } }) })]));
      lifespan(); refreshActivity();
      if (options.compose) message.focus();
    } catch { if (!disposed) grid.replaceChildren(el('p', { role: 'status', text: 'Forge could not load. Open Home again to retry.' })); }
  }
  async function startSession(payload) {
    try {
      const session = await api('/api/forge/sessions', { method: 'POST', toast: false, body: JSON.stringify(payload) });
      if (!disposed) navigate('forgeSession', { sessionId: session.id, initialMessage: payload.task });
    } catch (error) {
      if (disposed) return;
      // The route's dedicated NoCommits error is HTTP 409 with this stable copy.
      if (/^409: [\s\S]* has no commits yet\. Make a first commit to start a session\.$/.test(error.message)) {
        pendingStart = payload; firstCommitNote.textContent = error.message.replace(/^409: /, ''); recovery.hidden = false;
      } else toast(error.message.replace(/^\d+: /, ''), 'error');
    }
  }
  firstCommitButton.onclick = async () => {
    if (starting || !pendingStart) return;
    const payload = pendingStart;
    starting = true; updateSend();
    try {
      await permissionAction(`/api/forge/projects/${encodeURIComponent(payload.project_id)}/first-commit`, { signal: approvalController.signal, owner: page });
      if (disposed) return;
      pendingStart = null; recovery.hidden = true;
      await startSession(payload);
    } catch (error) { if (!disposed) toast(error.message.replace(/^\d+: /, ''), 'error'); }
    finally { starting = false; updateSend(); }
  };
  composer.onsubmit = async event => {
    event.preventDefault();
    if (send.disabled || !message.value.trim()) return;
    const project = projects.find(p => p.id === projectSelect.value), text = message.value.trim(), model = modelSelect.value;
    if (!project || !model) return;
    clearRecovery();
    const payload = { project_id: project.id, task: text, model_endpoint_id: model, model_override: variantSelect?.value || null, mode: modeSelect.value, isolation: isolationSelect.value, branch: isolationSelect.value === 'existing_branch' ? branchSelect.value : null };
    starting = true; updateSend();
    try {
      await startSession(payload);
    } finally { starting = false; updateSend(); }
  };
  async function loadVariants() {
    const version = ++variantVersion;
    const endpoint = models.find(m => m.id === modelSelect?.value);
    variantSelect = null; variantField.hidden = true; variantField.replaceChildren();
    if (!endpoint || !['claude_cli', 'codex_cli', 'api'].includes(endpoint.kind)) return;
    let choices = [];
    try { choices = await api(`/api/models/${encodeURIComponent(endpoint.id)}/catalog`); } catch { /* Default model only. */ }
    if (disposed || version !== variantVersion) return;
    variantSelect = customSelect({}, [el('option', { value: '', text: 'Default model' }),
      ...choices.map(m => el('option', { value: m.id, text: m.display_name || m.id }))]);
    variantSelect.querySelector('button').setAttribute('aria-label', 'Model for new session');
    variantField.replaceChildren(el('span', { class: 'meta', text: 'Model' }), variantSelect);
    variantField.hidden = false;
  }
  message.onkeydown = event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); } };
  load();
  document.addEventListener('kairos:forge-apps', refreshActivity);
  const timer = setInterval(() => { if (!document.hidden) { refreshActivity(); lifespan(); } }, 30000);
  return () => { disposed = true; approvalController.abort(); ++summaryVersion; ++branchVersion; unsubscribe(); clearInterval(timer); disposeBanner(); dialog?.close(); document.removeEventListener('kairos:appearance', appearance); document.removeEventListener('kairos:forge-apps', refreshActivity); };
}
