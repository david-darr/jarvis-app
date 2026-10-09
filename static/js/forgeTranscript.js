import { api, el, confirmDialog } from './api.js';
import { mountSessionChat } from './sessionChat.js';
import { renderMessageBody } from './chatContent.js';
import { clearPermission, getInFlight, subscribeAll } from './chatStream.js';
import { diffView } from './forgeSurfaces.js';
import { changeTotals, diffCounts } from './forgeUi.js';
import { mountChatBackdrop } from './chatBackdrop.js';

// Re-created for Kairos from the layout brief. No Monocode source is copied.
const duration = seconds => {
  const n = Math.max(1, Math.round(seconds || 0));
  return n < 60 ? `${n}s` : `${Math.floor(n / 60)}m${n % 60 ? ` ${n % 60}s` : ''}`;
};
const category = name => /edit|write|patch|file_change/i.test(name || '') ? 'edit'
  : /shell|bash|exec|command|terminal/i.test(name || '') ? 'command'
  : /read|file|search|grep|glob/i.test(name || '') ? 'read' : 'tool';
const keptCheckpoints = new Set(); // Keep survives pane/tab remounts, like the queue.
const checkpointFor = (state, rows) => rows.find(c => c.created >= state.startedAt && c.created <= state.endedAt && c.finished && c.finished <= state.endedAt);
const checkpointChanges = checkpoint => (checkpoint?.roots || []).flatMap(root => root.changes || []);
const undoable = checkpoint => checkpoint?.status === 'changed' && !checkpoint.overlap && checkpointChanges(checkpoint).every(c => !c.restored_at && (!c.restore_state || c.restore_state === 'available'));
function target(detail) {
  try {
    const args = JSON.parse(detail);
    for (const key of ['file_path', 'path', 'command', 'cmd', 'query', 'pattern', 'url']) if (typeof args?.[key] === 'string') return args[key];
    if (Array.isArray(args?.changes)) return args.changes.map(c => c.path).filter(Boolean).join(', ');
  } catch {
    // A clipped Edit input can retain its complete path but not valid JSON.
    for (const key of ['file_path', 'path', 'command', 'cmd', 'query', 'pattern', 'url']) {
      const match = (detail || '').match(new RegExp(`"${key}"\\s*:\\s*("(?:\\\\.|[^"\\\\])*")`));
      if (match) { try { return JSON.parse(match[1]); } catch { /* Keep looking. */ } }
    }
  }
  return detail || '';
}

export async function mountForgeTranscript(host, options) {
  const transcript = createTranscript(options);
  try { return await mountSessionChat(host, { ...options, transcript }); }
  catch (error) { transcript.dispose(); throw error; }
}

function createTranscript(options) {
  const id = options.sessionId, base = `/api/forge/sessions/${encodeURIComponent(id)}`;
  const messages = el('div', { class: 'forge-transcript', role: 'log', 'aria-label': 'Session transcript', tabindex: '0' });
  const documentBody = el('div', { class: 'forge-transcript-document' });
  // Controller writes to this inner document; the scroller stays stable.
  const states = new Map();
  let disposed = false, following = true, heldUntil = 0, refreshVersion = 0;
  let files = [], checkpoints = [], refreshing = false;
  let modelStatus, modeControl, sendControl, inputControl;
  let backdrop;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const jump = el('button', { class: 'forge-jump btn', text: 'Jump to latest', hidden: true, onclick: () => {
    following = true; heldUntil = 0; follow();
  } });
  messages.append(documentBody);
  const release = () => { following = false; heldUntil = performance.now() + 150; jump.hidden = false; };
  messages.addEventListener('wheel', event => { if (event.deltaY < 0) release(); }, { passive: true });
  let touchY;
  messages.addEventListener('touchstart', event => { touchY = event.touches[0]?.clientY; }, { passive: true });
  messages.addEventListener('touchmove', event => { if (event.touches[0]?.clientY > touchY) release(); touchY = event.touches[0]?.clientY; }, { passive: true });
  messages.addEventListener('keydown', event => { if (['ArrowUp', 'PageUp', 'Home'].includes(event.key)) release(); });
  messages.addEventListener('scroll', () => {
    const near = messages.scrollHeight - messages.scrollTop - messages.clientHeight <= 16;
    following = near && performance.now() >= heldUntil; jump.hidden = following;
  });
  function follow() {
    if (!disposed && following && performance.now() >= heldUntil) messages.scrollTop = messages.scrollHeight;
    jump.hidden = following;
  }
  const observer = new ResizeObserver(follow); observer.observe(documentBody);
  const spinnerFrames = ['\u2801', '\u2803', '\u2807', '\u2847', '\u28c7', '\u28e7', '\u28f7', '\u28ff', '\u28fe', '\u28fc'];
  const ticker = setInterval(() => {
    for (const state of states.values()) if (state.busy) labelWork(state);
  }, 80);
  const unsubscribe = subscribeAll(changed => { if (changed === id) setBusy(getInFlight(id)?.status === 'processing'); });

  function mount(root, controls) {
    const { input, inputTop, send, queueButton, queueList, tools, status } = controls;
    modelStatus = status; sendControl = send; inputControl = input; modeControl = options.modeControl;
    input.addEventListener('input', () => setBusy(getInFlight(id)?.status === 'processing'));
    input.className = 'forge-transcript-input'; input.placeholder = 'Ask your agent to build or plan';
    send.classList.add('forge-send'); queueButton.classList.add('forge-queue-button');
    const path = (options.workspace || '').replace(/\\/g, '/').replace(/^[a-z]:\/Users\/[^/]+/i, '~');
    const context = el('div', { class: 'forge-composer-context', title: options.workspace }, [
      el('span', { text: path }), el('span', { text: options.branch || 'In place' }),
    ]);
    const chips = el('div', { class: 'forge-composer-chips' }, [tools, ...(modeControl ? [modeControl] : [])]);
    const composer = el('div', { class: 'forge-transcript-composer' }, [context, inputTop,
      el('div', { class: 'forge-transcript-footer' }, [chips, queueButton, send])]);
    root.replaceChildren(el('div', { class: 'forge-transcript-layout' }, [
      el('div', { class: 'forge-transcript-scroll' }, [messages, jump]), queueList, composer,
    ]));
    backdrop = mountChatBackdrop(root.firstElementChild);
    backdrop.show(options.hasMessages ? 'sky' : 'figure');
  }
  function card(role, text, message) {
    backdrop?.show('sky', { animate: !options.hasMessages });
    if (role === 'user') {
      const content = el('div', { class: 'forge-prompt-text', text });
      const more = el('button', { class: 'forge-small-button', text: 'Show more', hidden: true, 'aria-expanded': 'false', onclick: () => {
        const expanded = content.classList.toggle('expanded'); more.textContent = expanded ? 'Show less' : 'Show more'; more.setAttribute('aria-expanded', String(expanded)); follow();
      } });
      const prompt = el('section', { class: 'forge-prompt', 'aria-label': 'Your prompt' }, [content, more]);
      const clampObserver = new ResizeObserver(() => { more.hidden = !content.classList.contains('expanded') && content.scrollHeight <= content.clientHeight + 1; });
      clampObserver.observe(content); prompt._observer = clampObserver;
      return prompt;
    }
    const prose = el('div', { class: 'forge-prose' }); renderMessageBody(prose, text, id, true);
    const glyph = el('span', { class: 'forge-terminal-spinner', 'aria-hidden': 'true' });
    const label = el('span');
    const summary = el('summary', { class: 'forge-work-label' }, [glyph, label]);
    const steps = el('div', { class: 'forge-work-spine' });
    const fold = el('details', { class: 'forge-work', hidden: true }, [summary, steps]);
    const review = el('div', { class: 'forge-turn-review', hidden: true });
    const error = el('p', { class: 'forge-transcript-error', role: 'status', hidden: true });
    const turn = el('section', { class: 'forge-turn', 'aria-label': 'Agent response' }, [prose, fold, error, review]);
    const state = { prose, turn, fold, label, glyph, steps, review, error, events: [], startedAt: message?.ts, runId: message?.run_id };
    states.set(prose, state); return turn;
  }
  function setBusy(busy) {
    if (modeControl) modeControl.disabled = busy || !!options.readOnly || !!options.modeBusy?.();
    if (sendControl && !busy) sendControl.disabled = !!options.readOnly || !inputControl.value.trim();
    for (const state of states.values()) state.review.querySelectorAll('button').forEach(b => { if (b.dataset.undo) b.disabled = busy || state.reviewBusy || !!options.readOnly || !undoable(state.checkpoint); });
  }
  function labelWork(state) {
    const counts = { read: 0, edit: 0, command: 0 };
    state.events.filter(e => e.phase === 'started').forEach(e => { const kind = category(e.name); if (kind in counts) counts[kind]++; });
    const description = [counts.read && `read ${counts.read} file${counts.read === 1 ? '' : 's'}`, counts.edit && `edited ${counts.edit}`, counts.command && `ran ${counts.command} command${counts.command === 1 ? '' : 's'}`].filter(Boolean).join(', ');
    const clockAt = state.permissionStartedAt || state.endedAt || Date.now() / 1000;
    state.label.textContent = `${state.busy ? 'Working' : 'Worked'} for ${duration(clockAt - state.startedAt - (state.pausedSeconds || 0))}${!state.busy && description ? ` · ${description}` : ''}`;
    state.glyph.textContent = state.busy ? spinnerFrames[reduced.matches || state.permission ? 0 : Math.floor(performance.now() / 80) % spinnerFrames.length] : '✓';
  }
  function fileFor(summary) {
    const normalized = (summary || '').replace(/\\/g, '/');
    return files.filter(f => normalized === f.path || normalized.endsWith('/' + f.path) || normalized.split(', ').some(p => p === f.path || p.endsWith('/' + f.path)));
  }
  function diffCard(file) {
    const full = diffView(file);
    const lines = [...full.querySelectorAll('.forge-diff-line')].filter(row => !row.classList.contains('diff-hunk') &&
      (row.querySelector('.forge-diff-number')?.textContent.trim() || row.querySelector('.forge-diff-number:nth-child(2)')?.textContent.trim())).slice(0, 6);
    for (const line of lines) {
      const [oldNumber, newNumber] = line.querySelectorAll('.forge-diff-number');
      oldNumber.textContent = newNumber.textContent || oldNumber.textContent; newNumber.remove();
    }
    return el('button', { class: 'forge-inline-diff', type: 'button', 'data-diff-path': file.path, 'aria-label': `Open diff of ${file.path}`, onclick: () => options.onDiff?.(file.path) }, [
      el('span', { class: 'forge-inline-diff-head' }, [el('span', { text: file.path }), diffCounts(changeTotals([file]))]),
      el('span', { class: 'forge-inline-diff-lines' }, lines.length ? lines : [el('span', { text: file.binary ? 'Binary file' : 'Review full diff' })]),
    ]);
  }
  function drawSteps(state) {
    const paired = [];
    for (const event of state.events) {
      if (event.phase === 'started') paired.push({ ...event });
      else {
        const own = paired.find(e => e.id === event.id);
        if (own) Object.assign(own, event); else paired.push({ ...event });
      }
    }
    state.steps.replaceChildren(...paired.map(step => {
      const kind = category(step.name);
      const row = el('div', { class: `forge-tool-step forge-tool-${kind}${step.ok === false ? ' is-error' : ''}`, 'data-tool-id': step.id }, [
        el('div', { class: 'forge-tool-heading' }, [el('span', { text: kind === 'command' ? '$' : step.name || 'Tool' }), el('code', { text: step.summary || '' }),
          step.ok === false && el('span', { text: 'Failed' })].filter(Boolean)),
      ]);
      if (kind === 'command' && step.output) row.append(el('pre', { class: 'forge-command-output', text: step.output }));
      else if (step.output && (step.ok === false || kind === 'tool')) row.append(el('details', {}, [el('summary', { text: 'Details' }), el('pre', { class: 'forge-command-output', text: step.output })]));
      if (kind === 'edit' && step.ok === true) row.append(...fileFor(step.summary).map(diffCard));
      return row;
    }));
    if (state.permission) {
      // Requests can arrive before TOOL_STARTED (provider gating). Prefer the
      // latest matching pending step; otherwise keep a named placeholder.
      const request = state.permission;
      const matches = paired.filter(step => step.phase !== 'finished' && (step.name === request.tool || step.name?.endsWith('__' + request.tool)));
      const parent = state.steps.querySelector(`[data-tool-id="${CSS.escape(String(matches.at(-1)?.id || ''))}"]`) || state.steps;
      const status = el('span', { role: 'alert' });
      const prompt = el('div', { class: 'forge-inline-permission', role: 'group', 'aria-label': 'Permission needed' }, [
        el('span', { text: request.title || request.tool }), request.target && el('code', { text: request.target }), status,
      ].filter(Boolean));
      for (const choice of request.choices || []) prompt.append(el('button', { class: `btn${choice.behavior === 'deny' ? ' danger' : ' primary'}`, text: choice.label, 'data-choice': choice.id, onclick: async () => {
        prompt.querySelectorAll('button').forEach(b => { b.disabled = true; });
        try {
          await api(`/api/permissions/${encodeURIComponent(request.id)}/answer`, { method: 'POST', body: JSON.stringify({ choice: choice.id }) });
          state.permission = null; clearPermission(id, request.id); drawSteps(state);
        } catch (error) { status.textContent = error.message; prompt.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
      } }));
      parent.append(prompt); state.fold.open = true;
    }
    follow();
  }
  function paint(body, entry) {
    const state = states.get(body); if (!state || disposed) return;
    const wasBusy = state.busy;
    state.busy = entry.status === 'processing'; state.startedAt = entry.startedAt; state.endedAt = entry.endedAt;
    state.permissionStartedAt = entry.permissionStartedAt; state.pausedSeconds = entry.pausedSeconds;
    state.runId = entry.runId; state.events = entry.toolSteps || []; state.permission = state.busy ? entry.permission : null;
    state.turn.setAttribute('aria-busy', String(state.busy)); state.fold.hidden = false; labelWork(state);
    if (state.lastEvents !== state.events.length || state.lastPermission !== state.permission?.id) {
      state.lastEvents = state.events.length; state.lastPermission = state.permission?.id; drawSteps(state);
    }
    if (!state.busy) {
      state.error.hidden = !entry.error && entry.status !== 'stopped'; state.error.textContent = entry.error || 'Stopped';
      if (wasBusy) { state.fold.open = false; refresh(); }
    } else if (state.events.some(e => category(e.name) === 'edit' && e.phase === 'finished') && !refreshing && state.diffEvents !== state.events.length) {
      state.diffEvents = state.events.length; refresh();
    }
    follow();
  }
  async function refresh() {
    if (disposed) return;
    const version = ++refreshVersion; refreshing = true;
    try {
      const results = await Promise.all([options.readOnly ? { files: [] } : api(`${base}/changes`), api(`${base}/checkpoints`)]);
      if (disposed || version !== refreshVersion) return;
      files = results[0].files; checkpoints = results[1];
      // The existing admin checkpoint detail has this turn's diff and restore
      // eligibility. Session Changes is cumulative, so it cannot supply accurate
      // per-turn totals or detect later edits to the same file.
      const relevant = new Set([...states.values()].map(state => checkpointFor(state, checkpoints)).filter(Boolean));
      await Promise.all([...relevant].map(async checkpoint => {
        const detail = await api(`/api/file-checkpoints/${encodeURIComponent(checkpoint.id)}`);
        if (disposed || version !== refreshVersion) return;
        const rootPaths = new Set(checkpoint.roots.map(root => root.path));
        checkpoint.roots = detail.roots.filter(root => rootPaths.has(root.path));
      }));
      if (disposed || version !== refreshVersion) return;
      for (const state of states.values()) { drawSteps(state); drawReview(state); }
    } catch (error) { if (!disposed) modelStatus.textContent = `Review could not load: ${error.message}`; }
    finally { if (version === refreshVersion) refreshing = false; }
  }
  function drawReview(state) {
    // Checkpoints start inside a run and end before its outcome is recorded.
    // Match by that interval, never by "latest", so Undo targets this turn.
    const checkpoint = checkpointFor(state, checkpoints);
    state.checkpoint = checkpoint;
    const changes = checkpointChanges(checkpoint);
    const paths = [...new Set(changes.map(c => c.path))];
    const counts = changes.reduce((total, change) => {
      if (typeof change.added === 'number' && typeof change.removed === 'number') {
        total.added += change.added; total.removed += change.removed; return total;
      }
      let inHunk = false;
      for (const line of (change.diff || '').split('\n')) {
        if (line.startsWith('@@ ')) inHunk = true;
        if (inHunk && line.startsWith('+')) total.added++;
        if (inHunk && line.startsWith('-')) total.removed++;
      }
      return total;
    }, { added: 0, removed: 0 });
    state.review.hidden = state.busy || keptCheckpoints.has(checkpoint?.id) || !paths.length || changes.every(c => c.restored_at);
    if (state.review.hidden) return;
    state.review.replaceChildren(el('span', { text: `${paths.length} file${paths.length === 1 ? '' : 's'}` }), diffCounts(counts),
      el('button', { class: 'forge-small-button', text: 'Undo', 'data-undo': checkpoint.id, disabled: !!options.readOnly || getInFlight(id)?.status === 'processing' || !undoable(checkpoint), onclick: async () => {
        if (state.reviewBusy || options.readOnly || getInFlight(id)?.status === 'processing') return;
        if (!await confirmDialog({ title: 'Undo this turn?', message: `Restore the ${paths.length} changed file${paths.length === 1 ? '' : 's'} to before this turn? Later edits will be checked before restoring.`, confirmLabel: 'Undo turn' })) return;
        if (disposed || getInFlight(id)?.status === 'processing') return;
        state.reviewBusy = true; state.review.querySelectorAll('button').forEach(button => { button.disabled = true; });
        try {
          await api(`${base}/checkpoints/${encodeURIComponent(checkpoint.id)}/undo`, { method: 'POST', body: JSON.stringify({ confirmed: true }) });
          await refresh(); await options.onTurnEnd?.();
        } catch (error) { modelStatus.textContent = error.message; }
        finally { state.reviewBusy = false; if (!disposed) drawReview(state); }
      } }),
      el('button', { class: 'forge-small-button', text: 'Keep', onclick: () => { keptCheckpoints.add(checkpoint.id); state.review.hidden = true; } }),
      el('button', { class: 'forge-small-button', text: 'Review', onclick: () => options.onChanges?.() }));
  }
  async function history(session) {
    try {
      const runs = (await api(`/api/runs?${new URLSearchParams({ session_id: id, limit: '500' })}`)).filter(r => r.surface === 'chat').sort((a, b) => a.started_at - b.started_at);
      // Replies carry run_id. Older replies use the preceding user message's
      // time interval and chronological order; no-model turns get no run.
      const users = session.messages.filter(m => m.role === 'user');
      const claimed = new Set();
      const replies = [...states.values()];
      const mappings = [];
      for (let i = 0; i < replies.length; i++) {
        const state = replies[i];
        const prompt = [...users].reverse().find(m => m.ts <= state.startedAt);
        const next = users.find(m => m.ts > (prompt?.ts ?? Infinity));
        const run = runs.find(r => state.runId ? r.id === state.runId : !claimed.has(r.id) && prompt && r.started_at >= prompt.ts && r.started_at <= (state.startedAt ?? Infinity) && (!next || r.started_at < next.ts));
        if (run) { claimed.add(run.id); mappings.push([state, run]); }
      }
      await Promise.all(mappings.map(async ([state, run]) => {
        const saved = await api(`/api/runs/${encodeURIComponent(run.id)}`); if (disposed) return;
        state.runId = run.id; state.startedAt = run.started_at; state.endedAt = run.ended_at;
        // Older timelines do not store call ids. Pair finishes with the first
        // pending call of the same name, preserving the stored step order.
        const pending = [];
        state.events = (saved.steps || []).filter(s => ['tool_started', 'tool_finished'].includes(s.kind)).map((step, index) => {
          if (step.kind === 'tool_started') {
            const event = { ...step, id: `saved-${index}`, phase: 'started', summary: target(step.detail), output: '' }; pending.push(event); return event;
          }
          const own = pending.find(e => !e.finished && e.name === step.name); if (own) own.finished = true;
          return { ...step, id: own?.id || `saved-${index}`, phase: 'finished', summary: own?.summary || '', output: step.detail };
        });
        state.fold.hidden = !state.events.length; labelWork(state); drawSteps(state);
      }));
      await refresh();
    } catch (error) { if (!disposed) modelStatus.textContent = `Tool history could not load: ${error.message}`; }
  }
  const reviewChanged = () => { if (!refreshing) refresh(); };
  document.addEventListener('kairos:forge-review', reviewChanged);
  return {
    messages: documentBody, mount, card, paint, history, follow, setBusy,
    dispose() {
      backdrop?.dispose();
      disposed = true; ++refreshVersion; clearInterval(ticker); observer.disconnect(); unsubscribe();
      documentBody.querySelectorAll('.forge-prompt').forEach(node => node._observer?.disconnect());
      document.removeEventListener('kairos:forge-review', reviewChanged); states.clear();
    },
  };
}
