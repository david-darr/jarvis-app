import { api, el } from './api.js';
import * as chatStream from './chatStream.js';

export const navigateForge = (tab, options = {}) => document.dispatchEvent(new CustomEvent('jarvis:navigate', { detail: { tab, ...options } }));
export const forgeRunning = id => chatStream.getInFlight(id)?.status === 'processing';

export function sessionStatus(session) {
  const flight = chatStream.getInFlight(session.id);
  if (flight?.permission || session.needs_approval) return { key: 'approval', label: 'Needs approval', rank: 0 };
  if (forgeRunning(session.id)) return { key: 'working', label: 'Working...', rank: 1 };
  if (flight?.status === 'done' || session.message_count > 0 || session.messages?.length) return { key: 'done', label: 'Done', rank: 2 };
  return { key: 'idle', label: 'Idle', rank: 3 };
}

export const changeTotals = files => files.reduce((counts, file) => ({ added: counts.added + (file.added || 0), removed: counts.removed + (file.removed || 0) }), { added: 0, removed: 0 });
export function diffCounts({ added = 0, removed = 0 }) {
  return el('span', { class: 'forge-counts' }, [el('span', { class: 'forge-added', text: `+${added.toLocaleString()}` }), el('span', { class: 'forge-removed', text: `-${removed.toLocaleString()}` })]);
}

export async function projectSessions(projects) {
  const results = await Promise.allSettled(projects.map(p => api(`/api/forge/projects/${encodeURIComponent(p.id)}/sessions`)));
  return results.map((result, i) => ({ project: projects[i], sessions: result.status === 'fulfilled' ? result.value : [], error: result.status === 'rejected' }));
}

export function sessionRow(session, project) {
  const running = forgeRunning(session.id);
  return el('button', { type: 'button', class: 'dashboard-row forge-session-row', 'data-session-id': session.id,
    onclick: () => navigateForge('forgeSession', { sessionId: session.id }) }, [
    el('span', { class: `status-dot ${running ? 'ok' : ''}`, 'aria-label': running ? 'Running' : 'Idle' }),
    el('span', { class: 'dashboard-row-copy' }, [el('strong', { text: session.title }),
      el('small', { class: 'meta', text: `${project.name} · ${session.forge.branch || 'In place'} · ${session.forge.mode === 'plan' ? 'Plan' : 'Build'}${session.forge.removed ? ' · Worktree removed' : ''}` })]),
    running && el('small', { class: 'meta', text: 'Running' }),
  ]);
}
