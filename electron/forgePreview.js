// Forge's separate local viewer. Port authority comes only from trusted main-process fetches.
const { WebContentsView, session, shell } = require('electron');
const PARTITION = 'persist:forge-preview';
const WIDTHS = Object.freeze({ desktop: null, tablet: 768, phone: 390 });
let host, view, backendPort, sessionId, getAllowedPorts;
let ports = new Set(), bounds = null, preset = 'desktop', poll = null, generation = 0;
let isolatedSession = null;

function safeUrl(raw) {
  try {
    const url = new URL(String(raw || ''));
    const port = Number(url.port || 80);
    if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname)
        || url.username || url.password || port === backendPort || !ports.has(port)) return null;
    return url.toString();
  } catch { return null; }
}
// Same loopback test as electron/browser.js: any localhost name, ::1, and
// 127.x / 0.0.0.0 literals (a prefix test would also match real domains).
function isLoopback(hostname) {
  const name = (hostname || '').toLowerCase().replace(/^\[|\]$/g, '');
  if (name === 'localhost' || name.endsWith('.localhost') || name === '::1' || name === '0:0:0:0:0:0:0:1') return true;
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(name)) {
    const octets = name.split('.').map(Number);
    if (octets.every(o => o <= 255)) return octets[0] === 127 || name === '0.0.0.0';
  }
  return false;
}
// Every request the page makes, not only navigations. The app's own ports
// (http, and ws for dev-server live reload) are allowed; every OTHER loopback
// address is refused, because that is where the credential-bearing backend
// and other local services live. Outside http(s) resources (fonts, CDNs,
// remote APIs) are allowed: this partition carries no Kairos credentials.
// Top-level navigation stays limited to the app's origin (will-navigate).
function requestAllowed(raw, resourceType) {
  try {
    const url = new URL(String(raw || ''));
    if (url.username || url.password) return false;
    const port = Number(url.port || (url.protocol === 'https:' || url.protocol === 'wss:' ? 443 : 80));
    if (isLoopback(url.hostname)) {
      return ['http:', 'ws:'].includes(url.protocol) && port !== backendPort && ports.has(port);
    }
    if (resourceType === 'mainFrame') return false;
    return ['http:', 'https:', 'ws:', 'wss:'].includes(url.protocol);
  } catch { return false; }
}
function outside(raw) {
  try {
    const url = new URL(raw);
    // The regular browser owns its own loopback/backend gate, unchanged.
    if (['http:', 'https:'].includes(url.protocol) && host && !host.isDestroyed()) {
      host.webContents.send('browser:open-request', url.toString());
    }
  } catch { /* invalid URL */ }
}
function navMethod(wc, name) {
  if (typeof wc.navigationHistory?.[name] === 'function') return wc.navigationHistory[name].bind(wc.navigationHistory);
  return typeof wc[name] === 'function' ? wc[name].bind(wc) : null;
}
function state() {
  if (!view || view.webContents.isDestroyed()) return { open: false };
  const wc = view.webContents;
  return { open: true, sessionId, url: wc.getURL(), loading: wc.isLoading(), width: preset,
    canGoBack: !!navMethod(wc, 'canGoBack')?.(), canGoForward: !!navMethod(wc, 'canGoForward')?.() };
}
function push() {
  if (host && !host.isDestroyed()) host.webContents.send('forge-preview:state', state());
}
function denyPermissionRequest(_wc, _permission, callback) { callback(false); }
function denyPermissionCheck() { return false; }
function denyDevicePermission() { return false; }
function create() {
  const isolated = isolatedSession || session.fromPartition(PARTITION);
  isolated.setPermissionRequestHandler(denyPermissionRequest);
  isolated.setPermissionCheckHandler(denyPermissionCheck);
  isolated.setDevicePermissionHandler(denyDevicePermission);
  // Also gate subresources and fetches. Blocking navigation alone would still
  // let a local app POST to the credential-bearing backend from page scripts.
  isolated.webRequest.onBeforeRequest((details, callback) => callback({ cancel: !requestAllowed(details.url, details.resourceType) }));
  if (!isolatedSession) isolated.on('will-download', event => event.preventDefault());
  isolatedSession = isolated;
  view = new WebContentsView({ webPreferences: { session: isolated, sandbox: true,
    contextIsolation: true, nodeIntegration: false, nodeIntegrationInWorker: false,
    webviewTag: false, spellcheck: false } });
  view.setBackgroundColor('#ffffff');
  const wc = view.webContents;
  wc.setWindowOpenHandler(({ url }) => {
    const safe = safeUrl(url);
    if (safe) wc.loadURL(safe).catch(() => {}); else outside(url);
    return { action: 'deny' };
  });
  for (const name of ['will-navigate', 'will-redirect', 'will-frame-navigate']) {
    wc.on(name, (event, url) => {
      const target = typeof url === 'string' ? url : event.url;
      if (!safeUrl(target)) { event.preventDefault(); outside(target); }
    });
  }
  for (const name of ['did-navigate', 'did-navigate-in-page', 'did-finish-load', 'did-stop-loading', 'did-start-loading']) wc.on(name, push);
  wc.on('did-fail-load', (_event, code, description, url, isMainFrame) => {
    if (isMainFrame && code !== -3 && host && !host.isDestroyed()) host.webContents.send('forge-preview:error', { description, url });
  });
  host.contentView.addChildView(view);
}
function attach(window, options = {}) {
  host = window;
  backendPort = Number(new URL(options.backendOrigin).port || 80);
  getAllowedPorts = options.getAllowedPorts;
}
async function refresh(id, token) {
  let list = [];
  try { list = await getAllowedPorts(id); } catch { /* fail closed */ }
  if (token !== generation || sessionId !== id) return false;
  ports = new Set(Array.isArray(list) ? list.filter(p => Number.isInteger(p) && p > 0 && p < 65536 && p !== backendPort) : []);
  return true;
}
async function open(id, rawUrl, rect) {
  close(); sessionId = String(id); const token = generation;
  if (!host || host.isDestroyed() || !await refresh(sessionId, token)) return { ok: false, reason: 'Preview unavailable.' };
  const target = safeUrl(rawUrl);
  if (!target) return { ok: false, reason: 'Only this session\'s running app ports can be previewed.' };
  create(); if (rect) setBounds(rect); view.setVisible(true);
  view.webContents.loadURL(target).catch(() => {});
  poll = setInterval(async () => {
    const id = sessionId, token = generation;
    if (await refresh(id, token) && view && (!ports.size || (view.webContents.getURL() && !safeUrl(view.webContents.getURL())))) close();
  }, 1000);
  push(); return { ok: true, url: target };
}
async function navigate(rawUrl) {
  if (!view || !await refresh(sessionId, generation)) return { ok: false };
  const target = safeUrl(rawUrl);
  if (!target) { outside(rawUrl); return { ok: false, reason: 'Choose a path on the running app.' }; }
  view.webContents.loadURL(target).catch(() => {}); return { ok: true, url: target };
}
function setBounds(rect) {
  if (!view || !rect || !['x', 'y', 'width', 'height'].every(key => Number.isFinite(rect[key]))) return;
  bounds = rect;
  const width = Math.max(0, Math.min(rect.width, WIDTHS[preset] || rect.width));
  view.setBounds({ x: Math.round(rect.x + (rect.width - width) / 2), y: Math.round(rect.y),
    width: Math.round(width), height: Math.max(0, Math.round(rect.height)) });
}
function setWidth(value) { if (Object.hasOwn(WIDTHS, value)) { preset = value; if (bounds) setBounds(bounds); push(); } }
function setVisible(value) { view?.setVisible(!!value); }
async function history(direction) {
  if (!view || !await refresh(sessionId, generation)) return;
  const wc = view.webContents;
  if (navMethod(wc, direction === 'back' ? 'canGoBack' : 'canGoForward')?.()) navMethod(wc, direction === 'back' ? 'goBack' : 'goForward')?.();
}
async function reload() {
  if (view && await refresh(sessionId, generation) && safeUrl(view.webContents.getURL())) view.webContents.reload();
}
async function openExternal() {
  if (!view || !await refresh(sessionId, generation)) return { ok: false };
  const target = safeUrl(view.webContents.getURL());
  if (!target) return { ok: false };
  await shell.openExternal(target); return { ok: true };
}
function close() {
  ++generation; clearInterval(poll); poll = null; ports.clear(); sessionId = null;
  const current = view; view = null;
  if (current) {
    try { host?.contentView.removeChildView(current); } catch { /* host already gone */ }
    if (!current.webContents.isDestroyed()) {
      if (typeof current.webContents.close === 'function') current.webContents.close();
      else current.webContents.destroy();
    }
  }
  push();
}
module.exports = { PARTITION, WIDTHS, attach, open, navigate, setBounds, setWidth, setVisible,
  goBack: () => history('back'), goForward: () => history('forward'), reload, openExternal, close, state,
  _safeUrl: safeUrl, _requestAllowed: requestAllowed, _webContents: () => view?.webContents,
  _denyPermissionRequest: denyPermissionRequest, _denyPermissionCheck: denyPermissionCheck,
  _denyDevicePermission: denyDevicePermission };
