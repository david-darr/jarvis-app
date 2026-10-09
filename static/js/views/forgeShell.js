import { api, el, modelMark } from '../api.js';
import { subscribeAll } from '../chatStream.js';
import { navigateForge, sessionStatus, changeTotals, diffCounts, forgeRunning } from '../forgeUi.js';
import { mountForgeSession } from '../forgeSessionPane.js';
import { fileView, diffView, changeRole, revertFile } from '../forgeSurfaces.js';
import { mountArtifactPane, hideArtifact, setArtifactContext } from '../chatContent.js';

// Project layouts survive Home and Chats navigation, without retaining DOM or subscriptions.
const layouts = new Map();
let controller = null;
export async function open(options = {}) { return controller?.open(options); }
const readStorage = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const remember = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Optional device storage. */ } };
const phone = () => matchMedia('(max-width: 768px)').matches;
const sessionBase = id => `/api/forge/sessions/${encodeURIComponent(id)}`;

export async function render(container, tabId, options = {}) {
  container.classList.add('forge-shell-view');
  const shell = el('div', { class: 'forge-shell' });
  const sidebar = el('aside', { class: 'forge-project-sidebar', 'aria-label': 'Project sidebar' });
  const name = el('strong'), branch = el('span', { class: 'forge-branch' });
  const collapse = el('button', { class: 'forge-icon-button', text: '×', 'aria-label': 'Close project sidebar', onclick: () => showSidebar(false) });
  const newSession = el('button', { class: 'forge-icon-button', text: '+', 'aria-label': 'New session', onclick: () => navigateForge('forgeHome', { projectId: project.id, compose: true }) });
  const sidebarTabs = el('div', { class: 'forge-sidebar-tabs', role: 'tablist', 'aria-label': 'Project panels' });
  const search = el('input', { type: 'search', placeholder: 'Search sessions', 'aria-label': 'Search sessions' });
  const cards = el('div', { class: 'forge-session-cards' });
  const sessionPanel = el('section', { class: 'forge-sidebar-panel', role: 'tabpanel', id: 'forge-panel-sessions' }, [el('div', { class: 'forge-session-search' }, [search]), cards]);
  const tree = el('div', { class: 'forge-explorer-tree', role: 'tree', 'aria-label': 'Project files' });
  const treeRoot = el('div', { class: 'forge-tree-root' }, [el('span', { text: '\u25be', 'aria-hidden': 'true' }), el('strong')]);
  const explorerPanel = el('section', { class: 'forge-sidebar-panel', role: 'tabpanel', id: 'forge-panel-explorer' }, [
    el('div', { class: 'forge-sidebar-toolbar' }, [el('button', { class: 'forge-small-button', text: 'Collapse all', onclick: () => { layout.directories.clear(); tree.querySelectorAll('details').forEach(d => { d.open = false; }); } }),
      el('button', { class: 'forge-small-button', text: 'Refresh', 'aria-label': 'Refresh Explorer', onclick: () => loadExplorer(true) })]), treeRoot, tree,
  ]);
  const changesList = el('div', { class: 'forge-changes-list' });
  const changesPanel = el('section', { class: 'forge-sidebar-panel', role: 'tabpanel', id: 'forge-panel-changes' }, [
    el('div', { class: 'forge-sidebar-toolbar' }, [el('span', { text: 'Changes from session baseline' }), el('button', { class: 'forge-small-button', text: 'Refresh', 'aria-label': 'Refresh changes', onclick: () => refreshReviews() })]), changesList,
  ]);
  const status = el('p', { class: 'forge-shell-status', role: 'alert' });
  const resize = el('div', { class: 'forge-project-resizer', role: 'separator', tabindex: '0', 'aria-label': 'Resize project sidebar', 'aria-orientation': 'vertical' });
  sidebar.append(el('header', { class: 'forge-project-header' }, [el('div', { class: 'forge-project-heading' }, [name, branch]), newSession, collapse]), sidebarTabs, status, sessionPanel, explorerPanel, changesPanel, resize);
  const strip = el('div', { class: 'forge-surface-tabs', role: 'tablist', 'aria-label': 'Open sessions and files' });
  const reopen = el('button', { class: 'forge-small-button', text: 'Project', 'aria-label': 'Open project sidebar', onclick: () => showSidebar(true) });
  const splitButton = el('button', { class: 'forge-small-button', text: 'Split', title: 'Split right (Ctrl+D)', 'aria-label': 'Split right', onclick: () => split() });
  const panes = el('div', { class: 'forge-panes', id: 'chat-main' });
  const parked = el('div', { hidden: true, inert: true });
  const left = el('section', { class: 'forge-pane', 'aria-label': 'Left pane' });
  const right = el('section', { class: 'forge-pane', 'aria-label': 'Right pane' });
  const divider = el('div', { class: 'forge-pane-divider', role: 'separator', tabindex: '0', 'aria-label': 'Resize split', 'aria-orientation': 'vertical' });
  const workspace = el('main', { class: 'forge-workspace' }, [el('div', { class: 'forge-surface-bar' }, [reopen, strip, splitButton]), panes]);
  const artifactHost = el('div', { class: 'chat-layout forge-conversation' });
  artifactHost.append(panes); workspace.append(artifactHost);
  panes.append(left, divider, right); shell.append(sidebar, workspace, parked); container.replaceChildren(shell);
  let disposed = false, project, projects = [], models = [], sessions = [], layout, navigation = 0, reviewsVersion = 0, explorerVersion = 0;
  let explorerContext = null, activePane = 'left', tabDrag = null, cancelResize = null, reviewBusy = false;
  let width = Number(readStorage('kairos:forge-sidebar-width', 260)) || 260;
  const panels = new Map(), reviews = new Map();
  const tabButtons = new Map();
  const cleanupPanels = () => { for (const panel of panels.values()) { panel._cleanup?.(); panel.remove(); } panels.clear(); };
  const cleanup = () => {
    disposed = true; ++navigation; ++reviewsVersion; ++explorerVersion; cancelResize?.(); cleanupPanels(); unsubscribe(); clearInterval(timer); hideArtifact({ navigation: true });
    document.removeEventListener('keydown', onKey); window.removeEventListener('resize', viewportChanged);
    document.removeEventListener('kairos:forge-projects', projectsChanged); if (controller === instance) controller = null;
  };
  let ready;
  const instance = { open: async next => { await ready; if (!disposed) return openContext(next); } }; controller = instance;
  options.registerCleanup?.(cleanup);
  search.oninput = drawCards;
  tree.onclick = event => {
    const row = event.target.closest('.forge-tree-row');
    if (row) { tree.querySelector('.selected')?.classList.remove('selected'); row.classList.add('selected'); }
  };
  tree.onkeydown = event => {
    const row = event.target.closest('.forge-tree-row'); if (!row) return;
    const rows = [...tree.querySelectorAll('.forge-tree-row')].filter(node => node.getClientRects().length), index = rows.indexOf(row);
    let target;
    if (event.key === 'ArrowDown') target = rows[Math.min(index + 1, rows.length - 1)];
    else if (event.key === 'ArrowUp') target = rows[Math.max(0, index - 1)];
    else if (event.key === 'Home') target = rows[0];
    else if (event.key === 'End') target = rows.at(-1);
    else if (event.key === 'ArrowRight' && row.tagName === 'SUMMARY') { row.parentElement.open = true; target = row; }
    else if (event.key === 'ArrowLeft') {
      if (row.tagName === 'SUMMARY' && row.parentElement.open) { row.parentElement.open = false; target = row; }
      else target = row.parentElement.closest('[role=group]')?.parentElement.querySelector('summary');
    }
    if (target) { event.preventDefault(); target.focus(); }
  };
  for (const key of ['sessions', 'explorer', 'changes']) {
    const button = el('button', { class: 'forge-sidebar-tab', role: 'tab', id: `forge-sidebar-${key}`, 'aria-controls': `forge-panel-${key}`, 'data-sidebar-tab': key,
      onclick: () => selectSidebarTab(key) });
    sidebarTabs.append(button); tabButtons.set(key, button);
    button.onkeydown = event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const keys = [...tabButtons.keys()], index = keys.indexOf(key);
      const next = event.key === 'Home' ? keys[0] : event.key === 'End' ? keys.at(-1) : keys[(index + (event.key === 'ArrowRight' ? 1 : -1) + keys.length) % keys.length];
      selectSidebarTab(next); tabButtons.get(next).focus();
    };
    ({ sessions: sessionPanel, explorer: explorerPanel, changes: changesPanel })[key].setAttribute('aria-labelledby', button.id);
  }
  const unsubscribe = subscribeAll(id => {
    if (!layout || disposed) return;
    drawCards(); drawChanges();
    if (sessions.some(s => s.id === id) && !forgeRunning(id)) refreshReviews();
  });
  const timer = setInterval(() => { if (!document.hidden && project) refreshReviews(); }, 30000);
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', viewportChanged);
  document.addEventListener('kairos:forge-projects', projectsChanged);
  resize.ondblclick = () => setWidth(260, true);
  resize.onpointerdown = event => beginResize(event, resize, () => width, value => setWidth(value), (x, start, origin) => origin + x - start, () => remember('kairos:forge-sidebar-width', width));
  resize.onkeydown = event => {
    if (['ArrowLeft', 'ArrowRight', 'Home'].includes(event.key)) { event.preventDefault(); setWidth(event.key === 'Home' ? 260 : width + (event.key === 'ArrowRight' ? 20 : -20), true); }
  };
  divider.onpointerdown = event => beginResize(event, divider, () => layout.ratio, value => { layout.ratio = Math.max(.08, Math.min(.92, value)); drawPanes(); }, (x, start, origin) => origin + (x - start) / panes.clientWidth);
  divider.onkeydown = event => { if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); layout.ratio = Math.max(.08, Math.min(.92, layout.ratio + (event.key === 'ArrowRight' ? .03 : -.03))); drawPanes(); } };
  workspace.ondragover = event => {
    if (!tabDrag || phone()) return;
    const rect = workspace.getBoundingClientRect();
    if (event.clientX > rect.right - Math.min(120, rect.width * .2)) { event.preventDefault(); workspace.classList.add('forge-drop-right'); }
    else workspace.classList.remove('forge-drop-right');
  };
  workspace.ondragleave = event => { if (!workspace.contains(event.relatedTarget)) workspace.classList.remove('forge-drop-right'); };
  workspace.ondrop = event => { event.preventDefault(); if (workspace.classList.contains('forge-drop-right') && tabDrag) split(tabDrag); cancelDrag(); };
  strip.onwheel = event => { if (strip.scrollWidth > strip.clientWidth) { event.preventDefault(); strip.scrollLeft += event.deltaY || event.deltaX; } };
  try {
    ready = Promise.all([api('/api/forge/projects'), api('/api/models/choices').catch(() => [])]).then(values => { [projects, models] = values; });
    await ready;
    if (!disposed) await openContext(options);
  } catch (error) { if (!disposed) status.textContent = error.message; }
  return cleanup;

  async function openContext(next = {}) {
    const version = ++navigation;
    let session = null;
    try {
      if (next.sessionId) session = await api(sessionBase(next.sessionId));
      if (disposed || version !== navigation) return;
      const id = session?.forge.project_id || next.projectId || project?.id || projects[0]?.id;
      const chosen = projects.find(p => p.id === id);
      if (!chosen) { status.textContent = 'Open a project folder to begin.'; return; }
      const changedProject = project?.id !== id;
      if (changedProject) {
        cleanupPanels(); ++explorerVersion; ++reviewsVersion; reviewBusy = false; explorerContext = null; reviews.clear(); search.value = ''; sessions = [];
        project = chosen;
        if (!layouts.has(id)) layouts.set(id, { tabs: [], left: null, right: null, split: false, ratio: .5, sessionId: null, directories: new Set() });
        layout = layouts.get(id); activePane = 'left';
        if (phone()) { layout.left ||= layout.right; layout.right = null; layout.split = false; }
      }
      status.textContent = ''; name.textContent = project.name;
      document.dispatchEvent(new CustomEvent('kairos:forge-project-selected', { detail: { projectId: id } }));
      showSidebar(true);
      const loadedSessions = await api(`/api/forge/projects/${encodeURIComponent(id)}/sessions`);
      if (disposed || version !== navigation) return;
      sessions = loadedSessions;
      if (session) {
        layout.sessionId = session.id;
        let tab = layout.tabs.find(t => t.type === 'session' && t.sessionId === session.id);
        if (!tab) { tab = { id: `session:${session.id}`, type: 'session', title: session.title, sessionId: session.id, session }; layout.tabs.push(tab); }
        tab.session = session;
        activate(tab.id, 'left');
        if (next.initialMessage) { tab.initialMessage = next.initialMessage; const mounted = panels.get(tab.id); if (mounted) { mounted._cleanup?.(); mounted.remove(); panels.delete(tab.id); } }
      } else if (layout.sessionId && !sessions.some(s => s.id === layout.sessionId)) layout.sessionId = null;
      branch.textContent = sessions.find(s => s.id === layout.sessionId)?.forge.branch || project.git?.branch || 'Project folder';
      drawCards(); drawTabs(); drawPanes();
      selectSidebarTab(readStorage(`kairos:forge-panel:${id}`, 'sessions'));
      await refreshReviews();
      if (disposed || version !== navigation) return;
      setArtifactContext(layout.sessionId);
      await mountArtifactPane(artifactHost);
      if (session && phone() && next.closeSheet) showSidebar(false);
    } catch (error) { if (!disposed && version === navigation) status.textContent = error.message; }
  }
  function showSidebar(show) {
    sidebar.hidden = !show; reopen.hidden = show && !phone();
    if (phone()) {
      sidebar.setAttribute('role', 'dialog'); sidebar.setAttribute('aria-modal', 'true');
      workspace.inert = show; if (show) collapse.focus(); else reopen.focus();
    } else { sidebar.removeAttribute('role'); sidebar.removeAttribute('aria-modal'); workspace.inert = false; }
    setWidth(width);
  }
  function setWidth(value, persist = false) {
    if (phone()) return;
    width = Math.min(560, Math.floor(innerWidth / 2), Math.max(260, Math.round(value)));
    sidebar.style.width = `${width}px`; resize.setAttribute('aria-valuenow', String(width));
    resize.setAttribute('aria-valuemin', '260'); resize.setAttribute('aria-valuemax', String(Math.min(560, Math.floor(innerWidth / 2))));
    if (persist) remember('kairos:forge-sidebar-width', width);
  }
  function viewportChanged() {
    if (!layout) return;
    if (phone()) { layout.split = false; if (activePane === 'right') layout.left = layout.right; layout.right = null; activePane = 'left'; }
    showSidebar(!sidebar.hidden); drawTabs(); drawPanes();
  }
  function selectSidebarTab(key) {
    if (!layout) return;
    if (!tabButtons.has(key)) key = 'sessions';
    remember(`kairos:forge-panel:${project.id}`, key);
    for (const [name, button] of tabButtons) { const selected = name === key; button.setAttribute('aria-selected', String(selected)); button.tabIndex = selected ? 0 : -1; }
    sessionPanel.hidden = key !== 'sessions'; explorerPanel.hidden = key !== 'explorer'; changesPanel.hidden = key !== 'changes';
    if (key === 'explorer') loadExplorer();
  }
  function drawCards() {
    if (!layout) return;
    const query = search.value.toLowerCase();
    const sorted = sessions.filter(s => s.title.toLowerCase().includes(query)).sort((a, b) => sessionStatus(a).rank - sessionStatus(b).rank || b.updated_at - a.updated_at || a.id.localeCompare(b.id));
    cards.replaceChildren(...sorted.map(session => {
      const state = sessionStatus(session), model = models.find(m => m.id === session.model_endpoint_id);
      return el('button', { class: `forge-session-card status-${state.key}`, 'data-session-id': session.id, 'aria-pressed': String(layout.sessionId === session.id),
        onclick: () => openContext({ sessionId: session.id, closeSheet: true }) }, [
        el('span', { class: 'forge-card-top' }, [modelMark(model?.mark, model?.name), el('span', { class: 'forge-card-agent', text: `${model?.name || 'Agent'} · ${session.model_override || model?.model || 'Default'}` }),
          el('span', { class: 'forge-card-status', text: state.key === 'idle' ? relativeTime(session.updated_at) + ' · Idle' : state.label })]),
        el('strong', { class: 'forge-card-title', text: session.title }),
        el('span', { class: 'forge-card-bottom' }, [el('span', { class: 'forge-branch', text: session.forge.removed ? 'Worktree removed' : session.forge.branch || 'In place' }), diffCounts(changeTotals(reviews.get(session.id) || []))]),
      ]);
    }));
    if (!sorted.length) cards.append(el('p', { class: 'meta', text: sessions.length ? 'No matching sessions.' : 'Start a session with +.' }));
  }
  function relativeTime(at) {
    const minutes = Math.max(0, Math.floor((Date.now() / 1000 - at) / 60));
    return minutes < 1 ? 'now' : minutes < 60 ? `${minutes}m` : minutes < 1440 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / 1440)}d`;
  }
  async function refreshReviews() {
    if (!layout || disposed || reviewBusy) return;
    const version = ++reviewsVersion, id = project.id;
    reviewBusy = true;
    try {
      const results = await Promise.allSettled(sessions.map(async session => [session.id, session.forge.removed ? [] : (await api(`${sessionBase(session.id)}/changes`)).files]));
      if (disposed || version !== reviewsVersion || project.id !== id) return;
      for (const result of results) if (result.status === 'fulfilled') reviews.set(...result.value);
      const failed = results.find(r => r.status === 'rejected');
      status.textContent = failed ? `Changes could not load: ${failed.reason.message}` : '';
      drawCards(); drawChanges();
      for (const tab of layout.tabs.filter(t => t.type === 'diff')) {
        const panel = panels.get(tab.id), file = reviews.get(tab.sessionId)?.find(f => f.path === tab.path);
        if (panel) panel.replaceChildren(diffView(file || { path: tab.path, patch: '', hunks: [] }));
      }
      if (!explorerPanel.hidden) await loadExplorer(true);
      document.dispatchEvent(new Event('kairos:forge-review'));
    } finally { if (version === reviewsVersion) reviewBusy = false; }
  }
  function drawChanges() {
    if (!layout) return;
    const files = reviews.get(layout.sessionId) || [], totals = changeTotals(files);
    tabButtons.get('sessions').textContent = 'Sessions'; tabButtons.get('explorer').textContent = 'Explorer';
    const changesTab = tabButtons.get('changes');
    changesTab.replaceChildren(totals.added || totals.removed ? diffCounts(totals) : el('span', { text: 'Changes' }));
    changesTab.setAttribute('aria-label', `Changes${totals.added || totals.removed ? ` +${totals.added} -${totals.removed}` : ''}`);
    changesList.replaceChildren(...files.map(file => {
      const id = layout.sessionId;
      const revert = el('button', { class: 'forge-small-button forge-revert', text: 'Revert', 'aria-label': `Revert ${file.path}`, disabled: forgeRunning(id) || !!file.unavailable,
        onclick: async () => { revert.disabled = true; try { if (await revertFile(id, file)) await refreshReviews(); } catch (error) { status.textContent = error.message; } finally { if (revert.isConnected) revert.disabled = forgeRunning(id); } } });
      return el('div', { class: 'forge-change-row', 'data-change-path': file.path }, [el('button', { class: 'forge-change-open', onclick: () => openSurface('diff', file.path, true), title: file.path }, [
        el('span', { class: `forge-change-letter role-${changeRole(file)}`, text: changeRole(file), 'aria-label': ({ M: 'Modified', A: 'Added', D: 'Deleted', U: 'Untracked' })[changeRole(file)] }),
        el('span', { class: 'forge-change-path', text: file.path }), diffCounts(changeTotals([file])),
      ]), revert]);
    }));
    if (!files.length) changesList.append(el('p', { class: 'meta', text: layout.sessionId ? 'No changes from the session baseline.' : 'Select a session to review its changes.' }));
  }
  // A declaration, not a const: render() returns above, and only hoisted
  // functions below that return are ever initialised.
  function sourceBase() {
    return layout.sessionId && !sessions.find(s => s.id === layout.sessionId)?.forge.removed
      ? sessionBase(layout.sessionId) : `/api/forge/projects/${encodeURIComponent(project.id)}`;
  }
  async function loadExplorer(force = false) {
    const base = sourceBase();
    treeRoot.querySelector('strong').textContent = project.name;
    treeRoot.title = (base === sessionBase(layout.sessionId) && sessions.find(s => s.id === layout.sessionId)?.workspace_dir) || project.path;
    if (!force && explorerContext === base) return;
    explorerContext = base; const version = ++explorerVersion;
    tree.replaceChildren(el('p', { class: 'meta', text: 'Loading files...' }));
    await readDirectory('', tree, 0, base, version);
  }
  async function readDirectory(path, target, depth, base, version) {
    try {
      const result = await api(`${base}/files?${new URLSearchParams({ path })}`);
      if (disposed || version !== explorerVersion) return;
      target.replaceChildren(...result.entries.map(entry => {
        const changed = (reviews.get(layout.sessionId) || []).filter(f => f.path === entry.path || f.path.startsWith(entry.path + '/'));
        const role = changed.length ? (changed.every(f => ['A', 'U'].includes(changeRole(f))) ? 'A' : changed.every(f => changeRole(f) === 'D') ? 'D' : 'M') : entry.changed ? 'M' : '';
        const content = [el('span', { class: 'forge-tree-icon', text: entry.directory ? '▸' : '·', 'aria-hidden': 'true' }), el('span', { text: entry.name })];
        const attrs = { class: `forge-tree-row${role ? ` role-${role}` : ''}`, style: `padding-left:${8 + depth * 12}px`, title: entry.path };
        if (!entry.directory) return el('button', { ...attrs, role: 'treeitem', 'data-file-path': entry.path,
          onclick: () => openSurface('file', entry.path), ondblclick: () => openSurface('file', entry.path, true) }, content);
        const children = el('div', { role: 'group' });
        const details = el('details', { class: 'forge-tree-directory', open: layout.directories.has(entry.path), 'data-directory-path': entry.path }, [el('summary', { ...attrs, role: 'treeitem' }, content), children]);
        let loaded = false;
        const expand = () => {
          details.querySelector('summary').setAttribute('aria-expanded', String(details.open));
          if (details.open) { layout.directories.add(entry.path); if (!loaded) { loaded = true; readDirectory(entry.path, children, depth + 1, base, version); } }
          else layout.directories.delete(entry.path);
        };
        details.ontoggle = expand; expand(); return details;
      }));
      if (!result.entries.length) target.append(el('p', { class: 'meta', text: 'No files here.' }));
      if (result.truncated) target.append(el('p', { class: 'meta', text: 'Showing the first 500 entries.' }));
    } catch (error) { if (!disposed && version === explorerVersion) target.replaceChildren(el('p', { class: 'meta', text: error.message }), el('button', { class: 'forge-small-button', text: 'Retry', onclick: () => readDirectory(path, target, depth, base, version) })); }
  }
  function openSurface(type, path, pinned = false) {
    const base = sourceBase(), id = `${type}:${base}:${path}`;
    let tab = layout.tabs.find(t => t.id === id);
    if (!tab) {
      const preview = layout.tabs.find(t => t.preview && t.pane === activePane);
      if (preview) closeTab(preview.id);
      tab = { id, type, path, title: path.split('/').pop(), base, sessionId: layout.sessionId, preview: !pinned, pane: activePane };
      layout.tabs.push(tab);
    } else if (pinned) tab.preview = false;
    activate(id); if (phone()) showSidebar(false);
  }
  function activate(id, pane = null) {
    const tab = layout.tabs.find(t => t.id === id); if (!tab) return;
    pane ||= layout.split && tab.pane === 'right' ? 'right' : activePane;
    if (!layout.split || phone()) pane = 'left';
    if (pane === 'right' && layout.left === id) layout.left = layout.tabs.find(t => t.id !== id && t.pane !== 'right')?.id || null;
    if (pane === 'left' && layout.right === id) layout.right = null;
    tab.pane = pane; layout[pane] = id; activePane = pane;
    if (tab.sessionId && tab.sessionId !== layout.sessionId) { layout.sessionId = tab.sessionId; explorerContext = null; branch.textContent = sessions.find(s => s.id === tab.sessionId)?.forge.branch || project.git?.branch || ''; drawCards(); drawChanges(); if (!explorerPanel.hidden) loadExplorer(); }
    setArtifactContext(layout.sessionId);
    drawTabs(); drawPanes();
  }
  function closeTab(id) {
    const at = layout.tabs.findIndex(t => t.id === id); if (at < 0) return;
    const tab = layout.tabs[at]; panels.get(id)?._cleanup?.(); panels.get(id)?.remove(); panels.delete(id); layout.tabs.splice(at, 1);
    for (const side of ['left', 'right']) if (layout[side] === id) layout[side] = layout.tabs.findLast(t => t.pane === side)?.id || null;
    if (tab.pane === 'right' && !layout.right) { layout.split = false; activePane = 'left'; }
    drawTabs(); drawPanes();
  }
  function drawTabs() {
    if (!layout) return;
    strip.replaceChildren(...layout.tabs.map((tab, index) => {
      const selected = layout[activePane] === tab.id;
      const button = el('button', { class: 'forge-surface-tab', role: 'tab', 'aria-selected': String(selected), tabindex: selected ? '0' : '-1', text: tab.type === 'diff' ? `${tab.title} · Diff` : tab.title,
        title: tab.path || tab.title, onclick: () => activate(tab.id), ondblclick: () => { tab.preview = false; drawTabs(); } });
      button.onkeydown = event => {
        if (event.key === 'Delete') { event.preventDefault(); closeTab(tab.id); }
        if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
          event.preventDefault(); const next = event.key === 'Home' ? 0 : event.key === 'End' ? layout.tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + layout.tabs.length) % layout.tabs.length;
          activate(layout.tabs[next].id); strip.querySelector('[aria-selected=true]')?.focus();
        }
      };
      const row = el('div', { class: `forge-surface-tab-group${tab.preview ? ' preview' : ''}${layout.left === tab.id || layout.right === tab.id ? ' visible' : ''}`, 'data-surface-id': tab.id, 'data-surface-type': tab.type, 'data-pane': tab.pane || 'left', draggable: !phone() }, [button,
        el('button', { class: 'forge-tab-close', text: '×', 'aria-label': `Close ${tab.title}`, onclick: () => closeTab(tab.id) })]);
      row.onmousedown = event => { if (event.button === 1) event.preventDefault(); };
      row.onauxclick = event => { if (event.button === 1) { event.preventDefault(); closeTab(tab.id); } };
      row.ondragstart = event => { tabDrag = tab.id; event.dataTransfer.setData('text/plain', tab.id); event.dataTransfer.effectAllowed = 'move'; row.classList.add('dragging'); };
      row.ondragend = cancelDrag; return row;
    }));
    strip.querySelector('[aria-selected=true]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  function drawPanes() {
    if (!layout) return;
    const isSplit = layout.split && !phone(); shell.dataset.split = String(isSplit); right.hidden = divider.hidden = !isSplit; splitButton.hidden = phone();
    left.style.flex = isSplit ? `${layout.ratio} 1 0` : '1 1 0'; right.style.flex = `${1 - layout.ratio} 1 0`;
    for (const [side, host] of [['left', left], ['right', right]]) {
      const tab = layout.tabs.find(t => t.id === layout[side]);
      host.onpointerdown = () => { if (activePane !== side) { activePane = side; drawTabs(); } };
      const panel = tab ? ensurePanel(tab) : el('div', { class: 'forge-pane-empty', text: side === 'right' ? 'Open a file or move a tab here.' : 'Select a session, or explore the project files.' });
      if (host.firstElementChild !== panel) {
        for (const child of [...host.children]) if (child.classList.contains('forge-surface')) parked.append(child);
        host.replaceChildren(panel);
      }
    }
  }
  function ensurePanel(tab) {
    if (panels.has(tab.id)) return panels.get(tab.id);
    const panel = el('div', { class: `forge-surface forge-${tab.type}-surface`, role: 'tabpanel', 'aria-label': tab.title }); panels.set(tab.id, panel);
    panel.textContent = 'Loading...';
    // Mount only after drawPanes attaches the surface. Async work is owned by this panel.
    queueMicrotask(async () => {
      try {
        if (disposed || panels.get(tab.id) !== panel) return;
        if (tab.type === 'session') {
          const session = await api(sessionBase(tab.sessionId));
          if (disposed || panels.get(tab.id) !== panel) return;
          const initialMessage = tab.initialMessage; delete tab.initialMessage;
          await mountForgeSession(panel, session, project, models, { initialMessage, onReview: refreshReviews,
            onDiff: path => { layout.sessionId = tab.sessionId; activePane = tab.pane || 'left'; openSurface('diff', path, true); },
            onChanges: () => { layout.sessionId = tab.sessionId; showSidebar(true); selectSidebarTab('changes'); refreshReviews(); },
            onLeave: () => { closeTab(tab.id); selectSidebarTab('sessions'); openContext({ projectId: project.id }); } });
        } else if (tab.type === 'file') {
          const file = await api(`${tab.base}/file?${new URLSearchParams({ path: tab.path })}`);
          if (!disposed && panels.get(tab.id) === panel) panel.replaceChildren(fileView(file));
        } else panel.replaceChildren(diffView(reviews.get(tab.sessionId)?.find(f => f.path === tab.path) || { path: tab.path, patch: '', hunks: [] }));
      } catch (error) { if (!disposed && panels.get(tab.id) === panel) panel.textContent = `Cannot open this tab: ${error.message}`; }
    });
    return panel;
  }
  function split(id = null) {
    if (phone() || !layout) return;
    if (!layout.split) layout.ratio = .5;
    layout.split = true;
    if (!id) {
      const selected = layout.tabs.find(t => t.id === layout[activePane]);
      id = selected?.type === 'session' ? layout.tabs.findLast(t => t.type !== 'session')?.id : selected?.id;
    }
    const tab = layout.tabs.find(t => t.id === id);
    if (tab) { tab.preview = false; const other = layout.tabs.findLast(t => t.id !== id && t.type === 'session') || layout.tabs.findLast(t => t.id !== id); if (other) { layout.left = other.id; other.pane = 'left'; } else layout.left = null; activate(id, 'right'); }
    else { activePane = 'right'; drawPanes(); }
  }
  function cancelDrag() { tabDrag = null; workspace.classList.remove('forge-drop-right'); strip.querySelectorAll('.dragging').forEach(row => row.classList.remove('dragging')); }
  function beginResize(event, handle, get, set, calculate, save = () => {}) {
    if (event.button !== 0 || phone()) return;
    event.preventDefault(); cancelResize?.();
    const origin = get(), start = event.clientX; let frame = null, pending = origin;
    handle.setPointerCapture(event.pointerId); handle.classList.add('dragging');
    const move = event => { pending = calculate(event.clientX, start, origin); if (!frame) frame = requestAnimationFrame(() => { frame = null; set(pending); }); };
    const finish = (cancel = false) => {
      if (frame) cancelAnimationFrame(frame); frame = null; set(cancel ? origin : pending); if (!cancel) save();
      handle.classList.remove('dragging'); handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); handle.removeEventListener('pointercancel', lost);
      if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId); cancelResize = null;
    };
    const up = () => finish(), lost = () => finish(true); cancelResize = () => finish(true);
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up); handle.addEventListener('pointercancel', lost);
  }
  function onKey(event) {
    if (event.defaultPrevented || document.querySelector('.panel-dialog')) return;
    if (event.key === 'Escape') { if (cancelResize || tabDrag) { event.preventDefault(); cancelResize?.(); cancelDrag(); } else if (phone() && !sidebar.hidden) showSidebar(false); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'd' && !event.target.closest?.('input, textarea, [contenteditable=true]')) { event.preventDefault(); split(); }
    if (phone() && !sidebar.hidden && event.key === 'Tab') {
      const controls = [...sidebar.querySelectorAll('button, input, summary, [tabindex]')].filter(n => n.tabIndex >= 0 && !n.disabled && n.getClientRects().length);
      const at = controls.indexOf(document.activeElement);
      if (event.shiftKey ? at <= 0 : at < 0 || at === controls.length - 1) { event.preventDefault(); (event.shiftKey ? controls.at(-1) : controls[0])?.focus(); }
    }
  }
  async function projectsChanged(event) {
    if (event.detail?.removed === project?.id) { cleanupPanels(); layouts.delete(project.id); navigateForge('forgeHome'); return; }
    try { projects = await api('/api/forge/projects'); } catch { /* Rail reports loading errors. */ }
  }
}
