// One observation stream per mounted company. Reconnect never dispatches work.
export function subscribe(systemId, after, { onChange, onStatus }) {
  let cursor = after;
  let closed = false;
  const source = new EventSource(`/api/swarm/systems/${encodeURIComponent(systemId)}/events?after=${cursor}`);
  source.onopen = () => { if (!closed) onStatus("Live updates connected"); };
  source.onerror = () => { if (!closed) onStatus("Updates disconnected; reconnecting…"); };
  source.addEventListener("unavailable", () => {
    if (closed) return;
    source.close();
    onStatus("Updates unavailable. Refresh to reconnect.");
  });
  source.addEventListener("change", (event) => {
    if (closed) return;
    const id = Number(event.lastEventId);
    if (!Number.isSafeInteger(id) || id <= cursor) return;
    try {
      const value = JSON.parse(event.data);
      if (value.id !== id || value.system_id !== systemId) return;
      cursor = id;
      onChange(value);
    } catch (_) { onStatus("An update could not be read. Refresh to reconnect."); }
  });
  return () => { closed = true; source.close(); };
}
