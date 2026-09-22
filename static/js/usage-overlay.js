const providersNode = document.getElementById('providers');
const recordedNode = document.getElementById('recorded');
const messageNode = document.getElementById('message');
let current = null;

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}
function countdown(timestamp) {
  if (!timestamp) return 'Reset time unavailable';
  const seconds = Math.max(0, Math.ceil(timestamp - Date.now() / 1000));
  if (!seconds) return 'Reset due now';
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.ceil((seconds % 3600) / 60);
  return `Resets in ${days ? `${days}d ` : ''}${hours ? `${hours}h ` : ''}${minutes}m`;
}
function render(data) {
  current = data;
  providersNode.replaceChildren(); recordedNode.replaceChildren();
  messageNode.textContent = !data.providers.length && !data.recorded.length ? 'Connect a model in JARVIS to see usage.' : '';
  for (const provider of data.providers) {
    const box = node('section', 'provider');
    const title = node('div', 'provider-title');
    title.append(node('span', '', provider.provider === 'claude' ? 'Claude Code' : 'Codex'));
    if (provider.stale) title.append(node('span', 'status', provider.windows.length ? 'Stale' : (provider.status || 'Unavailable')));
    box.append(title);
    if (!provider.windows.length) box.append(node('div', 'reset', 'Account quota unavailable. Check CLI sign-in.'));
    for (const window of provider.windows) {
      const item = node('div', 'window');
      const head = node('div', 'window-head');
      head.append(node('span', '', window.name), node('strong', '', `${window.used_percent}% used`));
      const track = node('div', 'track');
      const fill = node('div', 'fill');
      fill.style.width = `${window.used_percent}%`;
      track.append(fill);
      item.append(head, track, node('div', 'reset', countdown(window.resets_at)));
      box.append(item);
    }
    providersNode.append(box);
  }
  if (data.recorded.length) {
    recordedNode.append(node('h2', '', 'JARVIS-recorded tokens · lifetime'));
    for (const entry of data.recorded) {
      const row = node('div', 'recorded-row');
      row.append(node('span', '', entry.name), node('span', '', entry.total_tokens == null ? 'No usage reported' : entry.total_tokens.toLocaleString()));
      recordedNode.append(row);
    }
  }
}
async function refresh() {
  try {
    const response = await fetch('/api/models/quotas', { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 401 || response.status === 403) {
      current = null; providersNode.replaceChildren(); recordedNode.replaceChildren();
      messageNode.textContent = 'Open JARVIS and sign in to view usage.';
      return;
    }
    if (!response.ok) throw new Error('unavailable');
    render(await response.json());
  } catch {
    if (current) render({ ...current, providers: current.providers.map(p => ({ ...p, stale: true })) });
    else messageNode.textContent = 'Usage is unavailable. JARVIS may be offline.';
  }
}
document.getElementById('hide').onclick = () => window.usageOverlay?.hide();
document.getElementById('open-app').onclick = () => window.usageOverlay?.openApp();
document.getElementById('collapse').onclick = (event) => {
  const collapsed = document.body.classList.toggle('collapsed');
  event.currentTarget.textContent = collapsed ? '+' : '−';
  event.currentTarget.setAttribute('aria-expanded', String(!collapsed));
  window.usageOverlay?.collapse(collapsed);
};
refresh();
setInterval(refresh, 60_000);
setInterval(() => { if (current) render(current); }, 30_000);
