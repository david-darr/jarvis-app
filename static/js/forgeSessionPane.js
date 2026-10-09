import { api, el, customSelect, confirmDialog, openPanelDialog } from './api.js';
import { mountForgeTranscript } from './forgeTranscript.js';
import { forgeRunning } from './forgeUi.js';
import { subscribeAll } from './chatStream.js';
import { configureApp, launchApp, permissionAction } from './forgeAppPreview.js';

export async function mountForgeSession(panel, session, project, models, { initialMessage, onReview, onLeave, onDiff, onChanges, onPreview, onTerminal } = {}) {
  const id = session.id, base = `/api/forge/sessions/${encodeURIComponent(id)}`;
  let disposed = false, mutating = false, dialog = null, chatCleanup = () => {};
  const appController = new AbortController();
  let appDialogCleanup = () => {}, appStarting = false, appStatus = null;
  const status = el('p', { class: 'forge-review-status', role: 'alert' });
  const agentText = (endpoint, override) => [endpoint?.name || 'Default agent', override].filter(Boolean).join(' · ');
  const agentLabel = el('span', { class: 'meta', text: agentText(models.find(m => m.id === session.model_endpoint_id), session.model_override) });
  const mode = customSelect({}, ['build', 'plan'].map(value => el('option', { value, text: value === 'build' ? 'Build' : 'Plan' })));
  mode.value = session.forge.mode;
  mode.querySelector('button').setAttribute('aria-label', 'Session mode');
  const explanation = el('p', { class: 'meta forge-plan-note' });
  const explain = () => { explanation.textContent = session.forge.mode === 'plan' ? 'Plan keeps your agent read only. You can still edit files and use the terminal.' : 'Build lets your agent make changes in this workspace.'; };
  const end = el('button', { class: 'btn quiet', text: 'End session', onclick: endSession });
  const sync = () => { mode.disabled = end.disabled = forgeRunning(id) || mutating || !!session.forge.removed; };
  const unsubscribe = subscribeAll(changed => { if (changed === id) sync(); });
  mode.addEventListener('change', async () => {
    if (forgeRunning(id) || mutating) { mode.value = session.forge.mode; return; }
    mutating = true; sync();
    try { session = await api(`${base}/mode`, { method: 'POST', body: JSON.stringify({ mode: mode.value }) }); explain(); }
    catch (error) { mode.value = session.forge.mode; status.textContent = error.message; }
    finally { mutating = false; sync(); }
  });
  const run = el('button', { class: 'btn quiet forge-run-app', text: 'Run app', disabled: !!session.forge.removed, onclick: async () => {
    if (appStarting || disposed) return;
    if (appStatus?.ready) { onPreview?.(id); return; }
    appDialogCleanup = configureApp(session, project, panel, async () => {
      appStarting = true; run.disabled = true; status.textContent = 'Waiting for app approval and readiness...';
      try { appStatus = await launchApp(id, 'start', { signal: appController.signal, owner: panel }); if (!disposed) { status.textContent = ''; onPreview?.(id); } }
      catch (error) { if (!disposed) status.textContent = error.message; }
      finally { appStarting = false; refreshApp(); }
    });
  } });
  async function refreshApp() {
    try { const result = await api(`${base}/app/status`); if (!disposed) { appStatus = result; run.textContent = result.ready ? 'Preview' : 'Run app'; run.disabled = appStarting || !!session.forge.removed; } }
    catch (error) { if (!disposed) status.textContent = error.message; }
  }
  const appTimer = setInterval(refreshApp, 3000);
  document.addEventListener('kairos:forge-apps', refreshApp); refreshApp();
  const terminal = el('button', { class: 'btn quiet forge-open-terminal', text: 'Terminal', disabled: !!session.forge.removed, onclick: () => onTerminal?.(id) });
  const header = el('header', { class: 'forge-session-header' }, [el('strong', { text: project?.name || 'Forge project' }), agentLabel, run, terminal, end]);
  const host = el('div', { class: 'forge-chat-host' });
  // filter(Boolean): replaceChildren would print a false condition as the text "false".
  panel.replaceChildren(...[header, explanation, session.forge.isolation === 'in_place' && el('p', { class: 'forge-warning', text: 'In place: changes affect your project folder directly.' }), status, host].filter(Boolean));
  explain(); sync();
  // Teardown is installed before awaiting the chat mount.
  const cleanup = () => { disposed = true; appController.abort(); appDialogCleanup(); clearInterval(appTimer); document.removeEventListener('kairos:forge-apps', refreshApp); unsubscribe(); chatCleanup(); dialog?.close(); };
  panel._cleanup = cleanup;
  try {
    const mounted = await mountForgeTranscript(host, { sessionId: id, title: session.title, modelPicker: !session.forge.removed,
      workspace: session.workspace_dir, branch: session.forge.branch, modeControl: mode, modeBusy: () => mutating,
      hasMessages: !!session.messages?.length,
      onDiff, onChanges,
      queue: true, readOnly: !!session.forge.removed, initialMessage, placeholder: 'Message your agent',
      emptyText: 'Describe the next step for this project.', onTurnEnd: onReview,
      onModelChange: (model, override) => { agentLabel.textContent = agentText(model, override); },
    });
    if (disposed) mounted(); else chatCleanup = mounted;
  } catch (error) { if (!disposed) status.textContent = error.message; }
  return cleanup;

  async function endSession(event) {
    const opener = event.currentTarget;
    if (forgeRunning(id) || mutating) return;
    let state;
    try { state = await api(`${base}/git`); }
    catch (error) { status.textContent = `Could not check the worktree: ${error.message}`; return; }
    if (disposed) return;
    const body = el('div', { class: 'forge-form' }, [
      el('p', { text: 'Choose what to do with your session. Your conversation is kept.' }),
      (state.files.length || state.unmerged) && el('p', { class: 'forge-warning', text: 'This session has uncommitted or unmerged changes.' }),
      session.forge.isolation === 'in_place' && el('p', { class: 'meta', text: 'Your main project folder and its changes are kept.' }),
    ].filter(Boolean));
    const footer = el('div', { class: 'forge-end-options' });
    async function finish(option) {
      if (option === 'discard' && (state.files.length || state.unmerged) && !await confirmDialog({
        title: 'Discard this session?', message: `Discard uncommitted changes and remove this worktree and branch ${state.branch}? Your main project folder and its current branch are kept.`, confirmLabel: 'Discard' })) return;
      if (disposed || forgeRunning(id) || mutating) return;
      mutating = true; sync(); footer.querySelectorAll('button').forEach(b => { b.disabled = true; });
      try {
        const request = { option, confirmed: option === 'discard' };
        if (option === 'merge') await permissionAction(`${base}/end`, { body: request, signal: appController.signal, owner: panel });
        else await api(`${base}/end`, { method: 'POST', body: JSON.stringify(request) });
        if (disposed) return;
        document.dispatchEvent(new Event('kairos:forge-apps')); dialog.close(); onLeave?.();
      } catch (error) { body.append(el('p', { role: 'alert', text: error.message.replace(/^\d+: /, '') })); }
      finally { mutating = false; sync(); footer.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
    }
    for (const [option, text] of [['merge', 'Merge back and remove'], ['keep-branch', 'Keep the branch, remove the worktree'], ['discard', 'Discard'], ['leave', 'Keep and leave']]) {
      footer.append(el('button', { class: option === 'discard' ? 'btn danger' : 'btn', text, onclick: () => finish(option) }));
    }
    dialog = openPanelDialog({ title: 'End session', body, footer, owner: panel, opener, group: 'forge' });
  }
}
