/* JARVIS desktop usage overlay - the notch's behaviour.

   Ported from CodeNotch's Windows notch (windows/codenotch/ui/notch.html at
   117a38b, https://github.com/vinzdg/codenotch), MIT licensed, (c) 2026 Vinz;
   the full notice is in static/css/usage-overlay.css. The rendering (rings,
   card, tail, fold, handles) follows CodeNotch's code closely. What changed:
   Tauri's invoke/listen became JARVIS's own API plus a small Electron bridge
   (window.usageOverlay, electron/usage-overlay-preload.js); CodeNotch's other
   providers, translations and drag-to-another-edge were left out; and the
   hot-rectangle reporting became one signal - is the pointer over the notch -
   which electron/main.js turns into click-through everywhere else.

   Only two providers get a ring, Claude and Codex, because only a
   subscription has a quota to draw. Readings come from core/quota_usage.py.
*/
const bridge = window.usageOverlay || {};

/* Colour ramp (CodeNotch's palette values) */
const AMPLE = '#00FF88', WATCH = '#F2FF00', CRIT = '#FF3F00';
const TRACK = '#303030', INK = '#ffffff';
const tone = f => f >= 0.8 ? CRIT : f >= 0.5 ? WATCH : AMPLE;
// Whole percents, except where rounding would read as nothing used or nothing left
function smallPct(v) {
  if (v <= 0) return '0';
  const t = Math.round(v * 10) / 10;
  if (t < 0.1) return '<0.1';
  if (t > 99.9) return '>99.9';
  return t.toFixed(1);
}
function pctText(f) { const v = f * 100; return v > 0 && v < 1 ? smallPct(v) : String(Math.round(v)); }
function usedCopy(w) {
  const v = w.used * 100;
  let used, left;
  if ((v > 0 && v < 1) || (v > 99 && v < 100)) { used = smallPct(v); left = 100 - v > 99.9 ? '>99.9' : smallPct(Math.max(0, 100 - v)); }
  else { const u = Math.round(v); used = String(u); left = String(Math.max(0, 100 - u)); }
  return `${used}% Used · ${left}% left`;
}
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

/* ---- Readings --------------------------------------------------------------
   core/quota_usage.py reports {provider, status, windows:[{name, used_percent,
   resets_at (unix s)}], updated_at, note}. CodeNotch's page works in its own
   shape - {status, windows:[{id, label, used 0..1, resets_at ms}], fetched_at
   ms, note} - so readings are converted once, here, and everything below is
   CodeNotch's code reading CodeNotch's shape. */
const PROVIDERS = [
  { id: 'claude', name: 'Claude', glyph: 'C' },
  { id: 'codex', name: 'Codex', glyph: 'Cx' },
];
const WINDOW_IDS = { '5-hour': 'session', 'Weekly': 'weekly' };
const WINDOW_LABELS = { '5-hour': '5-hour limit', 'Weekly': 'Weekly limit' };
const STATUS = { ok: 'ok', stale: 'stale', needs_sign_in: 'needsAuth', rate_limited: 'stale', unavailable: 'stale' };
let snaps = {};  // provider id -> CodeNotch-shaped snapshot

function toSnap(p) {
  return {
    status: STATUS[p.status] || (p.stale ? 'stale' : 'ok'),
    windows: (p.windows || []).map(w => ({
      id: WINDOW_IDS[w.name] || w.name,
      label: WINDOW_LABELS[w.name] || w.name,
      used: Math.max(0, Number(w.used_percent) || 0) / 100,
      resets_at: w.resets_at ? w.resets_at * 1000 : 0,
    })),
    fetched_at: p.updated_at ? p.updated_at * 1000 : 0,
    note: p.note || '',
  };
}
function applyReadings(data) {
  const next = {};
  for (const p of (data && data.providers) || []) next[p.provider] = toSnap(p);
  snaps = next;
  renderRing();
  if (card.classList.contains('show')) renderCard();
}
function providers() {
  return PROVIDERS.filter(p => snaps[p.id]).map(p => ({ ...p, base: p.id, snap: snaps[p.id] }));
}

// Where the weekly limit's own ring goes: CodeNotch offers off, inside or outside
const weeklyRing = 'outside';
function headlineOf(snap) {
  const ws = snap.windows; if (!ws.length) return null;
  return ws.find(w => w.id === 'session') || ws[0];
}
function weeklyOf(snap) { return snap.windows.find(w => w.id === 'weekly') || null; }
// 15 minutes, CodeNotch's (and its Mac original's) stale threshold
function staleOf(snap) { if (snap.status === 'stale') return true; return snap.fetched_at > 0 && (Date.now() - snap.fetched_at) > 15 * 60 * 1000; }

function svgArc(r, frac, color, width, extra = '') {
  if (!(frac > 0)) return '';
  const C = 2 * Math.PI * r;
  return `<circle cx="28" cy="28" r="${r}" fill="none" stroke="${color}" stroke-width="${width}"
    stroke-dasharray="${(C * frac).toFixed(2)} ${C.toFixed(2)}" stroke-linecap="round"
    transform="rotate(-90 28 28)" ${extra}/>`;
}

const card = document.getElementById('card'), pill = document.getElementById('pill'), tail = document.getElementById('tail');

function renderRing() {
  const ps = providers();
  const want = ps.map(p => p.id).join(',');
  if (pill.dataset.cells !== want) {
    pill.innerHTML = ps.map((p, i) => `<div class="cell" data-p="${p.id}" style="--i:${i}">
      <div class="ringwrap"><svg class="ring" viewBox="0 0 56 56"></svg><svg class="reading" viewBox="0 0 56 56"></svg><svg class="activity" viewBox="0 0 56 56"></svg><div class="glyph ${p.glyph.length > 1 ? 'small' : ''}">${esc(p.glyph)}</div></div>
      <div class="pct">—</div></div>`).join('');
    pill.dataset.cells = want;
  }
  for (const p of ps) {
    const cell = pill.querySelector(`.cell[data-p="${p.id}"]`); if (!cell) continue;
    const svg = cell.querySelector('svg.ring'), reading = cell.querySelector('svg.reading'), pct = cell.querySelector('.pct'), glyph = cell.querySelector('.glyph'), wrap = cell.querySelector('.ringwrap');
    const h = headlineOf(p.snap);
    let inner = `<circle cx="28" cy="28" r="22" fill="#2a2a2a"/><circle cx="28" cy="28" r="25" fill="none" stroke="${TRACK}" stroke-width="5"/>`;
    wrap.classList.toggle('pressed', !!refreshing[p.id]);
    reading.innerHTML = h ? svgArc(25, Math.min(h.used, 1), tone(h.used), 5) : '';
    if (weeklyRing !== 'off') {  // the week, thinner, at its own radius: a session at 12% beside a week at 91% is why
      const wk = weeklyOf(p.snap);
      if (wk && (!h || wk.id !== h.id)) {
        const r = weeklyRing === 'inside' ? 16 : 31;
        inner += `<circle cx="28" cy="28" r="${r}" fill="none" stroke="${TRACK}" stroke-width="2.4" opacity="0.7"/>`
          + svgArc(r, Math.min(wk.used, 1), tone(wk.used), 2.4, 'opacity="0.85"');
      }
    }
    svg.innerHTML = inner;
    if (p.snap.status === 'needsAuth') pct.textContent = '—';
    else if (h) pct.textContent = pctText(h.used) + '%';
    else pct.textContent = '…';
    glyph.classList.toggle('dim', !!(h && h.used >= 1));
    wrap.classList.toggle('stale', staleOf(p.snap));
  }
  updateInteractive();
}

function resetCopy(ms) {
  if (!ms) return '';
  const diff = ms - Date.now();
  if (diff <= 0) return 'Resetting…';
  const min = Math.round(diff / 60000);
  if (min < 60) return `Resets in ${Math.max(1, min)} min`;
  const d = new Date(ms);
  // A weekday only names a day in the coming week
  if (daysApart(Date.now(), ms) >= 7) return 'Resets ' + d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  const t = d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  if (diff < 24 * 60 * 60 * 1000) return `Resets at ${t}`;
  return `Resets ${d.toLocaleDateString(undefined, { weekday: 'short' })} ${t}`;
}
function daysApart(from, to) {
  const day = ms => { const d = new Date(ms); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); };
  return Math.round((day(to) - day(from)) / 86400000);
}
function ago(ms) { const m = Math.round((Date.now() - ms) / 60000); return m < 60 ? `${m}m ago` : `${Math.round(m / 60)}h ago`; }

let hoverId = 'claude';
const SIGN_IN = { claude: 'Sign in to Claude Code to see usage.', codex: 'Sign in to Codex to see usage.' };
function renderCard() {
  const p = providers().find(x => x.id === hoverId) || providers()[0];
  if (!p) { hideCard(); return; }
  const snap = p.snap;
  let html = `<div class="c-head"><span class="c-title">${esc(p.name + ' Usage')}</span></div>`;
  if (staleOf(snap) && snap.fetched_at) html += `<div class="c-sub">Updated ${ago(snap.fetched_at)}</div>`;
  if (snap.status === 'needsAuth') {
    html += `<div class="c-note">${esc(SIGN_IN[p.id] || '')}<br>${esc(snap.note)}</div>`;
  } else if (!snap.windows.length) {
    html += `<div class="c-note">${esc(snap.note || 'Waiting for first reading…')}</div>`;
  } else {
    for (const w of snap.windows) {
      html += `<div class="win">
        <div class="w-row"><span class="w-label">${esc(w.label)}</span><span class="w-reset">${esc(resetCopy(w.resets_at))}</span></div>
        <div class="w-track"><div class="w-fill" style="width:${(Math.min(w.used, 1) * 100).toFixed(0)}%;background:${tone(w.used)}"></div></div>
        <div class="w-used">${esc(usedCopy(w))}</div>
      </div>`;
    }
    if (snap.note) html += `<div class="c-note">${esc(snap.note)}</div>`;
  }
  card.innerHTML = html;
  placeCard();
}
// The card follows the hovered cell, centred on it along the pill and kept inside the window
function placeCard() {
  const cell = pill.querySelector(`.cell[data-p="${hoverId}"]`) || pill;
  const cr = cell.getBoundingClientRect();
  const rr = (cell.querySelector('.ringwrap') || cell).getBoundingClientRect();
  card.style.transform = 'none';
  const cy = cr.top + cr.height / 2;
  const H = innerHeight, ch = card.offsetHeight || 0;
  let top = Math.round(cy - ch / 2); top = Math.max(8, Math.min(top, H - ch - 8));
  card.style.top = top + 'px'; card.style.left = '';
  const ry = rr.top + rr.height / 2;
  const th = tail.offsetHeight || 36, ty = Math.max(top + 16 + th / 2, Math.min(top + ch - 16 - th / 2, ry));
  tail.style.top = Math.round(ty - th / 2) + 'px'; tail.style.left = '';
}

/* Hover: stays open while the pill or the card is under the pointer; closes after CodeNotch's 250 ms grace */
let hideTimer = null;
function showCard() { clearTimeout(hideTimer); card.classList.add('show'); renderCard(); }
function hideCard() { card.classList.remove('show'); }
function scheduleHide() { clearTimeout(hideTimer); hideTimer = setTimeout(hideCard, 250); }

function inRect(x, y, r, pad) { return x >= r.left - pad && y >= r.top - pad && x < r.right + pad && y < r.bottom + pad; }
function cellAt(x, y) {
  for (const el of pill.querySelectorAll('.cell')) { if (inRect(x, y, el.getBoundingClientRect(), 6)) return el.dataset.p; }
  return null;
}
function pointerInHot(x, y) {
  const p = pill.getBoundingClientRect();
  if (inRect(x, y, p, 4)) return true;
  if (!card.classList.contains('show')) return false;
  const c = card.getBoundingClientRect();
  if (inRect(x, y, c, 4)) return true;
  const u = { left: Math.min(p.left, c.left), top: Math.min(p.top, c.top), right: Math.max(p.right, c.right), bottom: Math.max(p.bottom, c.bottom) };
  return inRect(x, y, u, 0);
}

/* ---- Click-through ----------------------------------------------------------
   CodeNotch reports hot rectangles to Rust, which gates clicks on them. Here the
   window ignores the mouse by default but still receives movement, and this page
   says whether the pointer is over something it should take clicks for. */
let interactive = null, lastPointer = null;
const WAKE_BAND = 34;  // CodeNotch's pillHotZone: the folded pill is small, the place that opens it should not be
function wakeHit(x, y) {
  const r = document.getElementById('rest').getBoundingClientRect();
  const band = notchEdge === 'left' ? { left: r.left, top: r.top, right: r.right + WAKE_BAND, bottom: r.bottom }
    : { left: r.left - WAKE_BAND, top: r.top, right: r.right, bottom: r.bottom };
  return inRect(x, y, band, 0);
}
function overNotch(x, y) {
  if (x == null) return false;
  if (folded) return wakeHit(x, y);
  return pointerInHot(x, y) || !!onHandle(x, y);
}
function updateInteractive() {
  const want = carrying || (lastPointer ? overNotch(lastPointer.x, lastPointer.y) : false);
  if (want === interactive) return;
  interactive = want;
  bridge.setInteractive && bridge.setInteractive(want);
}

/* ---- Refresh a ring ---------------------------------------------------------
   As in CodeNotch, a clicked ring presses in and its reading turns once while a
   fresh reading is fetched. PRESS_MIN keeps the press visible on a fast answer. */
const PRESS_MIN = 380;
const refreshing = {};
async function refreshRing(id) {
  if (refreshing[id]) return;
  refreshing[id] = Date.now();
  renderRing();
  turnReading(id);
  try {
    const response = await fetch('/api/models/quotas/refresh', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider: id }),
    });
    if (response.ok) applyReadings(await response.json());
    else if (response.status === 429) notice('Refreshed a moment ago. Try again shortly.');
    else notice('Could not refresh usage.');
  } catch { notice('JARVIS is not reachable.'); }
  const wait = refreshing[id] + PRESS_MIN - Date.now();
  setTimeout(() => { delete refreshing[id]; renderRing(); }, Math.max(0, wait));
}
function turnReading(id) {
  const el = pill.querySelector(`.cell[data-p="${id}"] svg.reading`);
  if (!el || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  if (el.getAnimations().some(a => a.playState === 'running')) return;
  el.animate([{ transform: 'rotate(0deg)' }, { transform: 'rotate(360deg)' }], { duration: 950, easing: 'cubic-bezier(.32,0,.14,1)' });
}

function notice(msg) {
  const n = document.getElementById('notice');
  n.textContent = msg; n.classList.add('show');
  clearTimeout(n._h); n._h = setTimeout(() => n.classList.remove('show'), 6000);
}

/* ---- The handles --------------------------------------------------------------
   Settings past the far end of the pill opens JARVIS; the move handle past the
   near end slides the notch along its edge. */
const orb = document.getElementById('orb'), moveHandle = document.getElementById('move');
const HANDLE_REACH = 28.5;
let orbAt = null, moveAt = null, hovered = null, orbSpins = 0, moveSpins = 0, carrying = false;
function put(el, x, y) {
  el.style.left = (x - HANDLE_REACH) + 'px'; el.style.top = (y - HANDLE_REACH) + 'px';
  el.classList.add('placed');
  return { x, y, reach: HANDLE_REACH };
}
function placeHandles() {
  const r = pill.getBoundingClientRect();
  if (!r.width) return false;
  const R = parseFloat(getComputedStyle(pill).getPropertyValue('--fillet')) || 38.7;
  const far = notchEdge === 'left' ? [r.left + R, r.bottom + R] : [r.right - R, r.bottom + R];
  const near = notchEdge === 'left' ? [r.left + R, r.top - R] : [r.right - R, r.top - R];
  orbAt = put(orb, far[0], far[1]);
  moveAt = put(moveHandle, near[0], near[1]);
  return true;
}
function near(at, x, y) { return !!at && Math.hypot(x - at.x, y - at.y) <= at.reach; }
function onHandle(x, y) { if (folded) return null; return near(orbAt, x, y) ? 'orb' : near(moveAt, x, y) ? 'move' : null; }
function setHovered(which) {
  if (which === hovered) return;
  hovered = which;
  orb.classList.toggle('hover', which === 'orb');
  moveHandle.classList.toggle('hover', which === 'move' || carrying);
}
function pressIn(el) {
  if (matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  el.animate([{ transform: 'scale(1)', easing: 'ease-out' }, { transform: 'scale(.84)', offset: .21, easing: 'cubic-bezier(.34,1.56,.64,1)' }, { transform: 'scale(1)' }], { duration: 430 });
}

/* ---- Fold on hover ------------------------------------------------------------
   CodeNotch's show-on-hover: folded, the notch rests as a 10 x 79 sliver at the
   edge; it opens on contact and folds a moment after the pointer has gone. It
   never folds while the notch is being carried. */
const FOLD_GRACE = 450;
let onHover = true, folded = true, pointerIn = false, foldTimer = null;
function foldAllowed() { return onHover && !pointerIn && !carrying; }
function setFolded(f) {
  if (f === folded) return;
  folded = f;
  if (f) { clearTimeout(hideTimer); hideCard(); setHovered(null); }
  document.body.classList.toggle('folded', f);
  requestAnimationFrame(() => { placeHandles(); updateInteractive(); });
}
function unfold() { clearTimeout(foldTimer); setFolded(false); }
function scheduleFold() {
  clearTimeout(foldTimer);
  if (!onHover) { setFolded(false); return; }
  if (foldAllowed()) foldTimer = setTimeout(() => { if (foldAllowed()) setFolded(true); }, FOLD_GRACE);
}

/* ---- Pointer ------------------------------------------------------------------ */
let press = null;
document.addEventListener('mousemove', e => {
  lastPointer = { x: e.clientX, y: e.clientY };
  if (carrying) { bridge.moveTo && bridge.moveTo(e.screenY); return; }
  const over = overNotch(e.clientX, e.clientY);
  if (over && folded) { pointerIn = true; unfold(); }
  pointerIn = over || pointerInHot(e.clientX, e.clientY);
  if (!pointerIn) scheduleFold(); else clearTimeout(foldTimer);
  updateInteractive();
  if (folded) return;
  setHovered(onHandle(e.clientX, e.clientY));
  if (hovered) { if (card.classList.contains('show')) { clearTimeout(hideTimer); hideCard(); } return; }
  if (pointerInHot(e.clientX, e.clientY)) {
    clearTimeout(hideTimer);
    const id = cellAt(e.clientX, e.clientY);
    if (id && id !== hoverId) { hoverId = id; if (card.classList.contains('show')) renderCard(); }
    if (!card.classList.contains('show') && providers().length) showCard();
  } else if (card.classList.contains('show')) scheduleHide();
});
document.addEventListener('mouseleave', () => {
  if (carrying) return;
  lastPointer = null; pointerIn = false; setHovered(null); scheduleHide(); scheduleFold(); updateInteractive();
});
pill.addEventListener('mousedown', e => { if (e.button !== 0 || folded) return; press = { id: cellAt(e.clientX, e.clientY) }; });
document.addEventListener('mouseup', e => {
  if (e.button !== 0) return;
  if (carrying) {
    carrying = false; moveHandle.classList.remove('armed'); setHovered(null);
    bridge.moveEnd && bridge.moveEnd();
    requestAnimationFrame(() => { placeHandles(); updateInteractive(); scheduleFold(); });
  } else if (press && press.id) refreshRing(press.id);
  press = null;
});
orb.addEventListener('click', e => {
  if (!near(orbAt, e.clientX, e.clientY)) return;
  orb.style.setProperty('--spins', ++orbSpins);
  pressIn(orb);
  bridge.openApp && bridge.openApp();
});
moveHandle.addEventListener('mousedown', e => {
  if (e.button !== 0 || !near(moveAt, e.clientX, e.clientY)) return;
  e.stopPropagation();
  carrying = true;
  moveHandle.classList.add('armed', 'hover');
  moveHandle.style.setProperty('--spins', ++moveSpins);
  clearTimeout(hideTimer); hideCard();
  bridge.moveStart && bridge.moveStart(e.screenY);
  updateInteractive();
});
document.addEventListener('contextmenu', e => e.preventDefault());

/* ---- Edge and settings, from the main process --------------------------------- */
let notchEdge = 'right';
function applyConfig(config) {
  const edge = config && config.edge === 'left' ? 'left' : 'right';
  notchEdge = edge;
  document.body.dataset.edge = edge;
  onHover = !(config && config.foldOnHover === false);
  if (!onHover) setFolded(false); else if (!pointerIn) setFolded(true);
  requestAnimationFrame(() => { placeHandles(); if (card.classList.contains('show')) placeCard(); updateInteractive(); });
}
bridge.onConfig && bridge.onConfig(applyConfig);
Promise.resolve(bridge.getConfig ? bridge.getConfig() : null).then(applyConfig).catch(() => applyConfig(null));

/* ---- Fetching ------------------------------------------------------------------ */
async function refresh() {
  try {
    const response = await fetch('/api/models/quotas', { credentials: 'same-origin', cache: 'no-store' });
    if (response.status === 401 || response.status === 403) { notice('Open JARVIS and sign in to see usage.'); return; }
    if (!response.ok) throw new Error('unavailable');
    applyReadings(await response.json());
  } catch {
    // Keep the last readings on screen, marked stale, rather than emptying the notch
    for (const id of Object.keys(snaps)) snaps[id] = { ...snaps[id], status: 'stale' };
    renderRing();
  }
}
refresh();
setInterval(refresh, 60_000);
setInterval(() => { renderRing(); if (card.classList.contains('show')) renderCard(); }, 30_000);  // reset copy moves with time
requestAnimationFrame(placeHandles);
