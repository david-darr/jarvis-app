import { ICONS } from "./icons.js";
import { api } from "./api.js";
import * as onboarding from "./onboarding.js";
import * as auth from "./auth.js";
import * as commandPalette from "./commandPalette.js";

// Nav order matches David's Figma wireframe, minus "New Chat" and "Search"
// as separate items (David's call, 2026-08-31) — both live inside the Chats
// view itself (its own "+ New Chat" button, and chat-message search once
// that's built server-side), so dedicated top-level nav entries were just
// redundant with Chats. Settings moved out of this list entirely (David's
// ask, 2026-08-31) — it's now the sidebar-footer user card instead of a
// top-level nav item, matching the Odysseus screenshot's bottom-left
// avatar+username+gear pattern.
const NAV = [
  { id: "home", label: "Home", icon: "home" },
  { id: "chat", label: "Chats", icon: "chats" },
  { id: "notes", label: "Notes", icon: "notes" },
  { id: "library", label: "Library", icon: "library" },
  { id: "calendar", label: "Calendar", icon: "calendar" },
  { id: "email", label: "Email", icon: "email" },
  { id: "tasks", label: "Tasks", icon: "tasks" },
  { id: "brain", label: "Brain", icon: "brain" },
  { id: "cookbook", label: "Cookbook", icon: "cookbook" },
];

// No tabs currently stubbed — Gallery was removed entirely (David's ask
// 2026-09-01, "we don't need it anymore"), Library shipped for real in
// Phase 7. Left as a real (if empty) mechanism rather than deleted outright:
// still the honest place a future genuinely-deferred tab would go, matching
// the project's "no fake UI" rule.
const STUB_TABS = new Set();

const modules = {};
// Tabs the user built live in the data directory (so app updates can't wipe
// them) and are served from /custom-views rather than the bundled
// static/js/views/. The server tells us which is which via the manifest's
// view_url; anything without one uses the built-in relative path.
const customViewUrls = {};

async function loadModule(tabId) {
  if (modules[tabId]) return modules[tabId];
  const custom = customViewUrls[tabId];
  const path = custom || (STUB_TABS.has(tabId) ? "./views/stub.js" : `./views/${tabId}.js`);
  modules[tabId] = await import(path);
  return modules[tabId];
}

let activeTab = null;
let activeUnmount = null; // set by a view's render() if it needs teardown (e.g. home.js's WebGL scene)

// Captured once, never re-queried by id. The first fix attempt looked up
// `document.getElementById("view-content")` fresh on every switchTab() call
// — but chat.js used to change the container's own `id` attribute for its
// layout, so after visiting Chat once, that id no longer existed on the
// page and every later lookup silently returned null, leaving Chat's old
// content on screen with no error. Root fix: never let a view module mutate
// this element's id at all (chat.js now uses a CSS class instead — see
// .chat-layout in style.css), and hold one stable reference here so a
// lookup-by-id can never go stale in the first place.
const view = document.getElementById("view-content");

async function switchTab(tabId) {
  // Views that own real resources (currently just home.js's WebGL scene)
  // return a cleanup function from render(). Without calling it here before
  // wiping the DOM, a canvas's animation loop and GPU buffers would keep
  // running forever in the background every time you left that tab — the
  // canvas element is gone, but requestAnimationFrame doesn't know that.
  if (activeUnmount) { activeUnmount(); activeUnmount = null; }

  activeTab = tabId;
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.tab === tabId);
  });
  view.className = "view active";
  view.removeAttribute("style");
  view.innerHTML = `<div class="empty-state">Loading...</div>`;
  const mod = await loadModule(tabId);
  const result = await mod.render(view, tabId);
  if (typeof result === "function") activeUnmount = result;
  closeMobileMenu();
}

// Mobile sidebar drawer (David's ask 2026-09-01, "similar UX/UI as Claude
// and ChatGPT's mobile apps") — #sidebar becomes a slide-out overlay below
// the responsive breakpoint (style.css's @media block); this just toggles
// the class + backdrop. Inert above the breakpoint since the button/backdrop
// are display:none there.
function openMobileMenu() {
  document.getElementById("sidebar").classList.add("open");
  document.getElementById("mobile-backdrop").classList.remove("hidden");
}
function closeMobileMenu() {
  document.getElementById("sidebar").classList.remove("open");
  document.getElementById("mobile-backdrop").classList.add("hidden");
}
function setupMobileMenu() {
  const btn = document.getElementById("mobile-menu-btn");
  const backdrop = document.getElementById("mobile-backdrop");
  btn.innerHTML = ICONS.menu;
  btn.addEventListener("click", () => {
    document.getElementById("sidebar").classList.contains("open") ? closeMobileMenu() : openMobileMenu();
  });
  backdrop.addEventListener("click", closeMobileMenu);
}

async function buildSidebar() {
  const brand = document.getElementById("brand");
  brand.innerHTML = `<img src="/static/img/jarvis-logo.png" alt="" class="brand-logo">JARVIS`;

  const nav = document.getElementById("nav");
  // Cleared before rebuilding — buildSidebar() now also runs whenever
  // Developer Mode is toggled (to show/hide "+ New Tab" live), not just
  // once at boot, so without this every toggle click appended a second
  // full copy of the nav on top of the first (the reported duplicate-tabs
  // bug, 2026-09-01).
  nav.innerHTML = "";
  for (const item of NAV) {
    const navEl = document.createElement("div");
    navEl.className = "nav-item";
    navEl.dataset.tab = item.id;
    navEl.innerHTML = `${ICONS[item.icon] || ""}<span>${item.label}</span>`;
    navEl.addEventListener("click", () => switchTab(item.id));
    nav.appendChild(navEl);
  }

  // Custom tabs (Developer Mode, David's ask 2026-09-01) — discovered
  // server-side from routes/tab_*.py (core/custom_tabs.py), appended after
  // the built-in NAV so a new tab never needs this array or icons.js
  // edited. Always shown once built, not gated behind Developer Mode being
  // on — that toggle is a cosmetic/context signal, not a visibility gate.
  // Best-effort: a fetch failure here shouldn't break the built-in nav.
  const customTabs = await api("/api/system/custom-tabs").catch(() => []);
  for (const item of customTabs) {
    if (item.view_url) customViewUrls[item.id] = item.view_url;
    const navEl = document.createElement("div");
    navEl.className = "nav-item";
    navEl.dataset.tab = item.id;
    navEl.innerHTML = `${item.icon_svg || ""}<span>${item.label}</span>`;
    navEl.addEventListener("click", () => switchTab(item.id));
    nav.appendChild(navEl);
  }

  // "+" New Tab (Developer Mode only, David's ask 2026-09-01) — at the
  // bottom of the nav list itself, below any custom tabs, distinct from
  // the Developer Mode toggle in the sidebar footer below. Rebuilt by
  // buildDeveloperModeRow()'s toggle handler so it appears/disappears
  // immediately without a page reload.
  if (document.documentElement.classList.contains("dev-mode")) {
    const newTabEl = document.createElement("div");
    newTabEl.className = "nav-item nav-item-new-tab";
    newTabEl.dataset.tab = "new-tab";
    newTabEl.innerHTML = `${ICONS.plus || ""}<span>New Tab</span>`;
    newTabEl.addEventListener("click", () => switchTab("new-tab"));
    nav.appendChild(newTabEl);
  }

  buildSidebarFooter();
  return customTabs;
}

// Settings' old nav-item slot replaced with the sidebar footer (David's ask
// 2026-08-31, follow-up same day: split into two separate controls rather
// than one combined card). The username card opens a Switch User/Log Out
// menu; the gear button is its own separate click straight into the
// floating Settings window — they're related but distinct actions, not one
// thing.
// Developer Mode toggle (David's ask 2026-09-01) — its own row, directly
// above the user-card/settings row. Reads current state off the
// documentElement class boot() already set rather than re-fetching
// /api/settings (admin-gated — a second call here would 401 for a
// non-admin user and break the whole sidebar footer over one toggle).
function buildDeveloperModeRow() {
  const row = document.createElement("div");
  row.className = "sidebar-footer-row sidebar-devmode-row";

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sidebar-devmode-btn";
  const sync = () => {
    const on = document.documentElement.classList.contains("dev-mode");
    btn.classList.toggle("active", on);
    btn.title = on ? "Developer Mode is on — click to turn off" : "Turn on Developer Mode";
  };
  btn.innerHTML = `${ICONS.devMode || ""}<span>Developer Mode</span>`;
  sync();

  btn.addEventListener("click", async () => {
    const next = !document.documentElement.classList.contains("dev-mode");
    document.documentElement.classList.toggle("dev-mode", next);
    sync();
    buildSidebar(); // rebuilds the nav so "+ New Tab" appears/disappears live
    try {
      await api("/api/settings/developer-mode", { method: "POST", body: JSON.stringify({ enabled: next }) });
    } catch (_) {
      // Couldn't persist (e.g. non-admin user) — revert the visual flip
      // rather than leaving the UI claiming a state that didn't save.
      document.documentElement.classList.toggle("dev-mode", !next);
      sync();
      buildSidebar();
    }
  });

  row.appendChild(btn);
  return row;
}

async function buildSidebarFooter() {
  const footer = document.getElementById("sidebar-footer");
  footer.innerHTML = "";
  footer.appendChild(buildDeveloperModeRow());

  const status = await api("/api/auth/status").catch(() => null);
  const displayName = !status ? "…" : status.username === "local" ? "Local User" : status.username;

  const row = document.createElement("div");
  row.className = "sidebar-footer-row";

  const card = document.createElement("div");
  card.className = "sidebar-user-card";
  card.innerHTML = `<span class="sidebar-user-name">${displayName}</span>`;

  const menu = document.createElement("div");
  menu.className = "overflow-menu hidden";
  menu.style.cssText = "position:absolute;bottom:calc(100% + 6px);left:0;min-width:170px;";

  // No stored multi-account sessions exist to actually swap accounts without
  // re-entering a password (see core/auth.py) — "Switch User" logs out and
  // lands back on the real login screen for the next person to sign in,
  // same as Log Out. Real gating here, though (David's follow-up ask
  // 2026-08-31): Switch User is only enabled when a second account
  // genuinely exists (`other_users_exist` from /api/auth/status, a plain
  // boolean — the actual username list stays admin-only, see the route's
  // own comment) so it isn't offered as a meaningful action when there's
  // truly nowhere else to switch to.
  const authOff = status && !status.auth_enabled;
  const switchItem = document.createElement("button");
  switchItem.type = "button";
  switchItem.className = "overflow-menu-item";
  switchItem.textContent = "Switch User";
  const logoutItem = document.createElement("button");
  logoutItem.type = "button";
  logoutItem.className = "overflow-menu-item";
  logoutItem.textContent = "Log Out";

  if (authOff) {
    switchItem.disabled = true;
    switchItem.title = "Auth is off (single local user) — nothing to switch to.";
    logoutItem.disabled = true;
    logoutItem.title = "Auth is off (single local user) — nothing to log out of.";
  } else {
    if (!status.other_users_exist) {
      switchItem.disabled = true;
      switchItem.title = "No other users registered yet — add one in Settings > Admin > Users.";
    } else {
      switchItem.addEventListener("click", async () => {
        await api("/api/auth/logout", { method: "POST" });
        window.location.reload();
      });
    }
    logoutItem.addEventListener("click", async () => {
      await api("/api/auth/logout", { method: "POST" });
      window.location.reload();
    });
  }
  menu.append(switchItem, logoutItem);

  const cardWrap = document.createElement("div");
  cardWrap.style.cssText = "position:relative;flex:1;min-width:0;";
  cardWrap.append(card, menu);
  card.addEventListener("click", (e) => { e.stopPropagation(); menu.classList.toggle("hidden"); });
  document.addEventListener("click", () => menu.classList.add("hidden"));

  const settingsBtn = document.createElement("button");
  settingsBtn.type = "button";
  settingsBtn.className = "sidebar-settings-btn";
  settingsBtn.title = "Settings";
  settingsBtn.innerHTML = ICONS.settings || "";
  settingsBtn.addEventListener("click", openSettings);

  row.append(cardWrap, settingsBtn);
  footer.appendChild(row);
}

// Shared by the sidebar gear button and the command palette (Ctrl+K).
async function openSettings() {
  const settings = await import("./views/settings.js");
  // Mobile gets a real full-screen page, not the floating popup window
  // (David's ask 2026-09-01) — same setup switchTab() does (stop any
  // running view's cleanup, clear nav highlighting, reset the shared
  // view container) since this bypasses switchTab() itself to avoid
  // settings.render()'s hardcoded desktop-modal behavior.
  if (window.matchMedia("(max-width: 768px)").matches) {
    if (activeUnmount) { activeUnmount(); activeUnmount = null; }
    activeTab = null;
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.remove("active"));
    view.className = "view active settings-mobile-page";
    closeMobileMenu();
    await settings.renderMobilePage(view);
    return;
  }
  await settings.openSettingsWindow();
}

async function boot() {
  const overlay = document.getElementById("onboarding-overlay");

  // Must resolve (login/setup, or no-op if AUTH_ENABLED=false) before any
  // authenticated call below — /api/settings is admin-gated and used to
  // 401 silently here with nothing on screen to show for it.
  overlay.classList.remove("hidden");
  await auth.run(overlay);
  overlay.classList.add("hidden");

  const settings = await api("/api/settings");
  const app = document.getElementById("app");

  // Developer Mode (David's ask 2026-09-01) — applied at boot from the
  // persisted setting; toggleDeveloperMode() (sidebar footer) flips it live.
  document.documentElement.classList.toggle("dev-mode", !!settings.developer_mode_enabled);

  if (!settings.onboarding_complete) {
    app.style.display = "none";
    overlay.classList.remove("hidden");
    await onboarding.run(overlay, () => {
      overlay.classList.add("hidden");
      overlay.innerHTML = "";
      app.style.display = "";
      startApp();
    });
    return;
  }

  startApp();
}

async function startApp() {
  const customTabs = await buildSidebar();
  setupMobileMenu();
  // Adding/removing a premade tab (views/new-tab.js) rebuilds the nav so it
  // appears immediately instead of after a reload.
  document.addEventListener("jarvis:tabs-changed", () => { buildSidebar(); });
  commandPalette.init({ nav: NAV, customTabs: customTabs || [], switchTab, openSettings });
  await switchTab("home");
}

boot();
