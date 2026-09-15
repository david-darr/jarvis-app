// electron/browser.js — the in-app side browser (David's ask 2026-09-15).
//
// A real Chromium view hosted inside the app window, not an iframe: iframes
// can't show most of the web, because any site sending X-Frame-Options or a
// frame-ancestors CSP simply refuses to render in one. The plain-HTTP web
// client still falls back to an iframe (see static/js/browserPane.js) and is
// honest about that limit; this file is the desktop half that doesn't have it.
//
// The whole design point is that a remote page gets NONE of JARVIS's
// privileges. The app's own renderer is trusted: it talks to a loopback
// backend that holds the user's vault, credentials, and agent tools. An
// arbitrary website must never be able to reach any of that, so:
//
//   - It runs on its own session partition, which means its own cookie jar
//     and storage. A site cannot see the app's session, and signing into
//     something here never touches JARVIS's own auth.
//   - No preload script and no node integration, so there is no bridge
//     object to find. sandbox: true keeps it in a real OS sandbox.
//   - Only http/https ever load. file:, data:, blob: and friends are refused
//     outright rather than filtered, so there is nothing clever to smuggle.
//   - Loopback is blocked entirely. The backend lives on 127.0.0.1 and is
//     the privileged surface this feature must never expose; other local
//     ports may be someone else's admin panel, so the whole range is off
//     rather than just the one port we happen to know about.
//   - Every permission request (camera, mic, location, notifications, ...)
//     is denied without prompting. A prompt raised by an untrusted page,
//     inside the app's own window, would read as JARVIS asking.
//   - Nothing here gives the agent access to page content. This is a viewer
//     for the person using the app; no tool reads from it.
//
// Lifecycle matters as much as the sandbox. A WebContentsView is a NATIVE
// layer composited above the HTML, so it ignores z-index and would otherwise
// paint straight through Settings, modals, and menus — hide() exists for
// exactly that, and callers use it rather than trying to out-stack it in CSS.
// The view is destroyed on close, on session switch, and on app quit, so a
// page can't keep running (or keep playing audio) behind the app.

const { WebContentsView, session, shell } = require("electron");

// persist: so a signed-in site survives closing the pane, matching what a
// browser does. Deliberately NOT the app's default session — that separation
// is the cookie-isolation guarantee above, not an implementation detail.
const PARTITION = "persist:jarvis-browser";

let view = null;
let host = null;
let backendOrigin = null;
let notify = () => {};

function isLoopback(hostname) {
  const name = (hostname || "").toLowerCase().replace(/^\[|\]$/g, "");
  if (name === "localhost" || name.endsWith(".localhost")) return true;
  if (name === "::1" || name === "0:0:0:0:0:0:0:1") return true;
  // Only a bare IPv4 literal can be a loopback address. A prefix test like
  // /^127\./ also matches "127.0.0.1.example.test", which is an ordinary
  // registrable domain someone can own — blocking it would be wrong, and
  // silently so. Caught by scripts/browser-smoke.cjs.
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(name)) {
    const octets = name.split(".").map(Number);
    if (octets.every((o) => o <= 255)) return octets[0] === 127 || name === "0.0.0.0";
  }
  return false;
}

// Single gate for every entry point: the address bar, link clicks,
// redirects, and window.open all funnel through this. Returns a normalized
// absolute URL string, or null when the target must not load.
function safeUrl(raw) {
  let parsed;
  try {
    parsed = new URL(String(raw || "").trim());
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  if (isLoopback(parsed.hostname)) return null;
  // Belt and braces: even if the backend somehow isn't on loopback (a
  // JARVIS_BACKEND_URL override), its origin stays off limits.
  if (backendOrigin && parsed.origin === backendOrigin) return null;
  return parsed.toString();
}

// What the user types is not a URL yet. "example.com" and "how tall is k2"
// both arrive here; the first is a host, the second is a search.
function fromAddressBar(input) {
  const text = String(input || "").trim();
  if (!text) return null;
  if (/^[a-z][a-z0-9+.-]*:/i.test(text)) return safeUrl(text);
  // A bare token with a dot and no spaces is a hostname; anything else is a
  // search phrase. Deliberately simple and predictable rather than clever.
  if (/^[^\s/]+\.[^\s/]{2,}(\/|$|\?|#)/i.test(text)) return safeUrl(`https://${text}`);
  return safeUrl(`https://duckduckgo.com/?q=${encodeURIComponent(text)}`);
}

// Navigation history moved from webContents onto webContents.navigationHistory
// across Electron versions, and the two overlap: on the version bundled here
// (31.x) the navigationHistory OBJECT already exists while its canGoBack /
// goBack methods do not. Detecting the object rather than the method is
// therefore wrong, and wrong in a way that is easy to miss — state() is
// called from push(), which is wired to half a dozen load events, so the
// TypeError fires inside Electron's own event dispatch and wedges the page
// load rather than surfacing as a clean error. Found exactly that way by
// scripts/browser-smoke.cjs. Always feature-detect the method.
function navMethod(wc, name) {
  const nav = wc.navigationHistory;
  if (nav && typeof nav[name] === "function") return nav[name].bind(nav);
  if (typeof wc[name] === "function") return wc[name].bind(wc);
  return null;
}

function state() {
  if (!view || view.webContents.isDestroyed()) return { open: false };
  const wc = view.webContents;
  const canBack = navMethod(wc, "canGoBack");
  const canForward = navMethod(wc, "canGoForward");
  return {
    open: true,
    url: wc.getURL(),
    title: wc.getTitle(),
    loading: wc.isLoading(),
    canGoBack: canBack ? canBack() : false,
    canGoForward: canForward ? canForward() : false,
  };
}

// Called from webContents event handlers, so it runs inside Electron's own
// event dispatch. Anything that throws here propagates into that dispatch
// and can stall the page load itself rather than surfacing as a clean error
// — which is precisely what a version-detection bug in state() did. A status
// update is never worth breaking navigation over.
function push() {
  try {
    if (host && !host.isDestroyed()) host.webContents.send("browser:state", state());
  } catch { /* status is best-effort */ }
}

// Named rather than inline so scripts/browser-smoke.cjs can assert the real
// shipped logic denies, instead of only checking that a handler was
// installed. Every permission Chromium can ask for is refused without a
// prompt: a permission dialog raised by an arbitrary site, inside JARVIS's
// own window, would read as JARVIS asking for the microphone.
function denyPermissionRequest(_wc, _permission, callback) { callback(false); }
function denyPermissionCheck() { return false; }
function denyDevicePermission() { return false; }

function buildSession() {
  const partition = session.fromPartition(PARTITION);
  partition.setPermissionRequestHandler(denyPermissionRequest);
  partition.setPermissionCheckHandler(denyPermissionCheck);
  // Device pickers are a separate path from the permission handlers above.
  partition.setDevicePermissionHandler(denyDevicePermission);
  return partition;
}

function create() {
  const browserSession = buildSession();
  view = new WebContentsView({
    webPreferences: {
      session: browserSession,
      // No preload: there is deliberately no bridge for a page to find.
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      nodeIntegrationInWorker: false,
      webviewTag: false,
      // webSecurity is left ON. Turning it off to "make sites work" would
      // hand every page the same-origin policy as a weapon.
      spellcheck: false,
    },
  });
  view.setBackgroundColor("#ffffff");

  const wc = view.webContents;

  // target=_blank and window.open stay inside this pane when they're
  // ordinary web links, and are refused otherwise. Never a real new window:
  // that would be a chrome-less Electron window with no address bar, which
  // is precisely the shape a phishing page wants.
  wc.setWindowOpenHandler(({ url }) => {
    const safe = safeUrl(url);
    if (safe) wc.loadURL(safe);
    return { action: "deny" };
  });

  // Covers redirects and in-page navigation, not just what the user typed.
  wc.on("will-navigate", (event, url) => {
    if (!safeUrl(url)) {
      event.preventDefault();
      push();
    }
  });
  wc.on("will-redirect", (event, url) => {
    if (!safeUrl(url)) event.preventDefault();
  });

  // A download inside a sandboxed pane would write to disk with the app's
  // privileges. Hand the URL to the real browser instead, where the user's
  // normal download UI and safe-browsing checks apply.
  browserSession.on("will-download", (event, item) => {
    event.preventDefault();
    const safe = safeUrl(item.getURL());
    if (safe) shell.openExternal(safe).catch(() => {});
  });

  for (const evt of ["did-navigate", "did-navigate-in-page", "did-finish-load", "did-stop-loading", "did-start-loading", "page-title-updated"]) {
    wc.on(evt, push);
  }
  wc.on("did-fail-load", (_e, code, description, failedUrl, isMainFrame) => {
    // -3 is ERR_ABORTED, which fires routinely when a navigation is
    // superseded — not a failure worth showing anyone.
    if (!isMainFrame || code === -3) return;
    if (host && !host.isDestroyed()) {
      host.webContents.send("browser:error", { code, description, url: failedUrl });
    }
  });

  host.contentView.addChildView(view);
  return view;
}

function attach(window, options = {}) {
  host = window;
  backendOrigin = options.backendOrigin || null;
  notify = options.notify || (() => {});
}

function open(rawUrl, bounds) {
  if (!host || host.isDestroyed()) return { ok: false, reason: "no window" };
  const target = fromAddressBar(rawUrl);
  if (!target) return { ok: false, reason: "That address can't be opened here." };
  if (!view || view.webContents.isDestroyed()) create();
  if (bounds) setBounds(bounds);
  view.setVisible(true);
  view.webContents.loadURL(target);
  push();
  return { ok: true, url: target };
}

function navigate(rawUrl) {
  if (!view || view.webContents.isDestroyed()) return { ok: false, reason: "not open" };
  const target = fromAddressBar(rawUrl);
  if (!target) return { ok: false, reason: "That address can't be opened here." };
  view.webContents.loadURL(target);
  push();
  return { ok: true, url: target };
}

// Rounded to whole pixels: the renderer measures with getBoundingClientRect,
// which returns fractions, and a native view placed on a fractional boundary
// shimmers against the HTML behind it.
function setBounds(rect) {
  if (!view || view.webContents.isDestroyed() || !rect) return;
  view.setBounds({
    x: Math.round(rect.x || 0),
    y: Math.round(rect.y || 0),
    width: Math.max(0, Math.round(rect.width || 0)),
    height: Math.max(0, Math.round(rect.height || 0)),
  });
}

// Used whenever HTML must appear above the pane — Settings, modals, menus.
// A native view is composited over the page, so this is the only thing that
// actually works; no amount of z-index will.
function setVisible(visible) {
  if (!view || view.webContents.isDestroyed()) return;
  view.setVisible(!!visible);
}

function goBack() {
  if (!view || view.webContents.isDestroyed()) return;
  const can = navMethod(view.webContents, "canGoBack");
  const go = navMethod(view.webContents, "goBack");
  if (can && go && can()) go();
}

function goForward() {
  if (!view || view.webContents.isDestroyed()) return;
  const can = navMethod(view.webContents, "canGoForward");
  const go = navMethod(view.webContents, "goForward");
  if (can && go && can()) go();
}

function reload() {
  if (view && !view.webContents.isDestroyed()) view.webContents.reload();
}

function openExternal() {
  if (!view || view.webContents.isDestroyed()) return { ok: false };
  const safe = safeUrl(view.webContents.getURL());
  if (!safe) return { ok: false };
  shell.openExternal(safe).catch(() => {});
  return { ok: true };
}

// Fully torn down, not merely hidden: a hidden view keeps running scripts,
// timers, and audio. Called on pane close, on chat-session switch, and on
// app quit.
function close() {
  if (!view) return;
  const current = view;
  view = null;
  try {
    if (host && !host.isDestroyed()) host.contentView.removeChildView(current);
  } catch { /* window already gone */ }
  try {
    const wc = current.webContents;
    if (!wc.isDestroyed()) {
      if (typeof wc.close === "function") wc.close();
      else wc.destroy();
    }
  } catch { /* already torn down */ }
  push();
  notify("closed");
}

function isOpen() {
  return !!(view && !view.webContents.isDestroyed());
}

module.exports = {
  PARTITION, attach, open, navigate, setBounds, setVisible,
  goBack, goForward, reload, openExternal, close, isOpen, state,
  // Exported for scripts/browser-smoke.cjs, which asserts the blocking rules
  // directly rather than inferring them from behaviour. _webContents lets
  // that suite run JS inside the loaded page to prove the sandbox is real —
  // the one claim that cannot be checked from outside.
  _safeUrl: safeUrl, _fromAddressBar: fromAddressBar, _isLoopback: isLoopback,
  _denyPermissionRequest: denyPermissionRequest,
  _denyPermissionCheck: denyPermissionCheck,
  _denyDevicePermission: denyDevicePermission,
  _webContents: () => (view && !view.webContents.isDestroyed() ? view.webContents : null),
};
