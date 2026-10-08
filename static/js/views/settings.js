import { api, el, customSelect, toast, confirmDialog, modelMark } from "../api.js";
import { suppressBrowser, releaseBrowser } from "../browserPane.js";
import { renderSpeechPanel } from "./speechPanel.js";
import { renderAppearancePanel } from './appearancePanel.js';
import { renderLayoutPanel } from './layoutPanel.js';
import { renderChannelsPanel } from "./settingsChannels.js";
import { runSummary, surfaceLabel, toggleRunTimeline } from "../runTimeline.js";
import { renderHooksPanel } from "./settingsHooks.js";
import { pageHeader, group, row, field, toggle, pill, note, empty, badge, hueFor } from "../settingsKit.js";

// The side browser's page is a NATIVE view composited above the HTML in the
// desktop app (see static/js/browserPane.js), so it would paint straight
// through this window no matter what z-index it carried. Hiding it is the
// only thing that actually works. Tracked with a flag so open/close/minimize
// can't leave the suppress counter unbalanced and the page hidden forever.
let browserHidden = false;
function hidePageBehind() {
  if (browserHidden) return;
  browserHidden = true;
  suppressBrowser();
}
function restorePageBehind() {
  if (!browserHidden) return;
  browserHidden = false;
  releaseBrowser();
}

// Same display convention chat.js's model picker uses (name + underlying
// model, CLI-aware) — duplicated rather than shared since the two modules
// build their pickers with different components (native menu vs. customSelect).
function modelLabel(ep) {
  return ep.kind === "claude_cli" || ep.kind === "codex_cli" ? `${ep.name} (${ep.model || "CLI default"})` : `${ep.name} (${ep.model})`;
}

const errorText = (problem) => (problem?.message || String(problem)).replace(/^\d+: /, "");

// Settings is a floating window (David, 2026-08-31: "a pop up that can be
// closed or minimized") with a grouped, searchable nav and one page on the
// right. Redesigned 2026-10-05 after Hermes's desktop settings, Codex and
// Claude: an icon per section, a page header with a one-line description, and
// every page built from static/js/settingsKit.js (groups of label-left,
// control-right rows) instead of stacked cards. No setting's behaviour or gate
// changed in the redesign.
//
// `keywords` is what makes the search box useful: the words someone would
// really type for each page ("2fa", "api key", "tailscale"), not a
// restatement of its title. `admin: true` on a group builds it for admins
// only. Tabs are managed in the Tool Store.
const I = (body) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const NAV_ICONS = {
  "add-models": I('<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z"/><path d="M12 9v6M9 12h6"/>'),
  "added-models": I('<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>'),
  integrations: I('<path d="M9 2v5M15 2v5M12 17v5"/><path d="M6 7h12v4a6 6 0 01-6 6 6 6 0 01-6-6V7z"/>'),
  channels: I('<path d="M4 5h16v11H9l-5 4V5z"/><path d="M8 10h8M8 13h5"/>'),
  remote: I('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 010 18M12 3a14 14 0 000 18"/>'),
  vault: I('<path d="M4 6a2 2 0 012-2h4l2 2h6a2 2 0 012 2v10a2 2 0 01-2 2H6a2 2 0 01-2-2V6z"/>'),
  speech: I('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/>'),
  appearance: I('<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 000 18z" fill="currentColor" stroke="none" opacity=".35"/>'),
  layout: I('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16M13 9h5M13 13h5"/>'),
  account: I('<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4-6 8-6s7 2 8 6"/>'),
  shortcuts: I('<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>'),
  "agent-tools": I('<path d="M14.7 6.3a4 4 0 00-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 005.4-5.4l-2.6 2.6-2.4-.6-.6-2.4 2.6-2.6z"/>'),
  permissions: I('<path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>'),
  hooks: I('<path d="M7 4v7a5 5 0 0010 0V9"/><path d="M14 6l3 3 3-3"/>'),
  users: I('<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.7-3.5 3.3-5.5 6.5-5.5s5.8 2 6.5 5.5M16 4.5a3.5 3.5 0 010 7M18 14.5c2 .7 3.2 2.5 3.5 5.5"/>'),
  system: I('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>'),
  logs: I('<path d="M6 3h9l5 5v13H6V3z"/><path d="M9 12h7M9 16h7M9 8h3"/>'),
  runs: I('<circle cx="5" cy="6" r="2"/><circle cx="5" cy="18" r="2"/><path d="M5 8v8"/><path d="M10 6h10M10 12h10M10 18h10"/>'),
  "sandbox-changes": I('<path d="M8 6l-5 6 5 6M16 6l5 6-5 6"/>'),
  "file-checkpoints": I('<path d="M3 12a9 9 0 109-9 9 9 0 00-6.4 2.6L3 8"/><path d="M3 3v5h5M12 8v4l3 2"/>'),
};

const SECTION_GROUPS = [
  {
    id: "models", label: "Models", sections: [
      { id: "add-models", label: "Add Models", render: renderAddModelsPanel,
        description: "Kairos ships with no default model. Add at least one, then pick it from the model menu above the chat box.",
        keywords: ["provider", "api key", "openai", "anthropic", "claude", "codex", "ollama", "local model", "endpoint", "base url", "openrouter", "gemini"] },
      { id: "added-models", label: "Added Models", render: renderAddedModelsPanel,
        description: "The model connections you have added. Test checks that each one answers.",
        keywords: ["manage", "remove model", "delete model", "test connection", "endpoints", "context"] },
    ],
  },
  {
    id: "connections", label: "Connections", sections: [
      { id: "integrations", label: "Integrations", render: renderIntegrationsPanel,
        description: "Tool servers, calendars, contacts and API services Kairos can use.",
        keywords: ["mcp", "connector", "tools", "caldav", "ical", "calendar feed", "google", "api service"] },
      { id: "channels", label: "Channels", render: renderChannelsPanel,
        description: "Reach Kairos and your agents from Discord, Telegram, Slack and other apps, and choose where task results and notifications go.",
        keywords: ["discord", "bot", "token", "telegram", "slack", "signal", "imessage", "email", "sms", "whatsapp", "matrix",
                   "mattermost", "irc", "line", "teams", "google chat", "ntfy", "webhook", "connector", "channel override", "announcements", "dm"] },
      { id: "remote", label: "Remote Access", render: renderRemotePanel,
        description: "Reach this Kairos from your phone or another computer over Tailscale, a private network between your own devices. Nothing is exposed to the public internet.",
        keywords: ["tailscale", "remote", "phone", "https", "certificate", "tunnel", "sign in", "account login", "url"] },
    ],
  },
  {
    id: "workspace", label: "Workspace", sections: [
      { id: "vault", label: "Vault", render: renderVaultPanel,
        description: "The notes folder Kairos reads and writes as its memory.",
        keywords: ["obsidian", "notes folder", "memory", "path", "sync", "location"] },
      { id: "speech", label: "Speech", render: renderSpeechPanel,
        description: "Dictate messages by voice. Audio is transcribed on this machine and never uploaded.",
        keywords: ["voice", "dictation", "microphone", "mic", "transcribe", "whisper", "speech to text", "open mic"] },
    ],
  },
  {
    id: "personal", label: "Personal", sections: [
      { id: "appearance", label: "Appearance", render: renderAppearancePanel,
        description: "A workspace that feels like yours. Saved for you on this device only.",
        keywords: ["theme", "background", "image", "color", "shader", "flow", "tint", "swirl", "grain", "motion"] },
      { id: "layout", label: "Layout", render: renderLayoutPanel,
        description: "The order of your sidebar and Home, and what they show. Saved for you on this device only.",
        keywords: ["sidebar", "tabs", "order", "reorder", "hide", "home", "panels", "arrange", "navigation"] },
      { id: "account", label: "Account", render: renderAccountPanel,
        description: "Your sign-in, password and two-factor authentication.",
        keywords: ["password", "2fa", "two factor", "totp", "authenticator", "username", "sign out", "security"] },
      { id: "shortcuts", label: "Shortcuts", render: renderShortcutsPanel,
        description: "Keys and commands that save a trip to the mouse.",
        keywords: ["keyboard", "hotkey", "command palette", "keys"] },
    ],
  },
  {
    id: "administration", label: "Administration", admin: true, sections: [
      { id: "agent-tools", label: "Agent Tools", render: renderAgentToolsPanel,
        description: "Tools every chat may use. Open chats pick up a change on their next message.",
        keywords: ["bash", "shell", "disabled tools", "allowed tools", "capabilities"] },
      { id: "permissions", label: "Permissions", render: renderPermissionsPanel,
        description: "What models may do without asking again. Anything not listed is asked for when it comes up.",
        keywords: ["permission", "allow", "always allow", "approval", "grant", "revoke", "prompt", "asked"] },
      { id: "hooks", label: "Hooks", render: renderHooksPanel,
        description: "Your own steps that run when something happens: post to a web address, add to a vault note, send to a channel, or run a command that can block a tool.",
        keywords: ["hook", "hooks", "lifecycle", "before tool", "after tool", "webhook", "block", "script", "automation", "event"] },
      { id: "users", label: "Users", render: renderUsersPanel,
        description: "Who can sign in to this Kairos, and who is an admin.",
        keywords: ["accounts", "add user", "roles", "admin", "people"] },
      { id: "system", label: "System", render: renderSystemPanel,
        description: "Health, backups, and permanent resets.",
        keywords: ["backup", "export", "import", "diagnostics", "health", "reset", "wipe", "danger"] },
      { id: "logs", label: "Logs", render: renderLogsPanel,
        description: "What Kairos has been doing. Errors keeps only warnings and errors; Desktop is the app window itself. Pick a chat to see only its turns.",
        keywords: ["log", "logs", "errors", "debug", "troubleshoot", "crash", "backend", "desktop", "warnings"] },
      { id: "runs", label: "Runs", render: renderRunsPanel,
        description: "Every model run: chat turns, tasks, cards, goals, helpers and agents. Open one to see what it did, step by step. Failed and stopped runs are one filter away.",
        keywords: ["run", "runs", "timeline", "steps", "failed", "stopped", "tools", "trace", "what happened"] },
      { id: "sandbox-changes", label: "Sandbox changes", render: renderSandboxChangesPanel,
        description: "Code changes a model made in the sandbox, waiting for you. Applying writes them only if none of those files changed since; it does not commit. Unapplied changes expire after 7 days.",
        keywords: ["sandbox", "run_code", "diff", "change set", "apply", "review", "patch", "code changes"] },
      { id: "file-checkpoints", label: "File checkpoints", render: renderFileCheckpointsPanel,
        description: "Each model turn saves the Vault and its working folder before and after. Review the files, then restore the ones you choose. A file changed again cannot be restored from an older checkpoint; large skipped files were not protected.",
        keywords: ["checkpoint", "rollback", "restore", "undo", "files", "vault", "history"] },
    ],
  },
];

let modalEl = null;
let pillEl = null;
let activeSectionId = "add-models";
let cachedStatus = null;
let renderVersion = 0;
// The phone layout (renderMobilePage) while it is on screen: its top bar, the
// way out, and the current sub-page's way back. null on desktop.
let mobile = null;

export async function render(container) {
  // Settings has no real "page" anymore — clicking its nav item opens the
  // floating window over whatever tab was active. Leave the tab body empty
  // rather than a jarring blank "Loading..." that never resolves.
  container.innerHTML = "";
  await openSettingsWindow();
}

export async function openSettingsWindow(section) {
  mobile = null;
  if (section === "integrations") activeSectionId = section;
  if (pillEl) { pillEl.remove(); pillEl = null; }
  cachedStatus = await api("/api/auth/status");
  const modal = getModal();
  modal.classList.remove("hidden");
  hidePageBehind();
  const panel = modal.querySelector(".settings-window");
  pinInitialRect(panel);
  // The window keeps its position between opens, so the viewport may have
  // changed since it was last on screen.
  clampToViewport(panel);
  buildNav();
  await selectSection(activeSectionId);
}

const SETTINGS_TITLE = () => el("div", { class: "title" }, [
  el("span", { class: "settings-title-icon" }), el("span", { text: "Settings" })]);

function titleWithIcon() {
  const title = SETTINGS_TITLE();
  title.firstChild.innerHTML = I('<circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.3-2l2-1.5-2-3.4-2.3.9a7 7 0 00-1.7-1L14 2h-4l-.7 2.6a7 7 0 00-1.7 1l-2.3-.9-2 3.4L5.3 10a7 7 0 000 4l-2 1.5 2 3.4 2.3-.9a7 7 0 001.7 1L10 22h4l.7-2.6a7 7 0 001.7-1l2.3.9 2-3.4-2-1.5c.2-.6.3-1.3.3-2z"/>');
  return title;
}

// Settings as a full-screen page on phones, not a popup (David's ask
// 2026-09-01); app.js calls this instead of openSettingsWindow() below the
// responsive breakpoint. Rebuilt 2026-10-05 after the iOS Settings and Claude
// mobile pattern: a list of sections first (search, grouped rows with icons),
// and each page full screen under a top bar whose back button goes up one
// level - sub-page to page, page to the list, the list to the tab you came
// from (`onExit`). Same nav and pages as desktop (found by id).
export async function renderMobilePage(container, section, onExit) {
  if (pillEl) { pillEl.remove(); pillEl = null; }
  // Only one of the floating modal and this page can exist at a time: both
  // use the #settings-nav/#settings-content ids.
  if (modalEl) { modalEl.remove(); modalEl = null; }
  cachedStatus = await api("/api/auth/status");
  container.innerHTML = "";

  const backBtn = el("button", { class: "settings-titlebar-btn settings-mobile-back", title: "Back", "aria-label": "Back", text: "‹" });
  const barTitle = el("span", { class: "settings-mobile-title", text: "Settings" });
  const titlebar = el("div", { class: "settings-titlebar" }, [el("div", { class: "title" }, [backBtn, barTitle])]);
  const nav = el("div", { class: "settings-nav", id: "settings-nav" });
  const content = el("div", { class: "settings-content", id: "settings-content" });
  const body = el("div", { class: "settings-body" }, [nav, content]);
  container.append(titlebar, body);

  mobile = {
    container, barTitle, subBack: null,
    showList() {
      renderVersion++; // a page still loading must not take the screen back
      container.dataset.mobileView = "list";
      barTitle.textContent = "Settings";
      this.subBack = null;
      content.replaceChildren();
    },
    showPage(title) { container.dataset.mobileView = "page"; barTitle.textContent = title; },
  };
  backBtn.addEventListener("click", () => {
    if (container.dataset.mobileView !== "page") { mobile = null; onExit?.(); return; }
    if (mobile.subBack) mobile.subBack();
    else mobile.showList();
  });

  buildNav();
  if (section) await selectSection(section);
  else mobile.showList();
}

// Closing leaves you on the tab you had open (David, 2026-10-05: it used to
// jump to Home). The window floats over that tab, so hiding it is enough.
function closeSettingsWindow() {
  if (modalEl) modalEl.classList.add("hidden");
  restorePageBehind();
}

function minimizeSettingsWindow() {
  if (modalEl) modalEl.classList.add("hidden");
  // Minimized leaves only a small pill on screen, so the page behind is
  // meant to be visible again — same as closing, from the pane's side.
  restorePageBehind();
  if (pillEl) return;
  pillEl = el("div", { class: "glass bracket settings-minimized-pill", onclick: () => { pillEl.remove(); pillEl = null; modalEl.classList.remove("hidden"); hidePageBehind(); } }, [
    el("span", { text: "⚙" }),
    el("span", { text: "Settings" }),
  ]);
  document.body.appendChild(pillEl);
}

function getModal() {
  if (modalEl) return modalEl;

  const closeBtn = el("button", { class: "settings-titlebar-btn", title: "Close", onclick: closeSettingsWindow, text: "✕" });
  const minBtn = el("button", { class: "settings-titlebar-btn", title: "Minimize", onclick: minimizeSettingsWindow, text: "–" });
  const titlebar = el("div", { class: "settings-titlebar" }, [
    titleWithIcon(),
    el("div", { class: "settings-titlebar-actions" }, [minBtn, closeBtn]),
  ]);

  const nav = el("div", { class: "settings-nav", id: "settings-nav" });
  const content = el("div", { class: "settings-content", id: "settings-content" });
  const body = el("div", { class: "settings-body" }, [nav, content]);

  const panel = el("div", { class: "glass settings-window" }, [titlebar, body]);
  const backdrop = el("div", { class: "modal-backdrop hidden" }, [panel]);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeSettingsWindow(); });
  panel.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(backdrop);
  attachResizeHandles(panel);
  attachTitlebarDrag(panel, titlebar);
  // Shrinking the app window, restoring it from maximised, or crossing the
  // responsive breakpoint can all leave the window partly or wholly outside
  // the viewport. Re-clamping keeps it reachable — losing the titlebar off
  // screen would be unrecoverable, since dragging is the only way back.
  window.addEventListener("resize", () => clampToViewport(panel));
  // Escape closes it too, unless a confirmation or an open menu inside it
  // should get the key first.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || backdrop.classList.contains("hidden")) return;
    if (document.querySelector(".confirm-panel, dialog[open]")) return;
    if ([...document.querySelectorAll(".custom-select-menu, .overflow-menu")].some((m) => !m.classList.contains("hidden"))) return;
    closeSettingsWindow();
  });

  modalEl = backdrop;
  return modalEl;
}

// Resizable from any edge/corner (David's ask 2026-09-01). The window starts
// at its CSS-centered size, then is pinned to that exact rect with
// `position: fixed` so each handle moves only its own edge. Mouse-only: the
// mobile shell is a separate full page.
//
// The initial rect must be captured while the window is visible (found live
// 2026-09-01: captured while hidden it pinned the window at 0x0), so
// attachResizeHandles() runs at construction and pinInitialRect() after the
// `hidden` class is removed.
const MIN_WIDTH = 480;
const MIN_HEIGHT = 360;
function pinInitialRect(panel) {
  if (panel.dataset.pinned) return; // only the very first real open
  panel.dataset.pinned = "1";
  const rect = panel.getBoundingClientRect();
  panel.style.position = "fixed";
  panel.style.left = `${rect.left}px`;
  panel.style.top = `${rect.top}px`;
  panel.style.width = `${rect.width}px`;
  panel.style.height = `${rect.height}px`;
  panel.style.maxWidth = "none";
  panel.style.maxHeight = "none";
}
// Keeps the window wholly inside the viewport (David's ask 2026-09-15). Used
// by the drag handler, the window-resize listener and after the breakpoint
// changes; a titlebar stranded off screen is unrecoverable. Shrinking comes
// before repositioning so an oversized window ends up fully visible.
function clampToViewport(panel) {
  if (!panel || !panel.dataset.pinned) return;
  const maxWidth = Math.max(MIN_WIDTH, window.innerWidth);
  const maxHeight = Math.max(MIN_HEIGHT, window.innerHeight);
  const width = Math.min(panel.offsetWidth, maxWidth);
  const height = Math.min(panel.offsetHeight, maxHeight);
  panel.style.width = `${width}px`;
  panel.style.height = `${height}px`;
  panel.style.left = `${Math.max(0, Math.min(parseFloat(panel.style.left) || 0, window.innerWidth - width))}px`;
  // Never under the desktop title bar, which sits above everything.
  const top = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--titlebar-h")) || 0;
  panel.style.top = `${Math.max(top, Math.min(parseFloat(panel.style.top) || 0, window.innerHeight - height))}px`;
}

function attachTitlebarDrag(panel, titlebar) {
  titlebar.addEventListener("mousedown", (e) => {
    // Only a plain left-press on the bar itself, so pressing minimize or
    // close never starts a drag.
    if (e.button !== 0 || e.target.closest("button")) return;
    if (!panel.dataset.pinned) return;
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const startLeft = parseFloat(panel.style.left) || 0;
    const startTop = parseFloat(panel.style.top) || 0;
    panel.classList.add("is-dragging");

    function onMove(ev) {
      const width = panel.offsetWidth;
      const height = panel.offsetHeight;
      panel.style.left = `${Math.max(0, Math.min(startLeft + ev.clientX - startX, window.innerWidth - width))}px`;
      panel.style.top = `${Math.max(0, Math.min(startTop + ev.clientY - startY, window.innerHeight - height))}px`;
    }
    function onUp() {
      panel.classList.remove("is-dragging");
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

function attachResizeHandles(panel) {
  for (const dir of ["n", "s", "e", "w", "ne", "nw", "se", "sw"]) {
    const handle = el("div", { class: `resize-handle resize-${dir}` });
    panel.appendChild(handle);
    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const startX = e.clientX;
      const startY = e.clientY;
      const startRect = panel.getBoundingClientRect();

      function onMove(ev) {
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;
        if (dir.includes("e")) panel.style.width = `${Math.max(MIN_WIDTH, startRect.width + dx)}px`;
        if (dir.includes("s")) panel.style.height = `${Math.max(MIN_HEIGHT, startRect.height + dy)}px`;
        if (dir.includes("w")) {
          const newWidth = Math.max(MIN_WIDTH, startRect.width - dx);
          panel.style.width = `${newWidth}px`;
          panel.style.left = `${startRect.left + (startRect.width - newWidth)}px`;
        }
        if (dir.includes("n")) {
          const newHeight = Math.max(MIN_HEIGHT, startRect.height - dy);
          panel.style.height = `${newHeight}px`;
          panel.style.top = `${startRect.top + (startRect.height - newHeight)}px`;
        }
      }
      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        // Dragging an edge outward can push the opposite edge past the
        // viewport; settle it back inside once the drag ends.
        clampToViewport(panel);
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }
}

// Which sections this user can actually reach. One place, so the nav, the
// search, and selectSection() can never disagree about it.
function visibleSections() {
  return SECTION_GROUPS
    .filter((group) => !group.admin || cachedStatus.is_admin)
    .filter((group) => group.sections.length);
}

function buildNav() {
  const nav = document.getElementById("settings-nav");
  nav.innerHTML = "";
  const search = el("input", { class: "settings-search", type: "search", placeholder: "Search settings", "aria-label": "Find settings" });
  const navList = el("div", { class: "settings-nav-list" });
  nav.append(search, navList);

  const entries = [];
  for (const group of visibleSections()) {
    const heading = el("div", { class: "settings-nav-group", text: group.label });
    const block = el("div", { class: "settings-nav-block" });
    navList.append(heading, block);
    const items = [];
    for (const sec of group.sections) {
      const icon = el("span", { class: "settings-nav-icon", "aria-hidden": "true" });
      icon.innerHTML = NAV_ICONS[sec.id] || "";
      const item = el("button", {
        type: "button",
        class: "settings-nav-item" + (sec.id === activeSectionId ? " active" : ""),
        onclick: () => selectSection(sec.id),
      }, [icon, el("span", { text: sec.label })]);
      // data-section rather than matching on text: two groups could hold
      // panels with the same label.
      item.dataset.section = sec.id;
      items.push({ item, sec });
      entries.push({ item, sec });
      block.appendChild(item);
    }
    entries.push({ heading, block, items });
  }

  const matches = (sec, q) => !q
    || sec.label.toLowerCase().includes(q)
    || (sec.keywords || []).some((word) => word.includes(q));

  search.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    for (const entry of entries) {
      if (entry.sec) entry.item.hidden = !matches(entry.sec, q);
      // A group heading with nothing under it is noise, so it hides with
      // its children rather than leaving a stray label behind.
      else entry.heading.hidden = entry.block.hidden = !entry.items.some(({ sec }) => matches(sec, q));
    }
  });
}

// Every page gets the same frame: a header (title, description, actions) and
// a body the page renders into. `page` lets a page add header actions or show
// a sub-page with its own title and a way back.
async function selectSection(id) {
  const groups = visibleSections();
  const reachable = groups.flatMap((group) => group.sections);
  const section = reachable.find((s) => s.id === id) || reachable[0];
  if (!section) return;
  activeSectionId = section.id;
  const mine = ++renderVersion;
  document.querySelectorAll(".settings-nav-item").forEach((n) => n.classList.toggle("active", n.dataset.section === section.id));
  const content = document.getElementById("settings-content");
  const headerSlot = el("div", { class: "set-header-slot" });
  const body = el("div", { class: "set-body" });
  content.replaceChildren(el("div", { class: "set-page", "data-page": section.id }, [headerSlot, body]));
  content.scrollTop = 0;
  const page = {
    actions(nodes = []) {
      headerSlot.replaceChildren(pageHeader(section.label, section.description, nodes));
      if (mobile && mine === renderVersion) { mobile.subBack = null; mobile.showPage(section.label); }
    },
    sub({ title, description, actions = [], back }) {
      headerSlot.replaceChildren(
        el("button", { type: "button", class: "btn quiet set-back", text: `← ${section.label}`, onclick: back }),
        pageHeader(title, description, actions));
      content.scrollTop = 0;
      if (mobile && mine === renderVersion) { mobile.subBack = back; mobile.showPage(title); }
    },
    get current() { return mine === renderVersion; },
  };
  page.actions();
  await section.render(body, cachedStatus, page);
}

// -- Add Models (moved from Cookbook, David's ask 2026-08-31). Kairos ships
// with no default model, so Claude itself has to be added here like anything
// else, not assumed. ---------------------------------------------------------

// Known API providers (mirrors Odysseus's provider list). Only providers that
// work with a plain bearer-token key over an OpenAI-compatible endpoint;
// device-auth flows Kairos does not have are left out, not faked.
const KNOWN_API_PROVIDERS = [
  { label: "OpenAI", base_url: "https://api.openai.com/v1" },
  { label: "Anthropic", base_url: "https://api.anthropic.com" },
  { label: "OpenRouter", base_url: "https://openrouter.ai/api/v1" },
  { label: "DeepSeek", base_url: "https://api.deepseek.com/v1" },
  { label: "Groq", base_url: "https://api.groq.com/openai/v1" },
  { label: "Mistral", base_url: "https://api.mistral.ai/v1" },
  { label: "Together AI", base_url: "https://api.together.xyz/v1" },
  { label: "Fireworks AI", base_url: "https://api.fireworks.ai/inference/v1" },
  { label: "Google Gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai" },
  { label: "xAI Grok", base_url: "https://api.x.ai/v1" },
  { label: "Z.AI", base_url: "https://api.z.ai/api/paas/v4" },
  { label: "NVIDIA", base_url: "https://integrate.api.nvidia.com/v1" },
  { label: "Ollama Cloud", base_url: "https://ollama.com/api" },
];

function modelForm(title, subtitle, kind, onAdded) {
  const isCliKind = kind === "claude_cli" || kind === "codex_cli";
  const nameInput = el("input", { placeholder: kind === "local" ? "Local Ollama" : kind === "claude_cli" ? "Claude" : kind === "codex_cli" ? "Codex" : "OpenRouter" });
  const urlInput = el("input", { placeholder: kind === "local" ? "http://localhost:11434/v1" : "https://api.openrouter.ai/v1" });
  const modelInput = el("input", { placeholder: isCliKind ? (kind === "claude_cli" ? "claude-sonnet-4-5" : "gpt-5-codex") : "Model id" });
  const keyInput = el("input", { type: "password", placeholder: kind === "local" ? "Optional" : "API key", autocomplete: "off" });
  // Context window cap (David's ask 2026-09-01, real incident: a local model
  // loaded with no cap took its max context and ~21GB of RAM). Local only:
  // sent as Ollama's `options.num_ctx`; other servers ignore it.
  const ctxInput = el("input", { type: "number", placeholder: "4096" });
  const images = toggle({ label: "Accepts images" });
  const err = el("div", { class: "set-error" });
  const addBtn = el("button", { class: "btn primary", text: "Add" });

  // Picking a known provider fills in its name and base URL, so only the
  // model id and key are left. "Custom URL" leaves both free.
  let providerSelect = null;
  if (kind === "api") {
    providerSelect = customSelect({}, [
      el("option", { value: "" }, "Custom URL"),
      ...KNOWN_API_PROVIDERS.map((p) => el("option", { value: p.base_url }, p.label)),
    ]);
    providerSelect.addEventListener("change", () => {
      const chosen = KNOWN_API_PROVIDERS.find((p) => p.base_url === providerSelect.value);
      if (chosen) {
        urlInput.value = chosen.base_url;
        if (!nameInput.value.trim()) nameInput.value = chosen.label;
      }
    });
  }

  addBtn.addEventListener("click", async () => {
    err.textContent = "";
    if (isCliKind) {
      if (!nameInput.value.trim()) { err.textContent = "Name is required."; return; }
    } else if (!nameInput.value.trim() || !urlInput.value.trim() || !modelInput.value.trim()) {
      err.textContent = "Name, URL, and model id are required.";
      return;
    }
    try {
      await api("/api/models", {
        method: "POST",
        body: JSON.stringify({
          name: nameInput.value.trim(),
          base_url: urlInput.value.trim(),
          model: modelInput.value.trim(),
          api_key: keyInput.value.trim() || null,
          kind,
          num_ctx: kind === "local" && ctxInput.value.trim() ? parseInt(ctxInput.value.trim(), 10) : null,
          supports_images: !isCliKind && images.checked,
        }),
      });
      nameInput.value = ""; urlInput.value = ""; modelInput.value = ""; keyInput.value = ""; ctxInput.value = ""; images.checked = false;
      if (providerSelect) providerSelect.value = "";
      onAdded();
    } catch (e) { err.textContent = errorText(e); }
  });

  const rows = kind === "local"
    ? [field("Name", nameInput), field("Server URL", urlInput), field("Model id", modelInput), field("Context window", ctxInput, "Tokens the model may hold. Keeps a large model from filling your memory.")]
    : isCliKind
      ? [field("Name", nameInput), field("Model", modelInput, "Optional. Leave empty for the CLI's own default.")]
      : [field("Provider", providerSelect), field("Name", nameInput), field("Base URL", urlInput), field("Model id", modelInput), field("API key", keyInput)];
  if (!isCliKind) rows.push(row({ title: "Accepts images", description: "The model can read pictures you attach.", control: images }));
  rows.push(el("div", { class: "set-row-actions" }, [err, addBtn]));
  return group({ title, description: subtitle }, rows);
}

function renderAddModelsPanel(body) {
  const done = () => selectSection("added-models");
  body.replaceChildren(
    modelForm("Claude Code CLI", "Uses the claude CLI already installed and signed in on this machine. No key needed.", "claude_cli", done),
    modelForm("Codex CLI", "Uses the codex CLI already installed and signed in on this machine. No key needed.", "codex_cli", done),
    modelForm("Local model server", "Ollama, llama.cpp, vLLM or any server on this machine or your network.", "local", done),
    modelForm("API provider", "A cloud provider such as OpenAI, Anthropic or OpenRouter.", "api", done),
  );
}

// -- Added Models -------------------------------------------------------
async function renderAddedModelsPanel(body, _status, page) {
  const endpoints = await api("/api/models");
  const tests = [];
  page.actions([el("button", { class: "btn", text: "Test all", onclick: () => tests.forEach((run) => run()) })]);

  function endpointRow(ep) {
    const result = el("span", { class: "set-row-result" });
    const runTest = async () => {
      result.replaceChildren(pill("Testing…", "muted"));
      const res = await api(`/api/models/${ep.id}/test`, { method: "POST" });
      result.replaceChildren(res.ok ? pill(`OK · ${res.latency_ms}ms`, "ok") : pill("Failed", "error"));
      result.title = res.ok ? "" : res.detail || "";
    };
    tests.push(runTest);
    const remove = el("button", { class: "btn quiet danger", text: "Remove", onclick: async () => {
      const ok = await confirmDialog({
        title: "Remove this model?",
        message: `"${ep.name}" will be removed. Any chat still set to it will need a new model chosen.`,
        confirmLabel: "Remove model",
      });
      if (!ok) return;
      await api(`/api/models/${ep.id}`, { method: "DELETE" });
      toast("Model removed", "success");
      await selectSection("added-models");
    } });
    const meta = ep.kind === "claude_cli" || ep.kind === "codex_cli"
      ? `Model: ${ep.model || "CLI default"}`
      : [ep.model, ep.base_url, ep.has_api_key ? "key saved" : null, ep.kind === "local" && ep.num_ctx ? `context ${ep.num_ctx}` : null].filter(Boolean).join(" · ");
    const controls = [result];
    // Kairos's own instructions and core tools take about 2,100 tokens of
    // every request (2026-10-06, vault note "Local Model Fit (Build Spec)").
    if (ep.kind === "local" && ep.num_ctx && ep.num_ctx < 8192) {
      const small = pill("Small window", "warn");
      small.title = `Kairos's own instructions and core tools take about 2,100 of these ${ep.num_ctx} tokens on every message, leaving little for the conversation. 16,384 is the default for new local models.`;
      small.classList.add("small-window");
      controls.push(small);
    }
    if (ep.kind === "local" || ep.kind === "api") {
      controls.push(el("label", { class: "set-inline-switch" }, [el("span", { class: "meta", text: "Images" }),
        toggle({ checked: ep.supports_images, label: `${ep.name} accepts images`, onChange: async (on) => {
          const updated = await api(`/api/models/${ep.id}/image-support`, { method: "PATCH", body: JSON.stringify({ enabled: on }) });
          ep.supports_images = updated.supports_images;
        } })]));
    }
    controls.push(el("button", { class: "btn", text: "Test", onclick: runTest }), remove);
    return row({ title: [modelMark(ep.mark, ep.name), el("span", { text: ep.name })].filter(Boolean), description: meta, control: controls,
      cls: "model-row", attrs: { "data-endpoint": ep.id } });
  }

  const kinds = [["claude_cli", "Claude Code CLI"], ["codex_cli", "Codex CLI"], ["local", "Local"], ["api", "API"]];
  const settings = await api("/api/settings").catch(() => ({}));
  // Roadmap phase 3 (2026-10-05): local and API models have small windows
  // and never compact themselves (services/chat_service.py).
  const longChats = group({ title: "Long chats", cls: "long-chats" }, [row({
    title: "Compact local and API chats automatically",
    description: "When a chat fills 85% of the model's context window, Kairos summarises its earlier part before your next message, as Compact does: the last messages stay word for word and nothing is deleted. Claude Code and Codex manage their own.",
    control: toggle({ checked: settings.auto_compact !== false, label: "Compact local and API chats automatically",
      onChange: (on) => api("/api/settings/auto-compact", { method: "POST", body: JSON.stringify({ enabled: on }) }) }),
  })]);
  // Helpers (roadmap phase 5, 2026-10-06; core/helpers.py): the model the
  // delegate tool's read-only helpers run on. Never Claude or Codex, whose own
  // tools can't be limited to reading.
  const firstLocal = endpoints.find((e) => e.kind === "local");
  const helperSelect = customSelect({}, [
    el("option", { value: "", text: `Automatic: ${firstLocal ? firstLocal.name : "the first local model (none added)"}` }),
    ...endpoints.filter((e) => e.kind === "local" || e.kind === "api").map((e) => el("option", { value: e.id, text: e.name })),
    el("option", { value: "off", text: "Off" }),
  ]);
  helperSelect.value = settings.helper_endpoint_id || "";
  helperSelect.addEventListener("change", async () => {
    try {
      await api("/api/settings/helpers", { method: "POST", body: JSON.stringify({ endpoint_id: helperSelect.value || null }) });
      toast("Helpers updated", "success");
    } catch (problem) { toast(problem.message, "error"); }
  });
  const helpers = group({ title: "Helpers", cls: "helpers-settings" }, [row({
    title: "Model for helpers",
    description: "Any chat or task can hand up to 3 research jobs to helpers that work in parallel and report back. They run on this model, can only read (notes, past chats, documents, the web) and stop after 10 minutes or 40,000 tokens each. Claude and Codex chats can use them too; their own built-in subagents are separate.",
    control: helperSelect,
  })]);
  body.replaceChildren(...kinds.map(([kind, title]) => {
    const list = endpoints.filter((e) => e.kind === kind);
    return group({ title }, list.length ? list.map(endpointRow) : [empty("None added")]);
  }), longChats, helpers);
  if (!endpoints.length) body.prepend(note(["Nothing added yet. ", el("button", { type: "button", class: "btn quiet", text: "Add a model", onclick: () => selectSection("add-models") })]));
}

// -- Integrations (David's ask, 2026-08-31, after Odysseus's "Add
// Integration" menu). CalDAV/CardDAV are a real one-way read sync
// (core/dav_client.py); Email reuses the Email tab's own account management
// rather than duplicating it. -----------------------------------------------
const INTEGRATION_KIND_LABELS = {
  api_service: "API service",
  mcp_server: "MCP tool server",
  caldav_calendar: "CalDAV calendar",
  carddav_contacts: "Contacts (CardDAV)",
  ical_feed: "iCal feed",
};

// Generic per-kind glyphs: brand logos would mean faking icons for services
// we don't actually connect to, or fetching them.
const CONNECTOR_ICONS = {
  api_service: I('<path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/>'),
  mcp_server: I('<path d="M9 2v6M15 2v6M12 17v5"/><path d="M6 8h12a2 2 0 012 2v2a6 6 0 01-6 6h-4a6 6 0 01-6-6v-2a2 2 0 012-2z"/>'),
  caldav_calendar: I('<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>'),
  ical_feed: I('<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18M8 15h.01M12 15h.01M16 15h.01"/>'),
  carddav_contacts: I('<path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/><circle cx="12" cy="7" r="4"/>'),
};

function connectorIsConnected(item) {
  if (item.kind === "api_service") return item.has_api_key || !!item.base_url;
  if (item.kind === "mcp_server") return item.auth === "oauth" ? item.signed_in : true; // sign-in servers count once signed in
  return item.last_synced_count != null; // dav/ical: connected once it has synced at least once
}

let connectorsFilter = "all";
let connectorsQuery = "";

async function renderIntegrationsPanel(body, status, page) {
  const items = await api("/api/integrations");
  const rerender = () => renderIntegrationsPanel(body, status, page);
  const formHost = el("div", { class: "integration-form-host" });

  const addBtn = el("button", { class: "btn primary", text: "Add integration" });
  const menu = el("div", { class: "overflow-menu below hidden", style: "min-width:200px;right:0;left:auto;" });
  const menuItem = (text, onclick) => el("button", { type: "button", class: "overflow-menu-item", text,
    onclick: () => { menu.classList.add("hidden"); onclick(); } });
  menu.append(
    menuItem("API service", () => renderApiServiceForm(formHost, rerender)),
    menuItem("CalDAV calendar", () => renderDavForm(formHost, rerender, "caldav_calendar")),
    menuItem("iCal feed", () => renderIcalForm(formHost, rerender)),
    menuItem("Contacts (CardDAV)", () => renderDavForm(formHost, rerender, "carddav_contacts")),
    menuItem("Email (IMAP/SMTP)", () => {
      // Email has full account management as its own tab; jump there.
      closeSettingsWindow();
      document.querySelector('.nav-item[data-tab="email"]')?.click();
    }),
    menuItem("MCP tool server", () => renderMcpServerForm(formHost, rerender)),
  );
  addBtn.addEventListener("click", (e) => { e.stopPropagation(); menu.classList.toggle("hidden"); });
  document.addEventListener("click", () => menu.classList.add("hidden"));
  page.actions([el("div", { style: "position:relative;" }, [addBtn, menu])]);

  const parts = [formHost];

  // Popular: only GitHub. Its remote MCP server's automatic sign-in fails
  // (no dynamic client registration, confirmed live), but GitHub documents a
  // personal-access-token fallback that works with the bearer-token field.
  // Services with no such fallback are not offered as fake "Connect" buttons.
  const githubAdded = items.some((i) => i.kind === "mcp_server" && i.url === "https://api.githubcopilot.com/mcp/");
  if (!githubAdded) {
    const pat = el("input", { type: "password", placeholder: "GitHub personal access token", autocomplete: "off" });
    pat.hidden = true;
    const err = el("div", { class: "set-error" });
    const connect = el("button", { class: "btn", text: "Connect" });
    connect.addEventListener("click", async () => {
      if (pat.hidden) { pat.hidden = false; pat.focus(); connect.textContent = "Save"; return; }
      if (!pat.value.trim()) { err.textContent = "A token is required."; return; }
      try {
        await api("/api/integrations/mcp-server", { method: "POST",
          body: JSON.stringify({ name: "GitHub", mcp_type: "http", url: "https://api.githubcopilot.com/mcp/", api_key: pat.value.trim() }) });
        await rerender();
      } catch (e) { err.textContent = errorText(e); }
    });
    const icon = el("span", { class: "set-icon" }); icon.innerHTML = CONNECTOR_ICONS.mcp_server;
    parts.push(group({ title: "Popular" }, [row({ icon, title: "GitHub",
      description: "GitHub's remote MCP server. Needs a personal access token: GitHub Settings, Developer settings, Personal access tokens.",
      control: [pat, connect], below: err })]));
  }

  // Search plus All / Connected / Not connected, after Claude's Connectors.
  const search = el("input", { class: "connectors-search", type: "search", placeholder: "Search integrations", value: connectorsQuery });
  const tabs = el("div", { class: "segmented-tabs" });
  for (const [id, label] of [["all", "All"], ["connected", "Connected"], ["not_connected", "Not connected"]]) {
    tabs.append(el("button", { type: "button", class: "segmented-tab" + (connectorsFilter === id ? " active" : ""), text: label,
      onclick: () => { connectorsFilter = id; rerender(); } }));
  }
  search.addEventListener("input", () => { connectorsQuery = search.value; rerender(); });

  const filtered = items.filter((item) => {
    if (connectorsQuery && !item.name.toLowerCase().includes(connectorsQuery.toLowerCase())) return false;
    const connected = connectorIsConnected(item);
    if (connectorsFilter === "connected" && !connected) return false;
    if (connectorsFilter === "not_connected" && connected) return false;
    return true;
  });

  const rows = filtered.map((item) => {
    const connected = connectorIsConnected(item);
    const icon = el("span", { class: "set-icon" }); icon.innerHTML = CONNECTOR_ICONS[item.kind] || "";
    const actions = [];
    if (item.kind === "mcp_server" && item.auth === "oauth") {
      const host = el("span", { class: "set-row-control" });
      host.append(el("button", { class: "btn", text: item.signed_in ? "Sign in again" : "Sign in",
        onclick: () => signInToMcp(item.id, item.name, rerender, host) }));
      if (item.signed_in) {
        host.append(el("button", { class: "btn", text: "Sign out", onclick: async () => {
          await api(`/api/integrations/${item.id}/oauth`, { method: "DELETE" });
          toast(`Signed out of ${item.name}`, "success");
          await rerender();
        } }));
      }
      actions.push(host);
    }
    if (["caldav_calendar", "carddav_contacts", "ical_feed"].includes(item.kind)) {
      const sync = el("button", { class: "btn", text: "Sync now" });
      sync.addEventListener("click", async () => {
        sync.textContent = "Syncing…";
        try {
          await api(`/api/integrations/${item.id}/sync`, { method: "POST" });
          toast(`${item.name} synced`, "success");
        } catch (e) { toast(errorText(e), "error"); }
        await rerender();
      });
      actions.push(sync);
    }
    actions.push(el("button", { class: "btn quiet danger", text: "Remove", onclick: async () => {
      const ok = await confirmDialog({
        title: "Remove this integration?",
        message: `"${item.name}" will be disconnected and its stored credentials deleted.`,
        confirmLabel: "Remove integration",
      });
      if (!ok) return;
      await api(`/api/integrations/${item.id}`, { method: "DELETE" });
      toast("Integration removed", "success");
      await rerender();
    } }));
    return row({ icon, title: item.name, description: INTEGRATION_KIND_LABELS[item.kind],
      control: [connected ? pill(item.auth === "oauth" ? "Signed in" : "Connected", "ok")
        : pill(item.auth === "oauth" ? "Needs sign-in" : "Not connected", "muted"), ...actions],
      cls: "integration-row", attrs: { "data-integration": item.name } });
  });
  parts.push(group({ title: "Your integrations", cls: "integrations-list" }, [
    el("div", { class: "set-toolbar set-group-toolbar" }, [search, tabs]),
    ...(items.length === 0 ? [empty("No integrations yet. Add one, or pick a server from the catalog below.")]
      : filtered.length === 0 ? [empty("No integrations match.")] : rows),
  ]));

  const contacts = items.length ? await api("/api/integrations/contacts") : [];
  if (contacts.length) {
    parts.push(group({ title: "Synced contacts", description: "There is no Contacts tab yet, so they are listed here." },
      contacts.map((c) => row({ title: c.name, description: [c.email, c.phone].filter(Boolean).join(" · ") }))));
  }
  parts.push(await mcpCatalogSection(rerender));
  if (!page.current) return;
  body.replaceChildren(...parts);
}

// OAuth sign-in to an MCP server (core/mcp_oauth.py). The provider's page
// opens in a browser, the provider sends that browser back to this backend,
// and this polls until the sign-in lands, fails, or times out. The link stays
// in the row in case no window appeared.
async function signInToMcp(itemId, name, rerender, host) {
  let started;
  try {
    started = await api(`/api/integrations/${itemId}/oauth/start`, { method: "POST" });
  } catch (problem) { toast(errorText(problem), "error"); return; }
  if (started.signed_in) {
    toast(`Already signed in to ${name}`, "success");
    await rerender();
    return;
  }
  window.open(started.url, "_blank", "noopener");
  host.replaceChildren(el("span", { class: "meta", text: "Waiting for sign-in…" }),
    el("a", { class: "meta mcp-signin-link", href: started.url, target: "_blank", rel: "noopener", text: "Open the sign-in page" }));
  const deadline = Date.now() + 5 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    if (!host.isConnected) return; // the page was left or redrawn
    let state;
    try { state = await api(`/api/integrations/${itemId}/oauth`); } catch { continue; }
    if (state.signed_in) { toast(`Signed in to ${name}`, "success"); break; }
    if (!state.pending) { toast(state.error ? `Sign-in failed: ${state.error}` : "Sign-in did not finish", "error"); break; }
  }
  await rerender();
}

// The MCP catalog (Hermes track 2026-09-23): known servers from
// core/mcp_catalog.json. Those needing no sign-in add in one step; the rest
// are added as sign-in servers and go straight to signing in.
async function mcpCatalogSection(rerender) {
  const catalog = await api("/api/integrations/catalog");
  const details = el("details", { class: "disclosure-panel mcp-catalog" });
  const search = el("input", { type: "search", class: "mcp-catalog-search", placeholder: "Search servers" });
  const list = el("div", { class: "mcp-catalog-list" });
  const draw = () => {
    const q = search.value.trim().toLowerCase();
    const shown = catalog
      .filter((s) => !q || `${s.name} ${s.description}`.toLowerCase().includes(q))
      .sort((a, b) => (a.auth === "none" ? 0 : 1) - (b.auth === "none" ? 0 : 1) || a.name.localeCompare(b.name));
    list.replaceChildren(...shown.map((server) => {
      let action;
      if (server.added) action = pill("Added", "ok");
      else if (server.auth === "none") {
        action = el("button", { class: "btn", text: "Add", onclick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try {
            await api("/api/integrations/mcp-server", { method: "POST",
              body: JSON.stringify({ name: server.name, mcp_type: "http", url: server.url }) });
            toast(`${server.name} added`, "success");
            await rerender();
          } catch (problem) { toast(problem.message, "error"); button.disabled = false; }
        } });
      } else {
        action = el("span", { class: "set-row-control" });
        action.append(el("button", { class: "btn", text: "Add and sign in", onclick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          let item;
          try {
            item = await api("/api/integrations/mcp-server", { method: "POST",
              body: JSON.stringify({ name: server.name, mcp_type: "http", url: server.url, auth: "oauth" }) });
          } catch (problem) { toast(problem.message, "error"); button.disabled = false; return; }
          await signInToMcp(item.id, server.name, rerender, action);
        } }));
      }
      return row({ title: server.name, control: action, cls: "mcp-catalog-row", attrs: { "data-server": server.id },
        description: [server.description, server.docs ? el("a", { class: "set-link", href: server.docs, target: "_blank", rel: "noopener", text: " Documentation" }) : null] });
    }));
    if (!shown.length) list.append(empty("No server matches."));
  };
  search.addEventListener("input", draw);
  details.append(
    el("summary", { text: `Browse the MCP catalog (${catalog.length} servers)` }),
    note("Tools from these servers are available to every model: Claude directly, local and API models through Kairos, which asks you before each call. Signing in happens in a browser on this computer."),
    el("div", { class: "set-toolbar" }, [search]), list,
  );
  draw();
  return group({ title: "Catalog" }, [details]);
}

function integrationForm(host, { title, description, fields, submit, label = "Add" }) {
  const err = el("div", { class: "set-error" });
  const save = el("button", { class: "btn primary", text: label });
  const cancel = el("button", { class: "btn quiet", text: "Cancel", onclick: () => host.replaceChildren() });
  save.addEventListener("click", async () => {
    err.textContent = "";
    const problem = fields.check?.();
    if (problem) { err.textContent = problem; return; }
    save.disabled = true;
    const before = save.textContent;
    if (label.includes("Sync")) save.textContent = "Syncing…";
    try { await submit(); }
    catch (e) { err.textContent = errorText(e); save.disabled = false; save.textContent = before; }
  });
  host.replaceChildren(group({ title, description, cls: "integration-form" },
    [...fields.rows, el("div", { class: "set-row-actions" }, [err, cancel, save])]));
  host.querySelector("input")?.focus();
}

function renderDavForm(host, rerender, kind) {
  const isCal = kind === "caldav_calendar";
  const name = el("input", { placeholder: "Name" });
  const url = el("input", { placeholder: isCal ? "Calendar collection URL" : "Address book collection URL" });
  const user = el("input", { placeholder: "Username", autocomplete: "off" });
  const pass = el("input", { type: "password", placeholder: "Password", autocomplete: "off" });
  integrationForm(host, {
    title: isCal ? "Add a CalDAV calendar" : "Add contacts (CardDAV)",
    description: "One-way read sync into Kairos, nothing written back. Use a specific calendar or address-book collection URL, not the server root.",
    label: "Add and sync",
    fields: { rows: [field("Name", name), field("URL", url), field("Username", user), field("Password", pass)],
      check: () => (!name.value.trim() || !url.value.trim() || !user.value.trim() || !pass.value) ? "All fields are required." : null },
    submit: async () => {
      await api(`/api/integrations/${isCal ? "caldav" : "carddav"}`, { method: "POST",
        body: JSON.stringify({ name: name.value.trim(), url: url.value.trim(), username: user.value.trim(), password: pass.value }) });
      await rerender();
    },
  });
}

function renderIcalForm(host, rerender) {
  const name = el("input", { placeholder: "Name" });
  const url = el("input", { placeholder: "https://… or webcal://… .ics feed URL" });
  const user = el("input", { placeholder: "Only if the feed needs it", autocomplete: "off" });
  const pass = el("input", { type: "password", placeholder: "Optional", autocomplete: "off" });
  integrationForm(host, {
    title: "Add an iCal feed",
    description: "A single .ics subscription URL, such as Google Calendar's secret iCal address or an Apple share link. One-way read sync.",
    label: "Add and sync",
    fields: { rows: [field("Name", name), field("Feed URL", url), field("Username", user), field("Password", pass)],
      check: () => (!name.value.trim() || !url.value.trim()) ? "Name and URL are required." : null },
    submit: async () => {
      await api("/api/integrations/ical", { method: "POST", body: JSON.stringify({
        name: name.value.trim(), url: url.value.trim(), username: user.value.trim() || null, password: pass.value || null }) });
      await rerender();
    },
  });
}

function renderApiServiceForm(host, rerender) {
  const name = el("input", { placeholder: "Name" });
  const url = el("input", { placeholder: "Base URL" });
  const key = el("input", { type: "password", placeholder: "Optional", autocomplete: "off" });
  integrationForm(host, {
    title: "Add an API service",
    fields: { rows: [field("Name", name), field("Base URL", url), field("API key", key)],
      check: () => (!name.value.trim() || !url.value.trim()) ? "Name and URL are required." : null },
    submit: async () => {
      await api("/api/integrations/api-service", { method: "POST",
        body: JSON.stringify({ name: name.value.trim(), base_url: url.value.trim(), api_key: key.value.trim() || null }) });
      await rerender();
    },
  });
}

function renderMcpServerForm(host, rerender) {
  const name = el("input", { placeholder: "Name" });
  const type = customSelect({}, [el("option", { value: "stdio", text: "Local command (stdio)" }), el("option", { value: "http", text: "Remote URL (http)" })]);
  const cmd = el("input", { placeholder: "npx @scope/server" });
  const url = el("input", { placeholder: "https://example.com/mcp" });
  const key = el("input", { type: "password", placeholder: "Optional", autocomplete: "off" });
  const cmdRow = field("Command", cmd);
  const urlRow = field("URL", url);
  urlRow.hidden = true;
  type.addEventListener("change", () => { cmdRow.hidden = type.value !== "stdio"; urlRow.hidden = type.value === "stdio"; });
  integrationForm(host, {
    title: "Add an MCP tool server",
    description: "A registered server widens what the models can really do. Open chats pick up a change on their next message.",
    fields: { rows: [field("Name", name), field("Type", type), cmdRow, urlRow, field("Auth token", key)],
      check: () => {
        if (!name.value.trim()) return "Name is required.";
        if (type.value === "stdio" && !cmd.value.trim()) return "A command is required for a local server.";
        if (type.value !== "stdio" && !url.value.trim()) return "A URL is required for a remote server.";
        return null;
      } },
    submit: async () => {
      const isStdio = type.value === "stdio";
      const parts = cmd.value.trim().split(/\s+/);
      await api("/api/integrations/mcp-server", { method: "POST", body: JSON.stringify({
        name: name.value.trim(), mcp_type: type.value, command: isStdio ? parts[0] : null, args: isStdio ? parts.slice(1) : null,
        url: isStdio ? null : url.value.trim(), api_key: key.value.trim() || null }) });
      await rerender();
    },
  });
}

// -- Vault --------------------------------------------------------------
async function renderVaultPanel(body, status, page) {
  const settings = await api("/api/settings");
  const pick = el("button", { class: "btn", text: "Choose folder…" });
  if (!window.jarvis) {
    pick.disabled = true;
    pick.title = "Folder picking is only available in the desktop app";
  }
  pick.addEventListener("click", async () => {
    const picked = await window.jarvis.pickVaultFolder();
    if (!picked) return;
    await api("/api/settings/vault-dir", { method: "POST", body: JSON.stringify({ path: picked }) });
    await renderVaultPanel(body, status, page);
  });

  // Vault <-> Notes sync (David's ask 2026-09-03: the app answered "no
  // priorities" while the vault was full of them). Runs at launch; this is
  // the manual re-run for edits made in Obsidian while the app is open.
  const syncStatus = el("span", { class: "meta" });
  const sync = el("button", { class: "btn", text: "Sync now" });
  sync.addEventListener("click", async () => {
    sync.disabled = true;
    syncStatus.textContent = "Syncing…";
    try {
      const r = await api("/api/vault/sync", { method: "POST" });
      const parts = [];
      if (r.imported) parts.push(`${r.imported} added`);
      if (r.updated) parts.push(`${r.updated} updated`);
      if (r.removed) parts.push(`${r.removed} removed`);
      syncStatus.textContent = parts.length ? `Synced: ${parts.join(", ")}.` : "Already up to date.";
      toast(parts.length ? `Vault synced: ${parts.join(", ")}` : "Notes already match your vault", "success");
    } catch {
      syncStatus.textContent = "";
    } finally {
      sync.disabled = false;
    }
  });

  body.replaceChildren(
    group({}, [
      row({ title: "Vault folder", description: [el("span", { class: "set-mono", text: settings.vault_dir })], control: pick,
        below: el("div", { class: "set-row-description", text: "Point Kairos at an existing vault on this device, or keep the default. Open chats keep their old vault until reconnected." }) }),
      row({ title: "Task list sync", description: "Checkbox items in your vault's Active Priorities.md show up as Notes, and ticking one here ticks it there. Runs every time Kairos starts.",
        control: [syncStatus, sync] }),
    ]),
  );
}

// -- Remote Access (David's ask 2026-09-03). A checklist rather than one
// "Enable" button: every prerequisite (Tailscale installed, signed in, the
// firewall, a login) is a separate thing someone might be missing, and one
// opaque failure gives them nothing to act on. See core/remote_access.py.
async function renderRemotePanel(body) {
  async function refresh() {
    let s;
    try {
      s = await api("/api/remote/status");
    } catch (e) {
      body.replaceChildren(note(`Couldn't read status: ${e.message}`, "set-error"));
      return;
    }
    const parts = [];

    // Fall back to composing the address from host and port rather than a
    // blank line, in case the URL wasn't captured (listener restored at start).
    if (s.running_now) s.url = s.url || (s.hostname ? `https://${s.hostname}:${s.port}` : null);
    if (s.running_now && s.url) {
      parts.push(group({}, [row({
        title: [el("span", { text: "Remote access is on" }), pill("Live", "ok")],
        description: [el("div", { class: "set-mono", text: s.url }),
          el("div", { text: "Open this exact address from any device signed into your Tailscale account. Include the port." })],
        control: [
          el("button", { class: "btn", text: "Copy link", onclick: async () => { await navigator.clipboard.writeText(s.url); toast("Link copied", "success"); } }),
          el("button", { class: "btn danger", text: "Turn off", onclick: async () => {
            await api("/api/remote/disable", { method: "POST" });
            toast("Remote access turned off", "success");
            await refresh();
          } }),
        ],
      })]));
      body.replaceChildren(...parts);
      return;
    }

    const steps = [
      { ok: s.installed, label: "Tailscale installed",
        hint: s.installed ? "Found on this machine." : "Tailscale is a free private network for your own devices. Install it, then come back here.",
        action: s.installed ? null : { label: "Get Tailscale", href: "https://tailscale.com/download" } },
      // Deliberately not the bare hostname: without scheme and port it read
      // like the address to visit and didn't work (David, 2026-09-03).
      { ok: s.logged_in, label: "Signed in to Tailscale",
        hint: s.logged_in ? `Connected as ${s.hostname ? s.hostname.split(".")[0] : "this machine"}.` : "Open the Tailscale app and sign in, then refresh." },
      // Binding needs no permission, so without this rule everything looks
      // right and no other device can connect.
      { ok: s.firewall_ok, label: "Allowed through Windows Firewall",
        hint: s.firewall_ok ? "Incoming connections to Kairos are allowed."
          : "Windows is blocking incoming connections to Kairos, so no other device can reach it. Adding the rule needs your permission; Windows will ask.",
        action: s.firewall_ok ? null : { label: "Allow", handler: async () => {
          await api("/api/remote/firewall", { method: "POST" });
          toast("Firewall rule added", "success");
          await refresh();
        } } },
      { ok: s.auth_ready, label: "Kairos login",
        hint: s.auth_ready ? "A login is set up."
          : s.has_any_users ? "An account exists, but login enforcement is off. Turn it on below."
            : "Remote access needs a real login; without one anyone reaching this machine would get straight in." },
    ];
    parts.push(group({ title: "Setup" }, steps.map((step) => {
      let control = pill(step.ok ? "Done" : "Needed", step.ok ? "ok" : "warn");
      if (step.action?.href) control = [control, el("a", { href: step.action.href, target: "_blank", rel: "noopener", class: "btn", text: step.action.label })];
      else if (step.action) {
        const button = el("button", { class: "btn", text: step.action.label });
        button.addEventListener("click", async () => {
          button.disabled = true; button.textContent = "Working…";
          try { await step.action.handler(); } catch { button.disabled = false; button.textContent = step.action.label; }
        });
        control = [control, button];
      }
      return row({ title: step.label, description: step.hint, control });
    })));

    // Two cases, deliberately separate (found live 2026-09-10): an account
    // can already exist with login enforcement off, which needs a one-click
    // fix, not a second sign-up form the backend would reject.
    if (!s.auth_ready && s.has_any_users) {
      const enable = el("button", { class: "btn primary", text: "Turn on account login" });
      enable.addEventListener("click", async () => {
        enable.disabled = true; enable.textContent = "Working…";
        try {
          await api("/api/remote/create-account", { method: "POST", body: JSON.stringify({ username: "", password: "" }) });
          toast("Login enforcement is on. Sign in with your existing account.", "success");
          await refresh();
        } finally { enable.disabled = false; enable.textContent = "Turn on account login"; }
      });
      parts.push(group({ title: "Turn on your existing login" }, [row({
        title: "Login enforcement", description: "An account exists on this machine, but logins are off. This turns them on; it won't create an account or change your password.",
        control: enable })]));
    } else if (!s.auth_ready) {
      const user = el("input", { placeholder: "Username", autocomplete: "off" });
      const pass = el("input", { type: "password", placeholder: "8 or more characters", autocomplete: "new-password" });
      const create = el("button", { class: "btn primary", text: "Create account" });
      create.addEventListener("click", async () => {
        if (!user.value.trim() || !pass.value) { toast("Enter a username and password", "error"); return; }
        create.disabled = true;
        try {
          await api("/api/remote/create-account", { method: "POST", body: JSON.stringify({ username: user.value.trim(), password: pass.value }) });
          toast("Account created. You'll sign in with it from now on.", "success");
          await refresh();
        } finally { create.disabled = false; }
      });
      parts.push(group({ title: "Create your login",
        description: "This turns on accounts for Kairos everywhere, this computer included, so you'll sign in here too." },
        [field("Username", user), field("Password", pass), el("div", { class: "set-row-actions" }, [create])]));
    }

    // The address, always shown once Tailscale knows this machine's name
    // (David, 2026-09-03: the panel named the machine but never the port).
    // The port saves on blur or Enter, not per keystroke, so typing "8" of
    // "8443" never tries to bind port 8.
    const port = el("input", { type: "number", value: String(s.port), style: "width:100px;" });
    const portStatus = el("span", { class: "meta" });
    const address = el("div", { class: "set-mono" });
    const syncAddress = () => { address.textContent = s.hostname ? `https://${s.hostname}:${parseInt(port.value, 10) || s.port}` : "—"; };
    syncAddress();
    port.addEventListener("input", () => { syncAddress(); portStatus.textContent = ""; });
    const savePort = async () => {
      const value = parseInt(port.value, 10);
      if (!value || value === s.port) return;
      portStatus.textContent = "Saving…";
      try {
        const r = await api("/api/remote/port", { method: "POST", body: JSON.stringify({ port: value }) });
        s.port = value;
        portStatus.textContent = r.restarted ? "Saved; listener moved" : "Saved";
        toast(r.restarted ? `Remote access moved to port ${value}` : `Port set to ${value}`, "success");
      } catch {
        portStatus.textContent = "";
        port.value = String(s.port); // failed to bind: show the port in use
        syncAddress();
      }
    };
    port.addEventListener("blur", savePort);
    port.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); port.blur(); } });
    if (s.hostname) {
      // Marked as not live: a real-looking address with nothing listening
      // was the same trap as the bare hostname (David, 2026-09-03).
      parts.push(group({ title: "Address" }, [
        row({ title: [el("span", { text: "Where Kairos will be reachable" }), pill("Not live yet", "muted")],
          description: address, control: el("button", { class: "btn", text: "Copy", onclick: async () => {
            await navigator.clipboard.writeText(address.textContent); toast("Address copied", "success"); } }) }),
        row({ title: "Port", description: "Part of the address you type.", control: [portStatus, port] }),
      ]));
    }

    const allReady = s.installed && s.logged_in && s.auth_ready;
    const enable = el("button", { class: "btn primary", text: "Turn on remote access", disabled: !allReady,
      title: allReady ? "" : "Finish the setup steps first" });
    enable.addEventListener("click", async () => {
      enable.disabled = true; enable.textContent = "Setting up…";
      try {
        await api("/api/remote/enable", { method: "POST", body: JSON.stringify({ port: parseInt(port.value, 10) || undefined }) });
        toast("Remote access is on", "success");
        await refresh();
      } catch {
        // api() already showed the real reason (certificate, port in use).
        enable.disabled = false; enable.textContent = "Turn on remote access";
      }
    });
    // Called out as its own step: with the checklist green and an address
    // shown, everything reads as already working and this is easy to skip.
    parts.push(group({}, [row({
      title: allReady ? "Last step: turn it on" : "Finish the setup steps first",
      description: allReady ? "Nothing is reachable until you do. It stays on across restarts." : "This unlocks once Tailscale is signed in and you have a login.",
      control: [el("button", { class: "btn", text: "Refresh", onclick: refresh }), enable] })]));
    parts.push(note("The first time, Kairos asks Tailscale for an HTTPS certificate so your browser trusts the connection. If your tailnet hasn't enabled HTTPS certificates, you'll be told where to switch them on."));
    body.replaceChildren(...parts);
  }
  await refresh();
}

// -- Account --------------------------------------------------------------
async function renderAccountPanel(body, status) {
  if (!status.auth_enabled) {
    body.replaceChildren(group({}, [row({ title: "Sign-in is off",
      description: "Kairos trusts this computer's single user, so there is nothing to manage here. Turn on logins in Remote Access, or with AUTH_ENABLED." })]));
    return;
  }
  const err = el("div", { class: "set-error" });
  const ok = el("span", { class: "meta" });
  const current = el("input", { type: "password", autocomplete: "current-password" });
  const next = el("input", { type: "password", autocomplete: "new-password" });
  const change = el("button", { class: "btn primary", text: "Change password" });
  change.addEventListener("click", async () => {
    err.textContent = ""; ok.textContent = "";
    try {
      await api("/api/auth/password", { method: "POST", body: JSON.stringify({ current_password: current.value, new_password: next.value }) });
      current.value = ""; next.value = "";
      ok.textContent = "Password changed.";
    } catch (e) { err.textContent = errorText(e); }
  });
  const totp = el("div");
  await renderTotpSection(totp);
  body.replaceChildren(
    group({}, [row({ icon: badge(status.username, hueFor(status.username)), title: status.username,
      description: status.is_admin ? "Admin" : "Member" })]),
    group({ title: "Password" }, [field("Current password", current), field("New password", next),
      el("div", { class: "set-row-actions" }, [err, ok, change])]),
    totp,
  );
}

async function renderTotpSection(host) {
  const enroll = el("button", { class: "btn", text: "Enable" });
  const disable = el("button", { class: "btn quiet danger", text: "Disable" });
  const uri = el("div", { class: "set-mono", style: "word-break:break-all;" });
  const code = el("input", { placeholder: "6-digit code", inputmode: "numeric", style: "width:140px;" });
  const confirm = el("button", { class: "btn primary", text: "Confirm" });
  const msg = el("div", { class: "set-row-description" });
  const setup = el("div", { class: "set-totp-setup" }, [msg, uri, el("div", { class: "set-row-control", style: "justify-content:flex-start;margin-top:8px;" }, [code, confirm])]);
  setup.hidden = true;
  enroll.addEventListener("click", async () => {
    const res = await api("/api/auth/totp/enroll", { method: "POST" });
    uri.textContent = res.provisioning_uri;
    msg.textContent = "Add this to your authenticator app (or paste the URI), then enter the 6-digit code.";
    setup.hidden = false;
  });
  confirm.addEventListener("click", async () => {
    try {
      await api("/api/auth/totp/confirm", { method: "POST", body: JSON.stringify({ code: code.value.trim() }) });
      setup.hidden = true;
      toast("Two-factor authentication is on", "success");
    } catch (e) { msg.textContent = errorText(e); }
  });
  disable.addEventListener("click", async () => {
    await api("/api/auth/totp/disable", { method: "POST" });
    toast("Two-factor authentication is off", "success");
  });
  host.replaceChildren(group({ title: "Two-factor authentication" }, [row({ title: "Authenticator app",
    description: "A code from your phone on every sign-in.", control: [enroll, disable], below: setup })]));
}

// -- Shortcuts --------------------------------------------------------------
function renderShortcutsPanel(body) {
  const shortcuts = [
    ["Enter", "Send message"],
    ["Shift + Enter", "New line in the composer"],
    ["Right-click a chat", "Rename, star or delete it"],
    ["/help in a chat", "List every slash command"],
  ];
  body.replaceChildren(group({}, shortcuts.map(([key, desc]) => row({ title: desc, control: el("span", { class: "set-kbd", text: key }) }))));
}

// -- Admin: Agent Tools -------------------------------------------------------
async function renderAgentToolsPanel(body, _status, page) {
  const data = await api("/api/settings/agent-tools");
  const switches = {};
  const save = el("button", { class: "btn primary", text: "Save" });
  save.addEventListener("click", async () => {
    const disabled_tools = Object.entries(switches).filter(([, sw]) => !sw.checked).map(([tool]) => tool);
    await api("/api/settings/agent-tools", { method: "POST", body: JSON.stringify({ disabled_tools }) });
    toast("Saved", "success");
  });
  page.actions([save]);
  const rows = data.available.map((tool) => {
    switches[tool] = toggle({ checked: !data.disabled.includes(tool), label: tool });
    return row({ title: el("span", { class: "set-mono", text: tool }), control: switches[tool] });
  });

  // Extra allowed MCP tools (David's ask 2026-09-10): an escape hatch for
  // what core/brain.py's built-in list doesn't cover. The SDK only matches
  // exact names, so a flow reaching for one more tool hits a wall nobody can
  // answer in a Discord or scheduled turn; this adds it without a release.
  const extra = el("textarea", { rows: "4", class: "set-mono", text: (data.extra_allowed || []).join("\n") });
  const saveExtra = el("button", { class: "btn", text: "Save extra tools" });
  saveExtra.addEventListener("click", async () => {
    const extra_allowed_tools = extra.value.split("\n").map((s) => s.trim()).filter(Boolean);
    await api("/api/settings/extra-allowed-tools", { method: "POST", body: JSON.stringify({ extra_allowed_tools }) });
    toast("Saved. Open chats pick it up on their next message.", "success");
  });
  body.replaceChildren(
    group({ title: "Built-in tools", description: "Switch a tool off to keep it out of every chat." }, rows),
    group({ title: "Extra allowed tools" }, [row({ stack: true, title: "Pre-approved MCP tools",
      description: "Exact tool names beyond the built-in list, one per line; for example a Canva tool that got blocked mid-conversation (the error names it).",
      control: extra }), el("div", { class: "set-row-actions" }, [saveExtra])]),
  );
}

// -- Admin: Users -------------------------------------------------------------
async function renderUsersPanel(body, status, page) {
  if (!status.auth_enabled) {
    body.replaceChildren(group({}, [row({ title: "Sign-in is off", description: "Managing users needs logins turned on (Remote Access, or AUTH_ENABLED)." })]));
    return;
  }
  const users = await api("/api/auth/users");
  const rerender = () => renderUsersPanel(body, status, page);
  const rows = users.map((u) => {
    const promote = el("button", { class: "btn", text: u.is_admin ? "Make member" : "Make admin", onclick: async () => {
      try {
        await api(`/api/auth/users/${u.username}/admin`, { method: "POST", body: JSON.stringify({ is_admin: !u.is_admin }) });
        toast(`${u.username} ${u.is_admin ? "is now a member" : "is now an admin"}`, "success");
        await rerender();
      } catch (e) { toast(errorText(e), "error"); }
    } });
    const remove = el("button", { class: "btn quiet danger", text: "Delete", onclick: async () => {
      const ok = await confirmDialog({ title: `Delete user "${u.username}"?`,
        message: "This account will be permanently removed and can no longer sign in.", confirmLabel: "Delete user" });
      if (!ok) return;
      try {
        await api(`/api/auth/users/${u.username}`, { method: "DELETE" });
        toast(`User ${u.username} deleted`, "success");
        await rerender();
      } catch (e) { toast(errorText(e), "error"); }
    } });
    if (u.username === status.username) { remove.disabled = true; remove.title = "You can't delete the account you're signed in as"; }
    return row({ icon: badge(u.username, hueFor(u.username)), title: u.username,
      description: [u.is_admin ? "Admin" : "Member", u.totp_enabled ? "two-factor on" : null, u.username === status.username ? "you" : null].filter(Boolean).join(" · "),
      control: [promote, remove] });
  });

  const name = el("input", { autocomplete: "off" });
  const pass = el("input", { type: "password", autocomplete: "new-password" });
  const admin = toggle({ label: "Admin" });
  const err = el("div", { class: "set-error" });
  const add = el("button", { class: "btn primary", text: "Add user" });
  add.addEventListener("click", async () => {
    err.textContent = "";
    try {
      await api("/api/auth/users", { method: "POST", body: JSON.stringify({ username: name.value.trim(), password: pass.value, is_admin: admin.checked }) });
      await rerender();
    } catch (e) { err.textContent = errorText(e); }
  });
  body.replaceChildren(
    group({ title: "People" }, rows),
    group({ title: "Add a user" }, [field("Username", name), field("Password", pass),
      row({ title: "Admin", description: "Admins manage models, agents, users and these settings.", control: admin }),
      el("div", { class: "set-row-actions" }, [err, add])]),
  );
}

// -- Admin: System (diagnostics, backup, wipe) -------------------------------
async function renderSystemPanel(body, status, page) {
  const diag = await api("/api/system/diagnostics");
  const facts = [
    ["Vault", diag.vault_exists ? diag.vault_dir : "missing"],
    ["Chats", diag.sessions_count],
    ["Notes", diag.notes_count],
    ["Tasks", diag.tasks_count],
    ["Skills", diag.skills_count],
    ["Model connections", diag.model_endpoints_count],
    ["Data on disk", `${(diag.data_dir_bytes / 1024).toFixed(1)} KB`],
    ["Discord", diag.discord_configured ? "configured" : "not configured"],
  ];

  const importInput = el("input", { type: "file", accept: "application/json", hidden: true });
  const backupMsg = el("span", { class: "meta" });
  importInput.addEventListener("change", async () => {
    const file = importInput.files[0];
    importInput.value = "";
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const res = await api("/api/system/backup/import", { method: "POST", body: JSON.stringify({ data }) });
      toast(`Imported ${res.settings} settings, ${res.notes} notes, ${res.skills} skills`, "success");
      await renderSystemPanel(body, status, page);
    } catch (e) { backupMsg.textContent = `Import failed: ${e.message}`; }
  });
  const exportBackup = async () => {
    const data = await api("/api/system/backup/export");
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const a = el("a", { href: url, download: `jarvis-backup-${new Date().toISOString().slice(0, 10)}.json` });
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  };

  // Back up everything and restore it (roadmap phase 8, core/backup.py).
  const restoreState = await api("/api/system/backup/restore").catch(() => ({}));
  const withKeys = toggle({ label: "Include saved passwords and keys" });
  const fullBackupLink = () => {
    const a = el("a", { href: `/api/system/backup/full?include_keys=${withKeys.checked ? "true" : "false"}`, download: "" });
    document.body.appendChild(a); a.click(); a.remove();
    toast("Preparing the backup; it downloads when ready", "success");
  };
  const restoreInput = el("input", { type: "file", accept: ".zip,application/zip", hidden: true });
  const restoreMsg = el("span", { class: "meta" });
  restoreInput.addEventListener("change", async () => {
    const file = restoreInput.files[0];
    restoreInput.value = "";
    if (!file) return;
    const ok = await confirmDialog({ title: "Restore this backup?",
      message: "At the next start, Kairos replaces its data with this backup. Your current data is kept first as a safety copy in the data folder. Your vault is not touched.",
      confirmLabel: "Restore at next start" });
    if (!ok) return;
    restoreMsg.textContent = "Checking the backup…";
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch("/api/system/backup/restore", { method: "POST", body: form });
      const payload = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : `HTTP ${res.status}`);
      toast("Backup ready: restart Kairos to finish restoring", "success");
      await renderSystemPanel(body, status, page);
    } catch (e) { restoreMsg.textContent = e.message; }
  });
  const when = (seconds) => new Date(seconds * 1000).toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
  const restoreRows = [];
  if (restoreState.pending) {
    restoreRows.push(row({ title: "A restore is waiting", cls: "backup-pending",
      description: `The backup from ${when(restoreState.pending.created_at)}${restoreState.pending.includes_keys ? " (with keys)" : ""} replaces the current data when Kairos next starts.`,
      control: el("button", { class: "btn quiet", text: "Cancel", onclick: async () => {
        await api("/api/system/backup/restore", { method: "DELETE" });
        await renderSystemPanel(body, status, page);
      } }) }));
  }
  if (restoreState.last) {
    const last = restoreState.last;
    restoreRows.push(row({ title: last.ok ? "Last restore" : "Last restore failed", cls: last.ok ? "backup-last" : "backup-last is-error",
      description: last.ok ? `Restored ${when(last.at)}. The data from before it is kept at ${last.safety_copy}.`
        : `${when(last.at)}: ${last.error}. Nothing was changed.` }));
  }

  // A genuine two-step confirmation: this wipes a whole kind of data
  // globally, for every user.
  const wipe = (kind) => el("button", { class: "btn danger", text: `Wipe ${kind}`, onclick: async () => {
    const first = await confirmDialog({ title: `Permanently delete all ${kind}?`,
      message: `Every one of your ${kind} will be erased. This cannot be undone.`, confirmLabel: `Wipe all ${kind}` });
    if (!first) return;
    const second = await confirmDialog({ title: "Are you absolutely sure?",
      message: `This wipes ${kind} for every user of this install, globally. There is no backup and no undo.`, confirmLabel: `Yes, wipe ${kind}` });
    if (!second) return;
    await api("/api/system/wipe", { method: "POST", body: JSON.stringify({ kind }) });
    toast(`All ${kind} wiped`, "success");
    await renderSystemPanel(body, status, page);
  } });

  body.replaceChildren(
    group({ title: "Diagnostics" }, facts.map(([label, value]) => row({ title: label,
      control: el("span", { class: "set-row-value", text: String(value) }) }))),
    group({ title: "Backup" }, [
      row({ title: "Back up everything", cls: "backup-full",
        description: "Chats, agents, tasks, triggers, connections, skills and settings as one zip. Your vault is not included: it is your own folder. Saved passwords and keys stay out unless you include them, and a backup with them is as private as the passwords themselves.",
        control: [el("label", { class: "set-inline-switch" }, [el("span", { class: "meta", text: "Keys" }), withKeys]),
          el("button", { class: "btn", text: "Back up", onclick: fullBackupLink })] }),
      row({ title: "Restore from a backup", cls: "backup-restore",
        description: "Replaces Kairos's data with a backup at the next start, keeping the current data as a safety copy first.",
        control: [restoreMsg, restoreInput, el("button", { class: "btn", text: "Restore…", onclick: () => restoreInput.click() })] }),
      ...restoreRows,
      row({ title: "Export settings, notes and skills", description: "A small JSON file that works on another computer or version.", control: el("button", { class: "btn quiet", text: "Export", onclick: exportBackup }) }),
      row({ title: "Import settings, notes and skills", description: "From a file made with Export.", control: [backupMsg, importInput,
        el("button", { class: "btn quiet", text: "Import…", onclick: () => importInput.click() })] }),
    ]),
    group({ title: "Danger zone", description: "Permanent, with no undo.", cls: "set-danger" },
      ["chats", "notes", "tasks", "skills"].map((kind) => row({ title: `All ${kind}`, description: `Erase every ${kind.slice(0, -1)} for every user.`, control: wipe(kind) }))),
  );
}

// -- Admin: Logs (Hermes track, 2026-09-22) ----------------------------------
// Reads backend.log, errors.log and desktop.log through /api/system/logs
// (core/logs.py). Follow polls with the byte cursor the last read returned,
// and stops by itself once the page is gone.
const LOG_FOLLOW_MS = 2000;
const LOG_MAX_SHOWN = 1000;

async function renderLogsPanel(body) {
  const [files, chats] = await Promise.all([
    api("/api/system/logs/files"),
    api("/api/sessions").catch(() => []),
  ]);
  const chatTitles = new Map(chats.map((c) => [c.id, c.title || "Untitled chat"]));
  const option = (value, text, selected = false) => el("option", { value, text, ...(selected ? { selected: "" } : {}) });
  const fileSelect = customSelect({ class: "logs-file" }, files.map((f) =>
    option(f.name, `${f.name[0].toUpperCase()}${f.name.slice(1)} (${f.size ? `${(f.size / 1024).toFixed(0)} KB` : "empty"})`)));
  const levelSelect = customSelect({ class: "logs-level" }, [
    option("", "All levels"), option("INFO", "Info and up"), option("WARNING", "Warnings and up"), option("ERROR", "Errors only")]);
  const componentSelect = customSelect({ class: "logs-component" }, [
    option("", "Every area"), ...["chat", "swarm", "tasks", "remote", "discord", "skills"].map((c) => option(c, c[0].toUpperCase() + c.slice(1)))]);
  const sinceSelect = customSelect({ class: "logs-since" }, [
    option("", "Any time"), option("15m", "Last 15 minutes"), option("1h", "Last hour"), option("24h", "Last day"), option("7d", "Last week")]);
  const chatSelect = customSelect({ class: "logs-chat" }, [
    option("", "Any chat or task"), ...chats.slice(0, 50).map((c) => option(c.id, c.title || "Untitled chat"))]);
  const searchInput = el("input", { class: "logs-search", type: "search", placeholder: "Search text", maxlength: "200" });
  const follow = toggle({ label: "Follow" });
  follow.classList.add("logs-follow");
  const status = el("div", { class: "meta logs-status" });
  const output = el("div", { class: "logs-output" });

  body.replaceChildren(
    el("div", { class: "logs-controls set-toolbar" }, [fileSelect, levelSelect, componentSelect, sinceSelect, chatSelect, searchInput,
      el("label", { class: "logs-follow-label set-inline-switch" }, [el("span", { text: "Follow" }), follow])]),
    status, output);

  let cursor = 0;
  let timer = null;
  let generation = 0;

  const query = (extra = {}) => {
    const params = new URLSearchParams({ name: fileSelect.value, ...extra });
    for (const [key, select] of [["level", levelSelect], ["component", componentSelect], ["since", sinceSelect], ["tag", chatSelect]]) {
      if (select.value) params.set(key, select.value);
    }
    if (searchInput.value.trim()) params.set("text", searchInput.value.trim());
    return `/api/system/logs?${params}`;
  };

  const entryEl = (entry) => {
    const rowEl = el("div", { class: `log-entry log-${entry.level.toLowerCase()}` });
    if (entry.tag) rowEl.append(el("span", { class: "log-tag", text: chatTitles.get(entry.tag) || entry.tag, title: entry.tag }));
    rowEl.append(document.createTextNode(entry.text));
    return rowEl;
  };

  const append = (entries) => {
    const stick = output.scrollTop + output.clientHeight >= output.scrollHeight - 8;
    for (const entry of entries) output.append(entryEl(entry));
    while (output.childElementCount > LOG_MAX_SHOWN) output.firstElementChild.remove();
    if (stick) output.scrollTop = output.scrollHeight;
  };

  const stopFollowing = () => { if (timer) { clearInterval(timer); timer = null; } };

  const poll = async (mine) => {
    if (!document.body.contains(output)) { stopFollowing(); return; }
    try {
      const res = await api(query({ cursor: String(cursor) }));
      if (mine !== generation) return;
      if (res.rotated) status.textContent = "The log rotated; following the new file.";
      cursor = res.end;
      append(res.entries);
    } catch (problem) { status.textContent = `Following stopped: ${problem.message}`; stopFollowing(); follow.checked = false; }
  };

  const load = async () => {
    const mine = ++generation;
    stopFollowing();
    status.textContent = "Loading…";
    try {
      const res = await api(query({ limit: "300" }));
      if (mine !== generation) return;
      output.innerHTML = "";
      cursor = res.end;
      if (!res.exists) status.textContent = "Nothing has been written to this log yet.";
      else if (!res.entries.length) status.textContent = "No lines match.";
      else status.textContent = `Showing the last ${res.entries.length} matching entries.`;
      append(res.entries);
      output.scrollTop = output.scrollHeight;
      if (follow.checked) timer = setInterval(() => poll(mine), LOG_FOLLOW_MS);
    } catch (problem) { status.textContent = problem.message; }
  };

  for (const control of [fileSelect, levelSelect, componentSelect, sinceSelect, chatSelect]) control.addEventListener("change", load);
  follow.addEventListener("click", load);
  let searchDelay = null;
  searchInput.addEventListener("input", () => { clearTimeout(searchDelay); searchDelay = setTimeout(load, 300); });
  await load();
}

// -- Admin: Runs (roadmap phase 8, 2026-10-06; routes/run_routes.py) --------
async function renderRunsPanel(body) {
  const outcome = customSelect({ class: "runs-outcome" }, [
    el("option", { value: "", text: "Every run" }), el("option", { value: "failed", text: "Failed" }),
    el("option", { value: "stopped", text: "Stopped" }), el("option", { value: "finished", text: "Finished" }),
  ]);
  const list = el("div", { class: "runs-list" });
  const load = async () => {
    list.replaceChildren(el("div", { class: "meta", text: "Loading…" }));
    let found;
    try { found = await api(`/api/runs?limit=100${outcome.value ? `&outcome=${outcome.value}` : ""}`); }
    catch (problem) { list.replaceChildren(el("div", { class: "meta", text: problem.message })); return; }
    if (!found.length) { list.replaceChildren(empty(outcome.value ? `No ${outcome.value} runs` : "No runs yet")); return; }
    list.replaceChildren(...found.map((run) => {
      const host = el("div", { class: "run-timeline-host" });
      const open = el("button", { class: "btn quiet", text: "Steps", onclick: () => toggleRunTimeline(run.id, host) });
      const when = new Date(run.started_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
      return el("div", { class: "run-item", "data-run": run.id }, [
        el("div", { class: "run-item-head" }, [
          el("span", { class: `run-outcome run-${run.outcome === "finished" ? "succeeded" : run.outcome}`, text: run.outcome }),
          el("span", { class: "run-item-title", text: `${surfaceLabel(run.surface)}${run.label ? `: ${run.label}` : ""}` }),
          el("span", { class: "meta", text: `${when} · ${runSummary(run)}` }),
          open,
        ]),
        host,
      ]);
    }));
  };
  outcome.addEventListener("change", load);
  body.replaceChildren(el("div", { class: "set-toolbar" }, [outcome]), list);
  await load();
}

async function renderFileCheckpointsPanel(body, status, page) {
  const events = await api("/api/file-checkpoints");
  const changed = events.filter((event) => event.status === "changed");
  if (!changed.length) {
    body.replaceChildren(group({}, [empty("No file changes recorded yet")]));
    return;
  }
  body.replaceChildren(...changed.map((event) => {
    const details = el("div", { class: "hidden" });
    const show = el("button", { class: "btn", text: "Review" });
    show.addEventListener("click", async () => {
      if (!details.classList.contains("hidden")) {
        details.classList.add("hidden");
        show.textContent = "Review";
        return;
      }
      const full = await api(`/api/file-checkpoints/${event.id}`);
      details.innerHTML = "";
      const choices = [];
      for (const root of full.roots) {
        if (!root.changes?.length && !root.before_skipped?.length && !root.after_skipped?.length) continue;
        details.append(el("div", { class: "meta set-mono", text: root.path }));
        for (const change of root.changes || []) {
          const available = change.restore_state === "available";
          const check = el("input", { type: "checkbox", ...(available ? { checked: "" } : { disabled: "" }) });
          if (available) choices.push({ check, root: root.path, path: change.path });
          const state = change.restore_state === "restored" ? " · Restored"
            : change.restore_state === "at_before" ? " · Already at previous state"
            : change.restore_state === "changed_since" ? " · Changed again; restore unavailable" : "";
          details.append(el("label", { class: "card-row" }, [check, el("span", { class: "meta", text: change.path + state })]),
            el("pre", { class: "sandbox-diff", text: change.diff || "No text diff." }));
        }
        const skipped = [...new Set([...(root.before_skipped || []), ...(root.after_skipped || [])])];
        if (skipped.length) details.append(el("div", { class: "meta", text: `Skipped: ${skipped.join(", ")}` }));
      }
      const restore = el("button", { class: "btn danger", text: choices.length ? "Restore selected files" : "Nothing left to restore",
        disabled: !choices.length });
      restore.addEventListener("click", async () => {
        const files = choices.filter((c) => c.check.checked).map(({ root, path }) => ({ root, path }));
        if (!files.length) return;
        const ok = await confirmDialog({ title: "Restore selected files?",
          message: `${files.length} file(s) will return to their state before this model turn.`, confirmLabel: "Restore files" });
        if (!ok) return;
        try {
          await api(`/api/file-checkpoints/${event.id}/restore`, { method: "POST", body: JSON.stringify({ files }) });
          toast("Files restored", "success");
          await renderFileCheckpointsPanel(body, status, page);
        } catch (problem) { toast(errorText(problem), "error"); }
      });
      details.append(restore);
      details.classList.remove("hidden");
      show.textContent = "Hide";
    });
    const count = event.roots.reduce((sum, root) => sum + root.changes.length, 0);
    const restored = event.roots.reduce((sum, root) => sum + root.changes.filter((change) => change.restored_at).length, 0);
    return el("div", { class: "sandbox-change file-checkpoint" }, [
      row({ title: event.source, control: show, description: `${new Date(event.created * 1000).toLocaleString()} · ${count} file(s)`
        + (restored ? ` · ${restored} restored` : "")
        + (event.overlap ? " · another turn overlapped; review attribution carefully" : "") }),
      details,
    ]);
  }));
}

// Sandbox changes (Hermes phase 7, 2026-09-24): edits a model made to a
// sandboxed copy of the Kairos code wait here until an admin applies or
// discards them (core/sandbox_changes.py). No model can apply one. Applying
// is all or nothing and refuses if a file changed since the run; it does not
// commit. Diffs are shown as text, never as HTML.
async function renderSandboxChangesPanel(body, status, page) {
  const [changes, chats] = await Promise.all([
    api("/api/sandbox/changes"),
    api("/api/sessions").catch(() => []),
  ]);
  const chatTitles = new Map(chats.map((c) => [c.id, c.title || "Untitled chat"]));
  if (!changes.length) {
    body.replaceChildren(group({}, [empty("No sandbox changes waiting")]));
    return;
  }
  const rerender = () => renderSandboxChangesPanel(body, status, page);
  body.replaceChildren(...changes.map((change) => {
    const diffBox = el("pre", { class: "sandbox-diff hidden" });
    const showBtn = el("button", { class: "btn", text: "Show diff" });
    showBtn.addEventListener("click", async () => {
      if (diffBox.classList.toggle("hidden")) { showBtn.textContent = "Show diff"; return; }
      showBtn.textContent = "Hide diff";
      if (diffBox.childElementCount) return;
      const full = await api(`/api/sandbox/changes/${change.id}`);
      for (const line of (full.diff || "No text diff (binary files only).").split("\n")) {
        const kind = line.startsWith("+") && !line.startsWith("+++") ? "add"
          : line.startsWith("-") && !line.startsWith("---") ? "del" : line.startsWith("@@") ? "hunk" : "";
        diffBox.append(el("span", { class: `sandbox-diff-line ${kind}`, text: `${line}\n` }));
      }
    });
    const applyBtn = el("button", { class: "btn primary", text: "Apply", disabled: !change.applicable });
    applyBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({ title: "Apply these changes?",
        message: `${change.changes.length} file(s) will be written into the Kairos folder. Nothing is committed.`, confirmLabel: "Apply changes" });
      if (!ok) return;
      try {
        await api(`/api/sandbox/changes/${change.id}/apply`, { method: "POST" });
        toast("Changes applied", "success");
      } catch (problem) { toast(errorText(problem), "error"); }
      await rerender();
    });
    const discardBtn = el("button", { class: "btn quiet danger", text: "Discard", onclick: async () => {
      await api(`/api/sandbox/changes/${change.id}`, { method: "DELETE" });
      toast("Changes discarded", "success");
      await rerender();
    } });
    return el("div", { class: "sandbox-change", "data-change": change.id }, [
      el("div", { class: "card-row sandbox-change-head" }, [
        el("div", {}, [
          el("div", { class: "set-row-title", text: `Change set ${change.id}` }),
          el("div", { class: "set-row-description", text: `${new Date(change.created * 1000).toLocaleString()} · `
            + (change.session_id ? chatTitles.get(change.session_id) || "a chat" : "no chat") }),
        ]),
        el("div", { class: "card-row sandbox-change-actions" }, [showBtn, applyBtn, discardBtn]),
      ]),
      el("ul", { class: "sandbox-change-files" }, change.changes.map((c) => el("li", { class: "meta", text: `${c.status}: ${c.path}` }))),
      change.applicable ? null : el("div", { class: "meta", text: "Too large to keep for applying; read only." }),
      diffBox,
    ]);
  }));
}

// Every standing grant, and a way to take it back. An "always" that cannot
// be found later is a trap, so this lists what was granted, how wide it is,
// and when - including the built-in grants that used to be invisible in code.
async function renderPermissionsPanel(body, status, page) {
  const { rules, audit } = await api("/api/permissions");
  const rows = rules.map((rule) => {
    const scope = rule.admin_only ? "admins only, everywhere" : rule.scope === "session" ? "this chat only" : "everywhere";
    return row({ title: rule.content ? `${rule.tool} — ${rule.content}` : rule.tool, cls: "settings-row",
      description: `${rule.behavior === "allow" ? "Allowed" : "Denied"} ${scope}`
        + (rule.source === "built-in" ? " · built in" : rule.granted_by ? ` · granted by ${rule.granted_by}` : "")
        + (rule.granted_at ? ` · ${new Date(rule.granted_at * 1000).toLocaleString()}` : ""),
      control: el("button", { class: "btn quiet danger", text: "Revoke", onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await api(`/api/permissions/${encodeURIComponent(rule.id)}`, { method: "DELETE" });
          renderPermissionsPanel(body, status, page);
        } catch (problem) { toast(problem.message, "error"); button.disabled = false; }
      } }) });
  });
  const parts = [group({ title: "Standing grants" }, rows.length ? rows : [empty("Nothing is granted yet.")])];
  if (audit.length) {
    const log = el("details", { class: "disclosure-panel" }, [el("summary", { text: "Recent decisions" })]);
    for (const entry of [...audit].reverse()) {
      log.append(el("div", { class: "meta", text:
        `${new Date(entry.at * 1000).toLocaleString()} · ${entry.decision}`
        + (entry.tool ? ` · ${entry.tool}` : "") + (entry.content ? ` (${entry.content})` : "")
        + (entry.by ? ` · ${entry.by}` : "") }));
    }
    parts.push(group({}, [log]));
  }
  body.replaceChildren(...parts);
}
