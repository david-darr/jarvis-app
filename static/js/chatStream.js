// Persistent (module-level, survives tab switches) registry of in-flight
// chat turns. David's ask 2026-09-12: leaving a chat mid-reply to check
// another tab, then coming back, showed nothing until the turn finished —
// root cause was that the fetch+stream-reading loop lived entirely inside
// views/chat.js's per-render closure, writing into DOM nodes app.js's
// switchTab() had already wiped. The request itself was never actually
// cancelled (fetch doesn't auto-abort just because its caller's DOM is
// gone) — it just had nowhere left to write. Moving the network/streaming
// logic here, one level above any single view, means any view (or the
// floating overlay in floatingProgress.js) can attach to a session's
// ongoing turn and see the same live text, regardless of which view
// actually sent the message.

const _inflight = new Map(); // sessionId -> { text, status, sessionTitle, listeners: Set<fn> }
const _globalListeners = new Set(); // fn(sessionId, entry|null)

// How long a finished entry stays visible/attachable after completing —
// long enough for a floating card (or a chat view reattaching right as it
// finishes) to show the done/failed state before it's gone, short enough
// not to look stuck (David's ask: "~4 seconds then fade").
const FINISHED_RETENTION_MS = 4000;

function _notify(sessionId) {
  const entry = _inflight.get(sessionId) || null;
  if (entry) for (const cb of entry.listeners) cb(entry);
  for (const cb of _globalListeners) cb(sessionId, entry);
}

// Stops a running turn. Safe to call when nothing is running, so callers
// (barge-in especially) never have to check first.
export function stopTurn(sessionId) {
  const entry = _inflight.get(sessionId);
  if (!entry || entry.status !== 'processing') return false;
  entry.stopped = true;
  try { entry.controller.abort(); } catch { /* already finished */ }
  return true;
}

export function getInFlight(sessionId) {
  return _inflight.get(sessionId) || null;
}

export function listInFlight() {
  return [..._inflight.entries()].map(([sessionId, entry]) => ({ sessionId, ...entry }));
}

// Returns an unsubscribe function. Silently a no-op to subscribe to a
// session with no in-flight entry (nothing to attach to) — callers should
// check getInFlight() first if they need to know whether it's worth it.
export function subscribe(sessionId, callback) {
  const entry = _inflight.get(sessionId);
  if (!entry) return () => {};
  entry.listeners.add(callback);
  return () => entry.listeners.delete(callback);
}

export function subscribeAll(callback) {
  _globalListeners.add(callback);
  return () => _globalListeners.delete(callback);
}

// Synchronous: registers the in-flight entry immediately (so a caller can
// subscribe() right after this returns and not race the first chunk),
// then runs the actual request in the background.
export function startTurn(sessionId, sessionTitle, text, attachmentIds) {
  if (_inflight.get(sessionId)?.status === 'processing') throw new Error('This chat already has a response in progress');
  // The controller lives on the entry rather than in a closure so stopTurn()
  // can reach it from anywhere - the composer's stop button, a tab switch, or
  // Open Mic barge-in - without the caller having to hold a reference.
  const controller = new AbortController();
  const entry = { text: "", status: "processing", connected: false, sessionTitle, error: null, controller, listeners: new Set() };
  _inflight.set(sessionId, entry);
  _notify(sessionId);
  _runTurn(sessionId, entry, text, attachmentIds);
  return entry;
}

async function _runTurn(sessionId, entry, text, attachmentIds) {
  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text, attachment_ids: attachmentIds }),
      signal: entry.controller.signal,
    });
    if (!res.ok || !res.body) {
      let detail = `Stream failed (${res.status})`;
      try { detail = (await res.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }

    entry.connected = true;
    _notify(sessionId);
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let completed = false;
    while (true) {
      const { done, value } = await reader.read();
      buffer += done ? decoder.decode() + '\n\n' : decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n\r?\n/);
      buffer = lines.pop();
      for (const line of lines) {
        const data = line.split(/\r?\n/).filter(row => row.startsWith('data:')).map(row => row.slice(5).trimStart()).join('\n');
        if (!data) continue;
        let payload;
        try { payload = JSON.parse(data); } catch (_) { throw new Error('Invalid response stream'); }
        if (payload.error) throw new Error(payload.error);
        if (payload.done === true) completed = true;
        if (payload.chunk) {
          entry.text += payload.chunk;
          _notify(sessionId);
        }
      }
      if (done) break;
    }
    if (!completed) throw new Error('Connection ended before the response finished. Reopen this chat to recover any saved reply.');
    entry.status = "done";
  } catch (e) {
    // A deliberate stop is not a failure. Aborting the fetch drops the
    // connection, which the backend already treats as client cancellation and
    // handles by saving the partial reply with status "interrupted" (see
    // services/chat_service.py's BaseException branch) - so the text already
    // on screen is the text that was kept, and there is nothing to apologise
    // for in the UI.
    if (entry.stopped || e.name === 'AbortError') {
      entry.status = "stopped";
    } else {
      entry.status = "failed";
      entry.error = e.message;
    }
  }
  _notify(sessionId);

  setTimeout(() => {
    if (_inflight.get(sessionId) === entry) {
      _inflight.delete(sessionId);
      _notify(sessionId);
    }
  }, FINISHED_RETENTION_MS);
}
