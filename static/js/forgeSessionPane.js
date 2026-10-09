import { api, el, customSelect, confirmDialog, openPanelDialog } from './api.js';
import { mountForgeTranscript } from './forgeTranscript.js';
import { forgeRunning } from './forgeUi.js';
import { subscribeAll } from './chatStream.js';
import { configureApp, launchApp } from './forgeAppPreview.js';

export async function mountForgeSession(panel, session, project, models, { initialMessage, onReview, onLeave, onDiff, onChanges, onPreview } = {}) {
  const id = session.id, base = `/api/forge/sessions/${encodeURIComponent(id)}`;
  let disposed = false, mutating = false, dialog = null, chatCleanup = () => {};
  const appController = new AbortController();
  let appDialogCleanup = () => {}, appStarting = false, appStatus = null;
  const status = el('p', { class: 'forge-review-status', role: 'alert' });
  const agentLabel = el('span', { class: 'meta', text: models.find(m => m.id === session.model_endpoint_id)?.name || 'Default agent' });
  const mode = customSelect({}, ['build', 'plan'].map(value => el('option', { value, text: value === 'build' ? 'Build' : 'Plan' })));
  mode.value = session.forge.mode;
  mode.querySelector('button').setAttribute('aria-label', 'Session mode');
  const explanation = el('p', { class: 'meta forge-plan-note' });
  const explain = () => { explanation.textContent = session.forge.mode === 'plan' ? 'Plan is read only. File edits and shell commands are blocked.' : 'Build lets your agent make changes in this workspace.'; };
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
  const header = el('header', { class: 'forge-session-header' }, [el('strong', { text: project?.name || 'Forge project' }), agentLabel, run, end]);
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
      onModelChange: model => { agentLabel.textContent = model?.name || 'Default agent'; },
    });
    if (disposed) mounted(); else chatCleanup = mounted;
  } catch (error) { if (!disposed) status.textContent = error.message; }
  return cleanup;

  async function endSession(event) {
    const opener = event.currentTarget;
    if (forgeRunning(id) || mutating) return;
    let dirty = false;
    try { dirty = (await api(`${base}/changes`)).files.length > 0; }
    catch (error) { status.textContent = `Could not check the worktree: ${error.message}`; return; }
    if (disposed) return;
    const inPlace = session.forge.isolation === 'in_place';
    const body = el('div', { class: 'forge-form' }, [el('p', { text: inPlace ? 'Keep your project folder and leave this session.' : 'Keep this worktree for later, or remove it. Your branch and conversation are kept.' }),
      dirty && el('p', { class: 'forge-warning', text: inPlace ? 'Your project has changes. Leaving keeps them.' : 'This worktree has changes from the session baseline. Removing it discards any uncommitted changes.' })]);
    const keep = el('button', { class: 'btn primary', text: 'Keep and leave', onclick: async () => {
      keep.disabled = true;
      try { await api(`${base}/end`, { method: 'POST' }); document.dispatchEvent(new Event('kairos:forge-apps')); dialog.close(); onLeave(); }
      catch (error) { status.textContent = error.message; keep.disabled = false; }
    } });
    const footer = el('div', {}, [keep]);
    if (!inPlace) footer.append(el('button', { class: 'btn danger', text: 'Remove worktree', onclick: async () => {
      if (dirty && !await confirmDialog({ title: 'Discard worktree changes?', message: 'Permanently discard all uncommitted changes in this worktree and remove it? Your branch and conversation will remain.', confirmLabel: 'Discard changes and remove' })) return;
      if (disposed || forgeRunning(id) || mutating) return;
      mutating = true; sync();
      try {
        await api(`${base}${dirty ? '?discard=true&confirmed=true' : ''}`, { method: 'DELETE' });
        dialog.close(); onLeave();
      } catch (error) {
        // The baseline diff can be clean even if a later commit has local edits.
        // DELETE's authoritative dirty check also handles edits made after opening.
        if (/^409:.*uncommitted changes/i.test(error.message)) {
          dirty = true;
          body.append(el('p', { class: 'forge-warning', role: 'alert', text: 'This worktree has uncommitted changes. Choose Remove worktree again to explicitly confirm discarding them.' }));
        } else body.append(el('p', { role: 'alert', text: error.message }));
      }
      finally { mutating = false; sync(); }
    } }));
    dialog = openPanelDialog({ title: 'End session', body, footer, owner: panel, opener, group: 'forge' });
  }
}
