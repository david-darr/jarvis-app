import { api, el, customSelect, toast, confirmDialog } from "../api.js";

// Same display convention chat.js's model picker uses (name + underlying
// model, CLI-aware) — duplicated rather than shared since the two modules
// build their pickers with different components (native menu vs. customSelect).
function modelLabel(ep) {
  return ep.kind === "claude_cli" ? `${ep.name} (${ep.model || "CLI default"})` : `${ep.name} (${ep.model})`;
}

// Settings as a real floating window (David's ask, 2026-08-31: "more of a
// pop up that can be closed or minimized, with a similar UI to Odysseus") —
// title bar + minimize/close, left nav with a real filtering search box,
// single content panel on the right. Structure ported from Odysseus's own
// Settings modal (the screenshot David sent + ~/odysseus's
// static/js/settings/{registry,navigation,search}.js), our glass/cyan skin.
//
// Scope note: Search (SearXNG) and Integrations have no foundation in
// JARVIS — not built as fake nav items. Model config MOVED here from
// Cookbook (David's follow-up ask, 2026-08-31, after sending real Odysseus
// screenshots — Odysseus itself keeps lightweight endpoint add/list in
// Settings and reserves Cookbook for the heavy local-model download/serve
// pipeline, matching this project's own original v1 scoping note before an
// earlier session moved it to Cookbook instead; this corrects that back to
// match the real reference). Cookbook is now an honest pointer back here.
// "AI Defaults" (per-feature default model: chat/utility/vision/research)
// isn't built — JARVIS has no utility-model split, vision routing, or
// research feature to point defaults at, so a settings panel for it would
// be fake UI with nothing behind it.

const NAV_SECTIONS = [
  { id: "add-models", label: "Add Models", render: renderAddModelsPanel },
  { id: "added-models", label: "Added Models", render: renderAddedModelsPanel },
  { id: "integrations", label: "Integrations", render: renderIntegrationsPanel },
  { id: "vault", label: "Vault", render: renderVaultPanel },
  { id: "channels", label: "Channels", render: renderChannelsPanel },
  { id: "remote", label: "Remote Access", render: renderRemotePanel },
  { id: "account", label: "Account", render: renderAccountPanel },
  { id: "shortcuts", label: "Shortcuts", render: renderShortcutsPanel },
];
const ADMIN_SECTIONS = [
  { id: "agent-tools", label: "Agent Tools", render: renderAgentToolsPanel },
  { id: "users", label: "Users", render: renderUsersPanel },
  { id: "system", label: "System", render: renderSystemPanel },
  // Developer Mode only (David's ask 2026-09-01) — filtered out of
  // buildNav()'s renderGroup(ADMIN_SECTIONS) call below unless dev-mode is
  // on, same gate app.js uses for the "+ New Tab" nav item.
  { id: "custom-tabs", label: "Custom Tabs", render: renderCustomTabsPanel },
];

let modalEl = null;
let pillEl = null;
let activeSectionId = "add-models";
let cachedStatus = null;

export async function render(container) {
  // Settings has no real "page" anymore — clicking its nav item opens the
  // floating window over whatever tab was active. Leave the tab body empty
  // rather than a jarring blank "Loading..." that never resolves.
  container.innerHTML = "";
  await openSettingsWindow();
}

export async function openSettingsWindow() {
  if (pillEl) { pillEl.remove(); pillEl = null; }
  cachedStatus = await api("/api/auth/status");
  // Guards against a stale activeSectionId from a previous visit if
  // Developer Mode got turned off in between (buildNav() already hides the
  // nav entry — this keeps selectSection() from rendering the panel anyway).
  if (activeSectionId === "custom-tabs" && !document.documentElement.classList.contains("dev-mode")) {
    activeSectionId = "add-models";
  }
  const modal = getModal();
  modal.classList.remove("hidden");
  pinInitialRect(modal.querySelector(".settings-window"));
  buildNav();
  await selectSection(activeSectionId);
}

// Settings as a real full-screen page on mobile, not a popup (David's ask
// 2026-09-01) — app.js's settings-gear handler calls this instead of
// openSettingsWindow() below the responsive breakpoint. Same
// titlebar/nav/content building blocks (buildNav()/selectSection() below
// look these up by id, so they work unchanged against either shell), just
// mounted directly into the tab's own view content — no backdrop, no
// minimize (that's a "floating window" concept a full page doesn't have),
// "back" instead of "close".
export async function renderMobilePage(container) {
  if (pillEl) { pillEl.remove(); pillEl = null; }
  // Guards against the (unlikely but possible) case of the floating modal
  // having been created earlier in this same page load — it and the mobile
  // page shell both use the same #settings-nav/#settings-content ids for
  // buildNav()/selectSection() to find, so only one can exist at a time.
  if (modalEl) { modalEl.remove(); modalEl = null; }
  cachedStatus = await api("/api/auth/status");
  container.innerHTML = "";

  const backBtn = el("button", { class: "settings-titlebar-btn", title: "Back", onclick: () => {
    const homeNav = document.querySelector('.nav-item[data-tab="home"]');
    if (homeNav) homeNav.click();
  }, text: "←" });
  const titlebar = el("div", { class: "settings-titlebar" }, [
    el("div", { class: "title" }, [el("span", { text: "⚙" }), el("span", { text: "Settings" })]),
    el("div", { class: "settings-titlebar-actions" }, [backBtn]),
  ]);
  const nav = el("div", { class: "settings-nav", id: "settings-nav" });
  const content = el("div", { class: "settings-content", id: "settings-content" });
  const body = el("div", { class: "settings-body" }, [nav, content]);
  container.append(titlebar, body);

  buildNav();
  await selectSection(activeSectionId);
}

function closeSettingsWindow() {
  if (modalEl) modalEl.classList.add("hidden");
  // No dedicated "Settings" page to return to — go back to Home, same as
  // closing any other floating window in the app.
  const homeNav = document.querySelector('.nav-item[data-tab="home"]');
  if (homeNav) homeNav.click();
}

function minimizeSettingsWindow() {
  if (modalEl) modalEl.classList.add("hidden");
  if (pillEl) return;
  pillEl = el("div", { class: "glass bracket settings-minimized-pill", onclick: () => { pillEl.remove(); pillEl = null; modalEl.classList.remove("hidden"); } }, [
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
    el("div", { class: "title" }, [el("span", { text: "⚙" }), el("span", { text: "Settings" })]),
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

  modalEl = backdrop;
  return modalEl;
}

// Resizable from any edge/corner (David's ask 2026-09-01) — the window
// starts at its normal CSS-centered size/position (880x640, capped to
// 94vw/88vh), then gets pinned to that exact rect via `position: fixed`
// with explicit left/top/width/height so each handle can move just its own
// edge independently, same as a real desktop window (dragging the left edge
// shouldn't also move the right edge, which is what plain centered flex
// layout would do if only width changed). Mouse-only (no touch handlers) —
// the mobile shell doesn't use this floating window at all (renderMobilePage
// is a separate full-page render), so there's no touch case to cover.
//
// Real bug found live 2026-09-01 (David: "when i click on settings nothing
// even shows anymore") — the initial rect used to be captured inside
// makeResizable() itself, called from getModal() right after the backdrop
// is built but while it still has the `hidden` class (`display: none`), so
// getBoundingClientRect() returned an all-zero rect and pinned the window at
// 0x0. Split in two: attachResizeHandles() (layout-independent, safe at
// construction time) and pinInitialRect() (needs a real, visible layout —
// called from openSettingsWindow() *after* the `hidden` class is removed).
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
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  }
}

function buildNav() {
  const nav = document.getElementById("settings-nav");
  nav.innerHTML = "";
  const search = el("input", { class: "settings-search", placeholder: "Find settings..." });
  nav.appendChild(search);

  const allItems = [];
  const renderGroup = (sections) => {
    for (const sec of sections) {
      const item = el("div", { class: "settings-nav-item" + (sec.id === activeSectionId ? " active" : ""), text: sec.label, onclick: () => selectSection(sec.id) });
      allItems.push({ item, label: sec.label });
      nav.appendChild(item);
    }
  };
  renderGroup(NAV_SECTIONS);
  if (cachedStatus.is_admin) {
    nav.appendChild(el("div", { class: "settings-nav-group", text: "ADMIN" }));
    const devMode = document.documentElement.classList.contains("dev-mode");
    renderGroup(ADMIN_SECTIONS.filter((s) => s.id !== "custom-tabs" || devMode));
  }

  search.addEventListener("input", () => {
    const q = search.value.trim().toLowerCase();
    for (const { item, label } of allItems) {
      item.style.display = label.toLowerCase().includes(q) ? "" : "none";
    }
  });
}

async function selectSection(id) {
  activeSectionId = id;
  document.querySelectorAll(".settings-nav-item").forEach((n) => n.classList.toggle("active", n.textContent === sectionLabel(id)));
  const content = document.getElementById("settings-content");
  content.innerHTML = "";
  const all = [...NAV_SECTIONS, ...ADMIN_SECTIONS];
  const section = all.find((s) => s.id === id) || all[0];
  await section.render(content, cachedStatus);
}

function sectionLabel(id) {
  const all = [...NAV_SECTIONS, ...ADMIN_SECTIONS];
  return (all.find((s) => s.id === id) || {}).label;
}

// Known API providers (David's ask 2026-08-31, "reference the odysseus
// repo" — mirrors static/index.html's #adm-epProvider option list in
// ~/odysseus). Only providers that actually work with a plain bearer-token
// API key over an OpenAI-compatible endpoint are included — Odysseus's
// GitHub Copilot / ChatGPT Subscription entries use a device-auth flow
// (core/providerDeviceFlow.js) jarvis-app doesn't have, so they're left out
// rather than faked as a simple key field.
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

// -- Add Models (moved from Cookbook, David's ask 2026-08-31; "claude_cli"
// kind added 2026-08-31 follow-up — JARVIS ships with no default model, so
// Claude itself has to be added here like anything else, not assumed) -----
function modelCard(title, subtitle, kind, onAdded) {
  const nameInput = el("input", { placeholder: "Name (e.g. \"" + (kind === "local" ? "Local Ollama" : kind === "claude_cli" ? "Claude" : "OpenRouter") + "\")", style: "flex:1;" });
  const urlInput = el("input", { placeholder: kind === "local" ? "http://localhost:11434/v1" : "https://api.openrouter.ai/v1", style: "flex:1;" });
  const modelInput = el("input", {
    placeholder: kind === "claude_cli" ? "Model override (optional, e.g. claude-sonnet-4-5)" : "Model id",
    style: "flex:1;",
  });
  const keyInput = el("input", { type: "password", placeholder: "API key" + (kind === "local" ? " (optional)" : ""), style: "flex:1;" });
  // Context window cap (David's ask 2026-09-01, real incident — a local
  // model loaded with no cap defaulted to its max context and its KV cache
  // alone ate ~21GB of RAM). Local-only: sent as Ollama's `options.num_ctx`;
  // other local servers that don't recognize it just ignore the field.
  const ctxInput = el("input", { type: "number", placeholder: "Context window (default 4096)", style: "flex:1;" });
  const err = el("div", { class: "meta", style: "color:var(--danger);" });
  const addBtn = el("button", { class: "btn", text: "+ Add" });

  // Provider picker (API cards only) — picking a known provider fills in its
  // name + base URL so the user only has to add a model id and their key,
  // same shape as Odysseus's #adm-epProvider dropdown. "Custom URL" clears
  // both fields back to freeform entry.
  let providerSelect = null;
  if (kind === "api") {
    const options = [
      el("option", { value: "" }, "Custom URL"),
      ...KNOWN_API_PROVIDERS.map((p) => el("option", { value: p.base_url }, p.label)),
    ];
    providerSelect = customSelect({ style: "flex:1;" }, options);
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
    if (kind === "claude_cli") {
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
        }),
      });
      nameInput.value = ""; urlInput.value = ""; modelInput.value = ""; keyInput.value = ""; ctxInput.value = "";
      if (providerSelect) providerSelect.value = "";
      onAdded();
    } catch (e) { err.textContent = e.message.replace(/^\d+: /, ""); }
  });

  const fields = kind === "local"
    ? [nameInput, urlInput, modelInput, ctxInput]
    : kind === "claude_cli"
      ? [nameInput, modelInput]
      : [providerSelect, nameInput, urlInput, modelInput, keyInput];
  return el("div", { class: "glass bracket card", style: "margin-bottom:14px;" }, [
    el("div", { class: "title", text: title }),
    el("div", { class: "meta", style: "margin:4px 0 10px;", text: subtitle }),
    el("div", { class: "card-row", style: "flex-wrap:wrap;gap:8px;" }, [...fields, addBtn]),
    err,
  ]);
}

function renderAddModelsPanel(content) {
  content.innerHTML = "";
  content.append(
    el("div", { class: "title", text: "Add Models" }),
    el("div", { class: "meta", style: "margin:6px 0 14px;", text: "JARVIS ships with no default model — add at least one below, then pick it from the model menu above the chat box." }),
    modelCard("Add Claude Code CLI", "Uses the claude CLI already installed and logged in on this machine — no key needed here.", "claude_cli", () => selectSection("added-models")),
    modelCard("Add Local Models", "A local model server (Ollama, llama.cpp, vLLM).", "local", () => selectSection("added-models")),
    modelCard("Add API Models", "Connect a cloud provider (OpenAI, Anthropic, OpenRouter, etc.).", "api", () => selectSection("added-models")),
  );
}

// -- Added Models -------------------------------------------------------
async function renderAddedModelsPanel(content) {
  const endpoints = await api("/api/models");
  content.innerHTML = "";
  const probeAllBtn = el("button", { class: "btn", text: "↻ Probe" });
  content.append(
    el("div", { class: "card-row", style: "justify-content:space-between;" }, [
      el("div", { class: "title", text: "Added Models" }),
      probeAllBtn,
    ]),
    el("div", { class: "meta", style: "margin:6px 0 14px;", text: "Endpoints you've connected. Probe re-tests them all." }),
  );

  const claudeEps = endpoints.filter((e) => e.kind === "claude_cli");
  const local = endpoints.filter((e) => e.kind === "local");
  const apiEps = endpoints.filter((e) => e.kind === "api");
  const rowsByEndpoint = {};

  function endpointRow(ep) {
    const resultEl = el("span", { class: "meta" });
    const testBtn = el("button", { class: "btn", text: "Test" });
    testBtn.addEventListener("click", async () => {
      resultEl.textContent = "Testing...";
      const res = await api(`/api/models/${ep.id}/test`, { method: "POST" });
      resultEl.textContent = res.ok ? `OK · ${res.latency_ms}ms` : `Failed: ${res.detail}`;
      resultEl.style.color = res.ok ? "var(--accent)" : "var(--danger)";
    });
    const delBtn = el("button", { class: "btn danger", text: "Remove" });
    delBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "Remove this model?",
        message: `"${ep.name}" will be removed. Any chat still set to it will need a new model chosen.`,
        confirmLabel: "Remove model",
      });
      if (!ok) return;
      await api(`/api/models/${ep.id}`, { method: "DELETE" });
      await renderAddedModelsPanel(content);
      toast("Model removed", "success");
    });
    const metaText = ep.kind === "claude_cli"
      ? `Model: ${ep.model || "CLI default"}`
      : `${ep.model} · ${ep.base_url}${ep.has_api_key ? " · key saved" : ""}${ep.kind === "local" && ep.num_ctx ? ` · ctx ${ep.num_ctx}` : ""}`;
    const row = el("div", { class: "card-row", style: "justify-content:space-between;align-items:center;margin-top:8px;" }, [
      el("div", {}, [
        el("div", { class: "title", style: "font-size:12.5px;", text: ep.name }),
        el("div", { class: "meta", text: metaText }),
      ]),
      el("div", { class: "card-row", style: "gap:6px;" }, [resultEl, testBtn, delBtn]),
    ]);
    rowsByEndpoint[ep.id] = testBtn;
    return row;
  }

  content.append(el("div", { class: "meta", style: "margin-top:12px;color:var(--text-faint);letter-spacing:1px;font-size:10px;", text: "CLAUDE CODE CLI" }));
  content.append(claudeEps.length === 0 ? el("div", { class: "meta", text: "None" }) : el("div", {}, claudeEps.map(endpointRow)));
  content.append(el("div", { class: "meta", style: "margin-top:16px;color:var(--text-faint);letter-spacing:1px;font-size:10px;", text: "LOCAL" }));
  content.append(local.length === 0 ? el("div", { class: "meta", text: "None" }) : el("div", {}, local.map(endpointRow)));
  content.append(el("div", { class: "meta", style: "margin-top:16px;color:var(--text-faint);letter-spacing:1px;font-size:10px;", text: "API" }));
  content.append(apiEps.length === 0 ? el("div", { class: "meta", text: "None" }) : el("div", {}, apiEps.map(endpointRow)));

  probeAllBtn.addEventListener("click", () => {
    for (const btn of Object.values(rowsByEndpoint)) btn.click();
  });
}

// -- Integrations (David's ask, 2026-08-31, matching a real Odysseus
// screenshot of their "Add Integration" dropdown). Follow-up ask same day:
// add Calendar/Contacts/Email too — CalDAV/CardDAV built for real
// (core/dav_client.py, one-way read sync), Email reuses the existing Email
// tab's account management rather than duplicating it. Claude/Codex Agent
// still aren't offered — no foundation (see core/integrations.py docstring).
const INTEGRATION_KIND_LABELS = {
  api_service: "API Service",
  mcp_server: "MCP Tool Server",
  caldav_calendar: "CalDAV Calendar",
  carddav_contacts: "Contacts (CardDAV)",
  ical_feed: "iCal Feed",
};

// One-line icon SVGs so the table has something to sit in the icon column,
// same "hand-written stroke icon, no icon-font/CDN" convention as icons.js —
// generic per-kind glyphs (Claude's own table uses real per-service logos,
// which would mean either faking brand icons for services we don't actually
// connect to, or fetching real ones; a generic glyph per kind is the honest
// version of that column).
const CONNECTOR_ICONS = {
  api_service: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
  mcp_server: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6"/><path d="M15 2v6"/><path d="M12 17v5"/><path d="M6 8h12a2 2 0 0 1 2 2v2a6 6 0 0 1-6 6h-4a6 6 0 0 1-6-6v-2a2 2 0 0 1 2-2z"/></svg>',
  caldav_calendar: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>',
  ical_feed: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/><path d="M8 15h.01M12 15h.01M16 15h.01"/></svg>',
  carddav_contacts: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>',
};

function connectorIsConnected(item) {
  if (item.kind === "api_service") return item.has_api_key || !!item.base_url;
  if (item.kind === "mcp_server") return true; // exists = registered = usable
  return item.last_synced_count != null; // dav/ical: connected once it's synced at least once
}

let connectorsFilter = "all";
let connectorsQuery = "";

async function renderIntegrationsPanel(content) {
  const items = await api("/api/integrations");
  content.innerHTML = "";

  const addBtn = el("button", { class: "btn", text: "+ Add Integration" });
  const menu = el("div", { class: "overflow-menu below hidden", style: "min-width:200px;" });
  const apiItem = el("button", { type: "button", class: "overflow-menu-item", text: "API Service" });
  const caldavItem = el("button", { type: "button", class: "overflow-menu-item", text: "CalDAV Calendar" });
  const icalItem = el("button", { type: "button", class: "overflow-menu-item", text: "iCal Feed" });
  const carddavItem = el("button", { type: "button", class: "overflow-menu-item", text: "Contacts (CardDAV)" });
  const emailItem = el("button", { type: "button", class: "overflow-menu-item", text: "Email (IMAP/SMTP)" });
  const mcpItem = el("button", { type: "button", class: "overflow-menu-item", text: "MCP Tool Server" });
  menu.append(apiItem, caldavItem, icalItem, carddavItem, emailItem, mcpItem);
  const addWrap = el("div", { style: "position:relative;" }, [addBtn, menu]);
  addBtn.addEventListener("click", (e) => { e.stopPropagation(); menu.classList.toggle("hidden"); });
  document.addEventListener("click", () => menu.classList.add("hidden"));

  const formHost = el("div", { style: "margin-top:14px;" });
  apiItem.addEventListener("click", () => { menu.classList.add("hidden"); renderApiServiceForm(formHost, content); });
  mcpItem.addEventListener("click", () => { menu.classList.add("hidden"); renderMcpServerForm(formHost, content); });
  caldavItem.addEventListener("click", () => { menu.classList.add("hidden"); renderDavForm(formHost, content, "caldav_calendar"); });
  carddavItem.addEventListener("click", () => { menu.classList.add("hidden"); renderDavForm(formHost, content, "carddav_contacts"); });
  icalItem.addEventListener("click", () => { menu.classList.add("hidden"); renderIcalForm(formHost, content); });
  emailItem.addEventListener("click", () => {
    // Email already has full account management as its own tab — jump
    // there instead of duplicating it, matching the "don't build a second
    // Email" scoping note in core/integrations.py.
    menu.classList.add("hidden");
    closeSettingsWindow();
    const emailNav = document.querySelector('.nav-item[data-tab="email"]');
    if (emailNav) emailNav.click();
  });

  content.append(
    el("div", { class: "card-row", style: "justify-content:space-between;align-items:flex-start;" }, [
      el("div", { class: "title", text: "Integrations" }),
      addWrap,
    ]),
  );

  // -- Popular (David's ask 2026-08-31, live-tested before building): only
  // GitHub is offered here. Its real remote MCP server's automatic OAuth
  // (dynamic client registration) genuinely fails — confirmed live:
  // "Incompatible auth server: does not support dynamic client
  // registration" — but GitHub separately documents a Personal Access
  // Token fallback that works with the exact bearer-token field our MCP
  // Tool Server type already has. Slack/Gmail/Calendar/Drive hit the same
  // DCR failure live-tested and have no token fallback — real OAuth app
  // registration, not built — so they aren't offered here as fake "Connect"
  // buttons.
  const githubItemExists = items.some((i) => i.kind === "mcp_server" && i.url === "https://api.githubcopilot.com/mcp/");
  if (!githubItemExists) {
    const patInput = el("input", { type: "password", placeholder: "GitHub Personal Access Token", style: "flex:1;display:none;" });
    const connectBtn = el("button", { class: "btn", text: "Connect" });
    const err = el("div", { class: "meta", style: "color:var(--danger);" });
    connectBtn.addEventListener("click", async () => {
      if (patInput.style.display === "none") {
        patInput.style.display = "";
        patInput.focus();
        connectBtn.textContent = "Save";
        return;
      }
      if (!patInput.value.trim()) { err.textContent = "A token is required."; return; }
      try {
        await api("/api/integrations/mcp-server", {
          method: "POST",
          body: JSON.stringify({ name: "GitHub", mcp_type: "http", url: "https://api.githubcopilot.com/mcp/", api_key: patInput.value.trim() }),
        });
        await renderIntegrationsPanel(content);
      } catch (e) { err.textContent = e.message.replace(/^\d+: /, ""); }
    });
    content.append(
      el("div", { class: "title", style: "font-size:11px;color:var(--text-faint);letter-spacing:0.5px;margin-top:16px;", text: "POPULAR" }),
      el("div", { class: "glass bracket card", style: "margin-top:8px;" }, [
        el("div", { class: "card-row", style: "justify-content:space-between;align-items:center;" }, [
          el("div", {}, [
            el("div", { class: "title", style: "font-size:12.5px;", text: "GitHub" }),
            el("div", { class: "meta", style: "margin-top:2px;", text: "Real remote MCP server. Needs a Personal Access Token — GitHub Settings → Developer settings → Personal access tokens." }),
          ]),
          el("div", { class: "card-row", style: "gap:8px;" }, [patInput, connectBtn]),
        ]),
        err,
      ]),
    );
  }

  // -- Claude-Connectors-style toolbar: search + All/Connected/Not connected --
  const searchInput = el("input", { class: "connectors-search", placeholder: "Search integrations...", value: connectorsQuery });
  const tabs = el("div", { class: "segmented-tabs" });
  for (const [id, label] of [["all", "All"], ["connected", "Connected"], ["not_connected", "Not connected"]]) {
    const tab = el("button", { type: "button", class: "segmented-tab" + (connectorsFilter === id ? " active" : ""), text: label });
    tab.addEventListener("click", () => { connectorsFilter = id; renderIntegrationsPanel(content); });
    tabs.appendChild(tab);
  }
  searchInput.addEventListener("input", () => { connectorsQuery = searchInput.value; renderIntegrationsPanel(content); });
  content.append(el("div", { class: "connectors-toolbar" }, [searchInput, tabs]));

  const filtered = items.filter((item) => {
    if (connectorsQuery && !item.name.toLowerCase().includes(connectorsQuery.toLowerCase())) return false;
    const connected = connectorIsConnected(item);
    if (connectorsFilter === "connected" && !connected) return false;
    if (connectorsFilter === "not_connected" && connected) return false;
    return true;
  });

  if (items.length === 0) {
    content.append(el("div", { class: "empty-state", style: "margin-top:14px;", text: "No integrations configured" }));
  } else if (filtered.length === 0) {
    content.append(el("div", { class: "empty-state", style: "margin-top:14px;", text: "No integrations match." }));
  } else {
    const table = el("table", { class: "connectors-table" });
    table.appendChild(el("tr", {}, [
      el("th", { text: "Connector" }),
      el("th", { text: "Type" }),
      el("th", { text: "Status" }),
      el("th", { text: "" }),
    ]));
    for (const item of filtered) {
      const connected = connectorIsConnected(item);
      const icon = el("div", { class: "connectors-icon" });
      icon.innerHTML = CONNECTOR_ICONS[item.kind] || "";

      const delBtn = el("button", { class: "btn danger", text: "Remove" });
      delBtn.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: "Remove this integration?",
          message: `"${item.name}" will be disconnected and its stored credentials deleted.`,
          confirmLabel: "Remove integration",
        });
        if (!ok) return;
        await api(`/api/integrations/${item.id}`, { method: "DELETE" });
        await renderIntegrationsPanel(content);
        toast("Integration removed", "success");
      });
      const actions = [delBtn];
      if (item.kind === "caldav_calendar" || item.kind === "carddav_contacts" || item.kind === "ical_feed") {
        const syncBtn = el("button", { class: "btn", text: "Sync now" });
        syncBtn.addEventListener("click", async () => {
          syncBtn.textContent = "Syncing...";
          try {
            await api(`/api/integrations/${item.id}/sync`, { method: "POST" });
            toast(`${item.name} synced`, "success");
          } catch (e) { toast(e.message.replace(/^\d+: /, ""), "error"); }
          await renderIntegrationsPanel(content);
        });
        actions.unshift(syncBtn);
      }

      const row = el("tr", {}, [
        el("td", {}, [el("div", { class: "connectors-name-cell" }, [icon, el("span", { text: item.name })])]),
        el("td", { class: "meta", text: INTEGRATION_KIND_LABELS[item.kind] }),
        el("td", {}, [
          connected
            ? el("span", { class: "connectors-status-ok", text: "✓ Connected" })
            : el("span", { class: "connectors-status-off", text: "Not connected" }),
        ]),
        el("td", {}, [el("div", { class: "card-row", style: "gap:6px;justify-content:flex-end;" }, actions)]),
      ]);
      table.appendChild(row);
    }
    content.appendChild(table);

    const contacts = await api("/api/integrations/contacts");
    if (contacts.length > 0) {
      content.append(
        el("div", { class: "title", style: "font-size:12.5px;margin-top:18px;", text: "Synced contacts" }),
        el("div", { class: "meta", style: "margin:4px 0 8px;", text: "No dedicated Contacts tab yet — viewable here for now." }),
      );
      const contactList = el("div", {});
      for (const c of contacts) {
        contactList.appendChild(el("div", { class: "card-row", style: "margin-top:4px;" }, [
          el("span", { class: "meta", style: "color:var(--text);", text: c.name }),
          el("span", { class: "meta", text: [c.email, c.phone].filter(Boolean).join(" · ") }),
        ]));
      }
      content.appendChild(contactList);
    }
  }

  content.appendChild(formHost);
}

function renderDavForm(host, content, kind) {
  host.innerHTML = "";
  const isCal = kind === "caldav_calendar";
  const nameInput = el("input", { placeholder: "Name", style: "flex:1;" });
  const urlInput = el("input", { placeholder: isCal ? "Calendar collection URL" : "Address book collection URL", style: "flex:1;" });
  const userInput = el("input", { placeholder: "Username", style: "flex:1;" });
  const passInput = el("input", { type: "password", placeholder: "Password", style: "flex:1;" });
  const err = el("div", { class: "meta", style: "color:var(--danger);" });
  const saveBtn = el("button", { class: "btn", text: "Add & Sync" });
  saveBtn.addEventListener("click", async () => {
    err.textContent = "";
    if (!nameInput.value.trim() || !urlInput.value.trim() || !userInput.value.trim() || !passInput.value) {
      err.textContent = "All fields are required.";
      return;
    }
    saveBtn.disabled = true; saveBtn.textContent = "Syncing...";
    try {
      await api(`/api/integrations/${isCal ? "caldav" : "carddav"}`, {
        method: "POST",
        body: JSON.stringify({ name: nameInput.value.trim(), url: urlInput.value.trim(), username: userInput.value.trim(), password: passInput.value }),
      });
      await renderIntegrationsPanel(content);
    } catch (e) {
      err.textContent = e.message.replace(/^\d+: /, "");
      saveBtn.disabled = false; saveBtn.textContent = "Add & Sync";
    }
  });
  host.append(
    el("div", { class: "glass bracket card" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: isCal ? "Add CalDAV Calendar" : "Add Contacts (CardDAV)" }),
      el("div", { class: "meta", style: "margin:4px 0 8px;", text: "One-way read sync (remote → JARVIS), no writeback. Point this at a specific calendar/address-book collection URL, not the server root." }),
      el("div", { class: "card-row", style: "flex-wrap:wrap;gap:8px;" }, [nameInput, urlInput, userInput, passInput, saveBtn]),
      err,
    ]),
  );
}

function renderIcalForm(host, content) {
  host.innerHTML = "";
  const nameInput = el("input", { placeholder: "Name", style: "flex:1;" });
  const urlInput = el("input", { placeholder: "https://... or webcal://... .ics feed URL", style: "flex:1;" });
  const userInput = el("input", { placeholder: "Username (only if the feed needs it)", style: "flex:1;" });
  const passInput = el("input", { type: "password", placeholder: "Password (optional)", style: "flex:1;" });
  const err = el("div", { class: "meta", style: "color:var(--danger);" });
  const saveBtn = el("button", { class: "btn", text: "Add & Sync" });
  saveBtn.addEventListener("click", async () => {
    err.textContent = "";
    if (!nameInput.value.trim() || !urlInput.value.trim()) {
      err.textContent = "Name and URL are required.";
      return;
    }
    saveBtn.disabled = true; saveBtn.textContent = "Syncing...";
    try {
      await api("/api/integrations/ical", {
        method: "POST",
        body: JSON.stringify({
          name: nameInput.value.trim(),
          url: urlInput.value.trim(),
          username: userInput.value.trim() || null,
          password: passInput.value || null,
        }),
      });
      await renderIntegrationsPanel(content);
    } catch (e) {
      err.textContent = e.message.replace(/^\d+: /, "");
      saveBtn.disabled = false; saveBtn.textContent = "Add & Sync";
    }
  });
  host.append(
    el("div", { class: "glass bracket card" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: "Add iCal Feed" }),
      el("div", { class: "meta", style: "margin:4px 0 8px;", text: "A single .ics subscription URL (Google Calendar's \"secret address in iCal format,\" an Apple share link, etc.) — one-way read sync, no auth needed for most public feeds." }),
      el("div", { class: "card-row", style: "flex-wrap:wrap;gap:8px;" }, [nameInput, urlInput, userInput, passInput, saveBtn]),
      err,
    ]),
  );
}

function renderApiServiceForm(host, content) {
  host.innerHTML = "";
  const nameInput = el("input", { placeholder: "Name", style: "flex:1;" });
  const urlInput = el("input", { placeholder: "Base URL", style: "flex:1;" });
  const keyInput = el("input", { type: "password", placeholder: "API key (optional)", style: "flex:1;" });
  const err = el("div", { class: "meta", style: "color:var(--danger);" });
  const saveBtn = el("button", { class: "btn", text: "Add" });
  saveBtn.addEventListener("click", async () => {
    err.textContent = "";
    if (!nameInput.value.trim() || !urlInput.value.trim()) { err.textContent = "Name and URL are required."; return; }
    try {
      await api("/api/integrations/api-service", { method: "POST", body: JSON.stringify({ name: nameInput.value.trim(), base_url: urlInput.value.trim(), api_key: keyInput.value.trim() || null }) });
      await renderIntegrationsPanel(content);
    } catch (e) { err.textContent = e.message.replace(/^\d+: /, ""); }
  });
  host.append(
    el("div", { class: "glass bracket card" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: "Add API Service" }),
      el("div", { class: "card-row", style: "flex-wrap:wrap;gap:8px;margin-top:8px;" }, [nameInput, urlInput, keyInput, saveBtn]),
      err,
    ]),
  );
}

function renderMcpServerForm(host, content) {
  host.innerHTML = "";
  const nameInput = el("input", { placeholder: "Name", style: "flex:1;" });
  const typeSelect = customSelect({ style: "flex:0 0 110px;" }, [el("option", { value: "stdio", text: "stdio" }), el("option", { value: "http", text: "http" })]);
  const cmdInput = el("input", { placeholder: "Command (e.g. npx @scope/server)", style: "flex:1;" });
  const urlInput = el("input", { placeholder: "URL (e.g. https://example.com/mcp)", style: "flex:1;display:none;" });
  const keyInput = el("input", { type: "password", placeholder: "Auth token (optional)", style: "flex:1;" });
  const err = el("div", { class: "meta", style: "color:var(--danger);" });
  const saveBtn = el("button", { class: "btn", text: "Add" });

  typeSelect.addEventListener("change", () => {
    const isStdio = typeSelect.value === "stdio";
    cmdInput.style.display = isStdio ? "" : "none";
    urlInput.style.display = isStdio ? "none" : "";
  });

  saveBtn.addEventListener("click", async () => {
    err.textContent = "";
    if (!nameInput.value.trim()) { err.textContent = "Name is required."; return; }
    const isStdio = typeSelect.value === "stdio";
    if (isStdio && !cmdInput.value.trim()) { err.textContent = "Command is required for a stdio server."; return; }
    if (!isStdio && !urlInput.value.trim()) { err.textContent = "URL is required for an http server."; return; }
    try {
      const parts = cmdInput.value.trim().split(/\s+/);
      await api("/api/integrations/mcp-server", {
        method: "POST",
        body: JSON.stringify({
          name: nameInput.value.trim(),
          mcp_type: typeSelect.value,
          command: isStdio ? parts[0] : null,
          args: isStdio ? parts.slice(1) : null,
          url: isStdio ? null : urlInput.value.trim(),
          api_key: keyInput.value.trim() || null,
        }),
      });
      await renderIntegrationsPanel(content);
    } catch (e) { err.textContent = e.message.replace(/^\d+: /, ""); }
  });

  host.append(
    el("div", { class: "glass bracket card" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: "Add MCP Tool Server" }),
      el("div", { class: "meta", style: "margin:4px 0 8px;", text: "Registered servers widen the agent's real tool access on its next new/reconnected chat." }),
      el("div", { class: "card-row", style: "flex-wrap:wrap;gap:8px;" }, [nameInput, typeSelect, cmdInput, urlInput, keyInput, saveBtn]),
      err,
    ]),
  );
}

// -- Vault --------------------------------------------------------------
async function renderVaultPanel(content) {
  const settings = await api("/api/settings");
  content.innerHTML = "";
  const vaultPathEl = el("div", { class: "meta", text: settings.vault_dir });
  const pickBtn = el("button", { class: "btn", text: "Choose Folder..." });
  if (!window.jarvis) {
    pickBtn.disabled = true;
    pickBtn.title = "Folder picking is only available in the desktop app";
  }
  pickBtn.addEventListener("click", async () => {
    const picked = await window.jarvis.pickVaultFolder();
    if (!picked) return;
    await api("/api/settings/vault-dir", { method: "POST", body: JSON.stringify({ path: picked }) });
    await renderVaultPanel(content);
  });

  // Vault <-> Notes sync (David's ask 2026-09-03 — the app answered "no
  // priorities" while the connected vault was full of them). Runs
  // automatically at launch; this is the manual re-run for when the vault
  // has been edited in Obsidian while the app is open.
  const syncStatus = el("div", { class: "meta", style: "margin-top:8px;" });
  const syncBtn = el("button", { class: "btn", text: "Sync now" });
  syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    syncStatus.textContent = "Syncing…";
    try {
      const r = await api("/api/vault/sync", { method: "POST" });
      const parts = [];
      if (r.imported) parts.push(`${r.imported} added`);
      if (r.updated) parts.push(`${r.updated} updated`);
      if (r.removed) parts.push(`${r.removed} removed`);
      syncStatus.textContent = parts.length ? `Synced — ${parts.join(", ")}.` : "Already up to date.";
      toast(parts.length ? `Vault synced — ${parts.join(", ")}` : "Notes already match your vault", "success");
    } catch (e) {
      syncStatus.textContent = "";
    } finally {
      syncBtn.disabled = false;
    }
  });

  content.append(
    el("div", { class: "title", text: "Vault" }),
    el("div", { class: "card-row", style: "justify-content:space-between;align-items:center;margin-top:10px;" }, [vaultPathEl, pickBtn]),
    el("div", { class: "meta", style: "margin-top:10px;", text: "Point JARVIS at an existing vault on this device, or leave the default. Open chats keep using their old vault until reconnected." }),
    el("div", { class: "glass card", style: "margin-top:16px;" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: "Task list sync" }),
      el("div", { class: "meta", style: "margin:4px 0 10px;line-height:1.6;", text: "Checkbox items in your vault's Active Priorities.md show up as Notes, so asking JARVIS about your priorities returns what's actually in your vault. Ticking one here ticks it there too. This runs automatically every time JARVIS starts." }),
      el("div", { class: "card-row", style: "gap:8px;" }, [syncBtn, syncStatus]),
    ]),
  );
}

// -- Channels (David's ask 2026-08-31: renamed from a Discord-only panel to
// a real place to add secondary comms channels — Discord is the only real
// one today, core/channels/registry.py is built so a second channel is one
// more list entry, not a rewrite, rather than faking options that don't work).
// -- Remote Access (David's ask 2026-09-03: users should be able to set up
// Tailscale remote access themselves during setup, the way we run it by
// hand). Deliberately a checklist rather than one "Enable" button that
// either works or doesn't: every prerequisite (Tailscale installed, signed
// in, an account created, HTTPS certs available for the tailnet) is a
// separate thing the user might be missing, and a single opaque failure
// gives them nothing to act on. See core/remote_access.py.
async function renderRemotePanel(content) {
  content.innerHTML = "";
  content.appendChild(el("div", { class: "title", text: "Remote Access" }));
  content.appendChild(el("div", {
    class: "meta", style: "margin:4px 0 16px;line-height:1.6;",
    text: "Reach this JARVIS from your phone or another computer over Tailscale, a private network between your own devices. Nothing is exposed to the public internet — only devices signed into your Tailscale account can connect.",
  }));

  const body = el("div");
  content.appendChild(body);

  async function refresh() {
    body.innerHTML = "";
    let s;
    try {
      s = await api("/api/remote/status");
    } catch (e) {
      body.appendChild(el("div", { class: "meta", style: "color:var(--danger);", text: `Couldn't read status: ${e.message}` }));
      return;
    }

    // Live state first, when it's on. Fall back to composing the address
    // from host+port rather than rendering a blank line, in case the URL
    // wasn't captured (e.g. the listener was restored on startup).
    if (s.running_now) {
      s.url = s.url || (s.hostname ? `https://${s.hostname}:${s.port}` : null);
    }
    if (s.running_now && s.url) {
      const urlRow = el("div", { class: "glass bracket card", style: "margin-bottom:14px;" }, [
        el("div", { class: "card-row" }, [
          el("div", { style: "flex:1;min-width:0;" }, [
            el("div", { class: "title", style: "color:var(--accent);", text: "Remote access is on" }),
            el("div", { class: "meta", style: "margin-top:4px;", text: "Open this exact address from any device signed into your Tailscale account. Include the port — the hostname on its own won't reach it." }),
            el("div", { style: "margin-top:8px;font-size:14px;color:var(--text);word-break:break-all;", text: s.url }),
            el("div", { class: "meta", style: "margin-top:6px;color:var(--text-faint);", text: `Host ${s.hostname || "—"} · port ${s.port}` }),
          ]),
          el("div", { class: "card-row", style: "gap:6px;" }, [
            el("button", { class: "btn", text: "Copy link", onclick: async () => {
              await navigator.clipboard.writeText(s.url);
              toast("Link copied", "success");
            }}),
            el("button", { class: "btn danger", text: "Turn off", onclick: async () => {
              await api("/api/remote/disable", { method: "POST" });
              toast("Remote access turned off", "success");
              await refresh();
            }}),
          ]),
        ]),
      ]);
      body.appendChild(urlRow);
      return;
    }

    // Otherwise: the checklist.
    const steps = [
      {
        ok: s.installed,
        label: "Tailscale installed",
        hint: s.installed ? "Found on this machine." : "Tailscale is a free private network for your own devices. Install it, then come back here.",
        action: s.installed ? null : { label: "Get Tailscale", href: "https://tailscale.com/download" },
      },
      {
        ok: s.logged_in,
        label: "Signed in to Tailscale",
        // Deliberately does NOT print the bare hostname here. It used to,
        // and it read like the address to visit — but without the scheme
        // and port it doesn't work, which is exactly the confusion David
        // hit (2026-09-03). The full address gets its own row below.
        hint: s.logged_in
          ? `Connected as ${s.hostname ? s.hostname.split(".")[0] : "this machine"}.`
          : "Open the Tailscale app and sign in, then refresh below.",
      },
      {
        ok: s.auth_ready,
        label: "JARVIS account created",
        hint: s.auth_ready
          ? "A login is set up."
          : "Remote access needs a real login — without one, anyone reaching this machine on your network would get straight in.",
      },
    ];

    for (const step of steps) {
      const row = el("div", { class: "card-row", style: "align-items:flex-start;gap:10px;padding:10px 0;border-top:1px solid var(--border);" }, [
        el("div", { class: "card-row", style: "align-items:flex-start;gap:10px;flex:1;" }, [
          el("span", { class: `status-dot ${step.ok ? "ok" : "warn"}`, style: "margin-top:5px;" }),
          el("div", {}, [
            el("div", { style: "font-size:13px;color:var(--text);", text: step.label }),
            el("div", { class: "meta", style: "margin-top:3px;line-height:1.5;", text: step.hint }),
          ]),
        ]),
        step.action
          ? el("a", { href: step.action.href, target: "_blank", rel: "noopener", class: "btn", style: "text-decoration:none;flex-shrink:0;", text: step.action.label })
          : el("div"),
      ]);
      body.appendChild(row);
    }

    // Inline first-account creation, so the user doesn't have to go find
    // two unrelated settings before the toggle will work.
    if (!s.auth_ready) {
      const userInput = el("input", { placeholder: "Username", autocomplete: "off" });
      const passInput = el("input", { type: "password", placeholder: "Password (8+ characters)", autocomplete: "new-password" });
      const createBtn = el("button", { class: "btn", text: "Create account" });
      createBtn.addEventListener("click", async () => {
        if (!userInput.value.trim() || !passInput.value) {
          toast("Enter a username and password", "error");
          return;
        }
        createBtn.disabled = true;
        try {
          await api("/api/remote/create-account", {
            method: "POST",
            body: JSON.stringify({ username: userInput.value.trim(), password: passInput.value }),
          });
          toast("Account created — you'll sign in with this from now on", "success");
          await refresh();
        } finally {
          createBtn.disabled = false;
        }
      });
      body.appendChild(el("div", { class: "glass card", style: "margin-top:14px;" }, [
        el("div", { class: "title", style: "font-size:12.5px;", text: "Create your login" }),
        el("div", { class: "meta", style: "margin:4px 0 10px;line-height:1.5;", text: "This turns on accounts for JARVIS everywhere, including on this computer — so you'll sign in here too. That's deliberate: it's the same app either way." }),
        el("div", { class: "form-grid" }, [
          el("div", { class: "field field-grow" }, [el("label", { text: "Username" }), userInput]),
          el("div", { class: "field field-grow" }, [el("label", { text: "Password" }), passInput]),
          createBtn,
        ]),
      ]));
    }

    // The address, always visible once Tailscale knows this machine's name —
    // not only after remote access is switched on. David hit this live
    // (2026-09-03): the panel named the machine but never the port, so the
    // address it implied didn't actually work. Port is editable here too,
    // since it's part of the address you have to type.
    const portInput = el("input", { type: "number", value: String(s.port), style: "width:100px;" });
    const addressEl = el("div", {
      style: "margin-top:6px;font-size:13px;color:var(--text);word-break:break-all;",
    });
    const portStatus = el("span", { class: "meta" });
    const syncAddress = () => {
      const port = parseInt(portInput.value, 10) || s.port;
      addressEl.textContent = s.hostname ? `https://${s.hostname}:${port}` : "—";
    };
    syncAddress();
    portInput.addEventListener("input", () => { syncAddress(); portStatus.textContent = ""; });

    // The port is a real setting, so editing it saves — it used to only take
    // effect as a side effect of pressing Enable, so a change made on its own
    // silently reverted on the next refresh (David hit this 2026-09-03).
    // Saves on blur/Enter rather than per keystroke, so typing "8" of "8443"
    // doesn't try to bind port 8.
    const savePort = async () => {
      const port = parseInt(portInput.value, 10);
      if (!port || port === s.port) return;
      portStatus.textContent = "Saving…";
      try {
        const r = await api("/api/remote/port", { method: "POST", body: JSON.stringify({ port }) });
        s.port = port;
        portStatus.textContent = r.restarted ? "Saved — listener moved" : "Saved";
        toast(r.restarted ? `Remote access moved to port ${port}` : `Port set to ${port}`, "success");
      } catch (e) {
        portStatus.textContent = "";
        portInput.value = String(s.port);  // failed to bind — show the port actually in use
        syncAddress();
      }
    };
    portInput.addEventListener("blur", savePort);
    portInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); portInput.blur(); } });

    if (s.hostname) {
      body.appendChild(el("div", { class: "glass card", style: "margin-top:16px;" }, [
        el("div", { class: "card-row", style: "align-items:flex-start;" }, [
          el("div", { style: "flex:1;min-width:0;" }, [
            el("div", { class: "title", style: "font-size:12.5px;", text: "Address" }),
            el("div", { class: "meta", style: "margin-top:4px;", text: "Open this from any device signed into your Tailscale account. The port is part of it — the hostname alone won't work." }),
            addressEl,
          ]),
          el("div", { class: "card-row", style: "gap:6px;align-items:flex-end;" }, [
            el("div", { class: "field field-sm" }, [
              el("label", { text: "Port" }), portInput, portStatus,
            ]),
            el("button", { class: "btn", text: "Copy", onclick: async () => {
              await navigator.clipboard.writeText(addressEl.textContent);
              toast("Address copied", "success");
            }}),
          ]),
        ]),
      ]));
    }

    const allReady = s.installed && s.logged_in && s.auth_ready;
    const enableBtn = el("button", {
      class: "btn",
      text: "Turn on remote access",
      disabled: !allReady,
      title: allReady ? "" : "Finish the steps above first",
    });
    enableBtn.addEventListener("click", async () => {
      enableBtn.disabled = true;
      enableBtn.textContent = "Setting up…";
      try {
        const port = parseInt(portInput.value, 10) || undefined;
        await api("/api/remote/enable", { method: "POST", body: JSON.stringify({ port }) });
        toast("Remote access is on", "success");
        await refresh();
      } catch (e) {
        // api() already toasted the real reason (cert not available for the
        // tailnet, port in use, etc.) — just restore the button.
        enableBtn.disabled = false;
        enableBtn.textContent = "Turn on remote access";
      }
    });

    body.appendChild(el("div", { class: "card-row", style: "margin-top:16px;gap:8px;" }, [
      enableBtn,
      el("button", { class: "btn", text: "Refresh", onclick: refresh }),
    ]));

    body.appendChild(el("div", {
      class: "meta", style: "margin-top:12px;line-height:1.6;",
      text: "The first time you turn this on, JARVIS asks Tailscale for an HTTPS certificate so your browser trusts the connection. If your tailnet doesn't have HTTPS certificates enabled yet, you'll be told exactly where to switch them on.",
    }));
  }

  await refresh();
}

async function renderChannelsPanel(content) {
  content.innerHTML = "";
  content.append(
    el("div", { class: "title", text: "Channels" }),
    el("div", { class: "meta", style: "margin:4px 0 14px;", text: "Secondary ways to reach JARVIS, and where scheduled task output (Tasks tab) can be delivered. Discord is the only real channel today." }),
  );

  // Real setup guide (David's ask 2026-09-01) — a bot token isn't something
  // most people have lying around; point at the actual place to get one
  // instead of assuming they already know.
  content.append(
    el("div", { class: "glass card", style: "margin-bottom:14px;" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: "Setting up a Discord bot" }),
      el("div", { class: "meta", style: "margin-top:6px;line-height:1.6;" }, [
        "1. Go to the ",
        el("a", { href: "https://discord.com/developers/applications", target: "_blank", rel: "noopener", style: "color:var(--accent);", text: "Discord Developer Portal" }),
        " and create a New Application.",
        el("br"),
        "2. Open Bot in the sidebar, click Reset Token, and copy it — that's the token you paste below.",
        el("br"),
        "3. Under Privileged Gateway Intents, enable Message Content Intent (JARVIS needs this to read messages).",
        el("br"),
        "4. Under OAuth2 > URL Generator, check \"bot\", give it Send Messages + Read Message History, then open the generated URL to invite it to your server.",
      ]),
    ]),
  );

  const [bots, models] = await Promise.all([
    api("/api/settings/discord-bots"),
    api("/api/models"),
  ]);

  const modelOptions = (selectedId) => [
    el("option", { value: "" }, "No default model"),
    ...models.map((m) => el("option", { value: m.id, ...(m.id === selectedId ? { selected: "" } : {}) },
      m.kind === "claude_cli" ? `${m.name} (${m.model || "CLI default"})` : `${m.name} (${m.model})`)),
  ];

  const botsSection = el("div", {});
  content.append(botsSection);

  function renderBots() {
    botsSection.innerHTML = "";
    if (bots.length === 0) {
      botsSection.appendChild(el("div", { class: "empty-state", text: "No bots connected yet." }));
    }
    for (const bot of bots) {
      const modelSelect = customSelect({ style: "flex:1;" }, modelOptions(bot.model_endpoint_id));
      const saveBtn = el("button", { class: "btn", text: "Save" });
      const delBtn = el("button", { class: "btn danger", text: "Remove" });
      saveBtn.addEventListener("click", async () => {
        await api(`/api/settings/discord-bots/${bot.id}`, {
          method: "PATCH",
          body: JSON.stringify({ name: bot.name, allowed_user_id: bot.allowed_user_id, model_endpoint_id: modelSelect.value || null }),
        });
        await refresh();
      });
      delBtn.addEventListener("click", async () => {
        const ok = await confirmDialog({
          title: "Remove this bot?",
          message: `"${bot.name}" will be disconnected from Discord and its token deleted. Any task delivering to Discord will stop reaching you.`,
          confirmLabel: "Remove bot",
        });
        if (!ok) return;
        await api(`/api/settings/discord-bots/${bot.id}`, { method: "DELETE" });
        await refresh();
        toast("Bot removed", "success");
      });
      botsSection.appendChild(
        el("div", { class: "glass bracket card", style: "margin-bottom:8px;" }, [
          el("div", { class: "card-row" }, [
            el("div", {}, [
              el("div", { class: "title", style: "font-size:12.5px;", text: bot.name }),
              el("div", { class: "meta", text: bot.allowed_user_id ? `Allowlisted to user ${bot.allowed_user_id}` : "Replies to anyone in its channels" }),
            ]),
            el("div", { class: "card-row", style: "gap:6px;" }, [delBtn]),
          ]),
          el("div", { class: "card-row", style: "margin-top:8px;gap:8px;" }, [
            el("div", { class: "meta", style: "flex-shrink:0;", text: "Default model:" }),
            modelSelect, saveBtn,
          ]),
        ]),
      );
    }
  }
  renderBots();

  async function refresh() {
    const fresh = await api("/api/settings/discord-bots");
    bots.length = 0;
    bots.push(...fresh);
    renderBots();
  }

  // -- Add a bot ------------------------------------------------------
  const nameInput = el("input", { placeholder: "Name (e.g. \"JARVIS\")", style: "flex:1;" });
  const tokenInput = el("input", { type: "password", placeholder: "Bot token", style: "flex:1;" });
  const allowedInput = el("input", { placeholder: "Allowed Discord user ID (optional)", style: "flex:1;" });
  const addModelSelect = customSelect({ style: "flex:1;" }, modelOptions(null));
  const addErr = el("div", { class: "meta", style: "color:var(--danger);" });
  const addBtn = el("button", { class: "btn", text: "+ Add Bot" });
  addBtn.addEventListener("click", async () => {
    addErr.textContent = "";
    if (!nameInput.value.trim() || !tokenInput.value.trim()) {
      addErr.textContent = "Name and token are required.";
      return;
    }
    try {
      await api("/api/settings/discord-bots", {
        method: "POST",
        body: JSON.stringify({
          name: nameInput.value.trim(),
          token: tokenInput.value.trim(),
          allowed_user_id: allowedInput.value.trim() || null,
          model_endpoint_id: addModelSelect.value || null,
        }),
      });
      nameInput.value = ""; tokenInput.value = ""; allowedInput.value = ""; addModelSelect.value = "";
      await refresh();
    } catch (e) { addErr.textContent = e.message.replace(/^\d+: /, ""); }
  });

  content.append(
    el("div", { class: "glass bracket card", style: "margin-top:14px;" }, [
      el("div", { class: "title", style: "font-size:12.5px;", text: "Add a Discord bot" }),
      el("div", { class: "card-row", style: "flex-wrap:wrap;gap:8px;margin-top:10px;" }, [nameInput, tokenInput, allowedInput, addModelSelect, addBtn]),
      addErr,
      el("div", { class: "meta", style: "margin-top:10px;", text: "Adding/editing/removing a bot restarts the Discord connection immediately. Task delivery DMs the allowed user ID of whichever bot has one set." }),
    ]),
  );
}

// -- Account --------------------------------------------------------------
async function renderAccountPanel(content, status) {
  content.innerHTML = "";
  if (!status.auth_enabled) {
    content.append(
      el("div", { class: "title", text: "Account" }),
      el("div", { class: "meta", style: "margin-top:10px;", text: "Auth is off (single trusted local user) — nothing to manage here. Enable it via AUTH_ENABLED for a real login." }),
    );
    return;
  }

  const err = el("div", { class: "meta", style: "color: var(--danger); min-height: 16px;" });
  const okMsg = el("div", { class: "meta", style: "color: var(--accent); min-height: 16px;" });

  const curPass = el("input", { type: "password", placeholder: "Current password", style: "flex:1;" });
  const newPass = el("input", { type: "password", placeholder: "New password", style: "flex:1;" });
  const changeBtn = el("button", { class: "btn", text: "Change password" });
  changeBtn.addEventListener("click", async () => {
    err.textContent = ""; okMsg.textContent = "";
    try {
      await api("/api/auth/password", { method: "POST", body: JSON.stringify({ current_password: curPass.value, new_password: newPass.value }) });
      curPass.value = ""; newPass.value = "";
      okMsg.textContent = "Password changed.";
    } catch (e) { err.textContent = e.message.replace(/^\d+: /, ""); }
  });

  const totpSection = el("div", { style: "margin-top:14px;" });
  await renderTotpSection(totpSection);

  content.append(
    el("div", { class: "title", text: "Account" }),
    el("div", { class: "meta", style: "margin-top:4px;", text: `Signed in as ${status.username}${status.is_admin ? " (admin)" : ""}` }),
    el("div", { class: "card-row", style: "flex-wrap:wrap;gap:8px;margin-top:10px;" }, [curPass, newPass, changeBtn]),
    err, okMsg,
    totpSection,
  );
}

async function renderTotpSection(host) {
  host.innerHTML = "";
  const enrollBtn = el("button", { class: "btn", text: "Enable 2FA" });
  const disableBtn = el("button", { class: "btn danger", text: "Disable 2FA" });
  const uriBox = el("div", { class: "meta", style: "word-break:break-all;margin:8px 0;display:none;" });
  const codeInput = el("input", { placeholder: "6-digit code", style: "width:140px;display:none;" });
  const confirmBtn = el("button", { class: "btn", text: "Confirm", style: "display:none;" });
  const msg = el("div", { class: "meta" });

  enrollBtn.addEventListener("click", async () => {
    const res = await api("/api/auth/totp/enroll", { method: "POST" });
    uriBox.textContent = res.provisioning_uri;
    uriBox.style.display = "";
    codeInput.style.display = "";
    confirmBtn.style.display = "";
    msg.textContent = "Add this to your authenticator app (or paste the URI manually), then enter the 6-digit code.";
  });
  confirmBtn.addEventListener("click", async () => {
    try {
      await api("/api/auth/totp/confirm", { method: "POST", body: JSON.stringify({ code: codeInput.value.trim() }) });
      msg.textContent = "2FA enabled.";
      uriBox.style.display = "none"; codeInput.style.display = "none"; confirmBtn.style.display = "none";
    } catch (e) { msg.textContent = e.message.replace(/^\d+: /, ""); }
  });
  disableBtn.addEventListener("click", async () => {
    await api("/api/auth/totp/disable", { method: "POST" });
    msg.textContent = "2FA disabled.";
  });

  host.append(
    el("div", { class: "title", style: "font-size:12.5px;", text: "Two-factor authentication" }),
    el("div", { class: "card-row", style: "gap:8px;margin-top:6px;" }, [enrollBtn, disableBtn]),
    uriBox, codeInput, confirmBtn, msg,
  );
}

// -- Shortcuts --------------------------------------------------------------
function renderShortcutsPanel(content) {
  content.innerHTML = "";
  content.appendChild(el("div", { class: "title", text: "Shortcuts" }));
  const shortcuts = [
    ["Enter", "Send message"],
    ["Shift + Enter", "New line in composer"],
    ["Right-click a chat", "Rename / star / delete"],
    ["/help in Chat", "List all slash commands"],
  ];
  for (const [key, desc] of shortcuts) {
    content.appendChild(el("div", { class: "card-row", style: "margin-top:10px;" }, [
      el("span", { class: "meta", style: "font-family:monospace;color:var(--accent);", text: key }),
      el("span", { class: "meta", text: desc }),
    ]));
  }
}

// -- Admin: Agent Tools -------------------------------------------------------
async function renderAgentToolsPanel(content) {
  const data = await api("/api/settings/agent-tools");
  content.innerHTML = "";
  content.append(
    el("div", { class: "title", text: "Agent Tools" }),
    el("div", { class: "meta", style: "margin-top:6px;", text: "Globally disable tools for every new chat session. Takes effect on the next new/reconnected session." }),
  );
  const list = el("div", { style: "margin-top:12px;display:flex;flex-wrap:wrap;gap:14px;" });
  const checks = {};
  for (const tool of data.available) {
    const cb = el("input", { type: "checkbox" });
    cb.checked = !data.disabled.includes(tool);
    checks[tool] = cb;
    list.appendChild(el("label", { style: "display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--text-dim);" }, [cb, el("span", { text: tool })]));
  }
  const saveBtn = el("button", { class: "btn", text: "Save", style: "margin-top:14px;" });
  saveBtn.addEventListener("click", async () => {
    const disabled_tools = Object.entries(checks).filter(([, cb]) => !cb.checked).map(([tool]) => tool);
    await api("/api/settings/agent-tools", { method: "POST", body: JSON.stringify({ disabled_tools }) });
  });
  content.append(list, saveBtn);
}

// -- Admin: Users -------------------------------------------------------------
async function renderUsersPanel(content, status) {
  const users = await api("/api/auth/users");
  content.innerHTML = "";
  content.append(el("div", { class: "title", text: "Users" }));

  if (!status.auth_enabled) {
    content.append(el("div", { class: "meta", style: "margin-top:10px;", text: "Auth is off — user management needs AUTH_ENABLED." }));
    return;
  }

  const list = el("div", { style: "margin-top:10px;" });
  for (const u of users) {
    const adminToggle = el("button", { class: "btn", text: u.is_admin ? "Demote" : "Promote" });
    adminToggle.addEventListener("click", async () => {
      try {
        await api(`/api/auth/users/${u.username}/admin`, { method: "POST", body: JSON.stringify({ is_admin: !u.is_admin }) });
        await renderUsersPanel(content, status);
        toast(`${u.username} ${u.is_admin ? "demoted" : "promoted"}`, "success");
      } catch (e) { toast(e.message.replace(/^\d+: /, ""), "error"); }
    });
    const delBtn = el("button", { class: "btn danger", text: "Delete" });
    delBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Delete user "${u.username}"?`,
        message: "This account will be permanently removed and can no longer sign in.",
        confirmLabel: "Delete user",
      });
      if (!ok) return;
      try {
        await api(`/api/auth/users/${u.username}`, { method: "DELETE" });
        await renderUsersPanel(content, status);
        toast(`User ${u.username} deleted`, "success");
      } catch (e) { toast(e.message.replace(/^\d+: /, ""), "error"); }
    });
    if (u.username === status.username) { delBtn.disabled = true; delBtn.title = "Can't delete the account you're logged in as"; }

    list.appendChild(el("div", { class: "card-row", style: "justify-content:space-between;margin-top:8px;" }, [
      el("span", { class: "meta", text: `${u.username}${u.is_admin ? " (admin)" : ""}${u.totp_enabled ? " · 2FA" : ""}` }),
      el("div", { class: "card-row", style: "gap:6px;" }, [adminToggle, delBtn]),
    ]));
  }
  content.appendChild(list);

  const newUser = el("input", { placeholder: "Username", style: "flex:1;" });
  const newPass = el("input", { type: "password", placeholder: "Password", style: "flex:1;" });
  const newAdmin = el("input", { type: "checkbox" });
  const addBtn = el("button", { class: "btn", text: "Add user" });
  const err = el("div", { class: "meta", style: "color:var(--danger);" });
  addBtn.addEventListener("click", async () => {
    err.textContent = "";
    try {
      await api("/api/auth/users", { method: "POST", body: JSON.stringify({ username: newUser.value.trim(), password: newPass.value, is_admin: newAdmin.checked }) });
      newUser.value = ""; newPass.value = ""; newAdmin.checked = false;
      await renderUsersPanel(content, status);
    } catch (e) { err.textContent = e.message.replace(/^\d+: /, ""); }
  });
  content.append(
    el("div", { class: "card-row", style: "flex-wrap:wrap;gap:8px;margin-top:16px;" }, [
      newUser, newPass,
      el("label", { style: "display:flex;align-items:center;gap:5px;font-size:12px;color:var(--text-dim);" }, [newAdmin, el("span", { text: "admin" })]),
      addBtn,
    ]),
    err,
  );
}

// -- Admin: System (diagnostics, backup, wipe) -------------------------------
async function renderSystemPanel(content) {
  const diag = await api("/api/system/diagnostics");
  content.innerHTML = "";
  content.append(el("div", { class: "title", text: "System" }));

  const diagGrid = el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;margin-top:10px;" });
  const rows = [
    ["Vault", diag.vault_exists ? diag.vault_dir : "missing"],
    ["Chats", diag.sessions_count],
    ["Notes", diag.notes_count],
    ["Tasks", diag.tasks_count],
    ["Skills", diag.skills_count],
    ["Model endpoints", diag.model_endpoints_count],
    ["Data on disk", `${(diag.data_dir_bytes / 1024).toFixed(1)} KB`],
    ["Discord", diag.discord_configured ? "configured" : "not configured"],
  ];
  for (const [label, value] of rows) {
    diagGrid.append(el("span", { class: "meta", text: label }), el("span", { class: "meta", style: "color:var(--text);", text: String(value) }));
  }
  content.appendChild(diagGrid);

  const exportBtn = el("button", { class: "btn", text: "Export backup" });
  const importInput = el("input", { type: "file", accept: "application/json", style: "display:none;" });
  const importBtn = el("button", { class: "btn", text: "Import backup..." });
  const backupMsg = el("div", { class: "meta" });
  exportBtn.addEventListener("click", async () => {
    const data = await api("/api/system/backup/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = el("a", { href: url, download: `jarvis-backup-${new Date().toISOString().slice(0, 10)}.json` });
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  });
  importBtn.addEventListener("click", () => importInput.click());
  importInput.addEventListener("change", async () => {
    const file = importInput.files[0];
    importInput.value = "";
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const res = await api("/api/system/backup/import", { method: "POST", body: JSON.stringify({ data }) });
      backupMsg.textContent = `Imported: ${res.settings} settings, ${res.notes} notes, ${res.skills} skills.`;
      await renderSystemPanel(content);
    } catch (e) { backupMsg.textContent = `Import failed: ${e.message}`; }
  });
  content.append(
    el("div", { class: "card-row", style: "gap:8px;margin-top:16px;" }, [exportBtn, importBtn, importInput]),
    backupMsg,
  );

  const wipeRow = el("div", { style: "margin-top:18px;" });
  wipeRow.append(el("div", { class: "meta", style: "color:var(--danger);margin-bottom:8px;", text: "Danger zone — permanent, no undo." }));
  const wipeBtns = el("div", { style: "display:flex;flex-wrap:wrap;gap:8px;" });
  for (const kind of ["chats", "notes", "tasks", "skills"]) {
    const btn = el("button", { class: "btn danger", text: `Wipe ${kind}` });
    btn.addEventListener("click", async () => {
      // Kept as a genuine two-step confirmation (this wipes a whole domain
      // globally, for every user) — just no longer native OS dialogs.
      const first = await confirmDialog({
        title: `Permanently delete all ${kind}?`,
        message: `Every one of your ${kind} will be erased. This cannot be undone.`,
        confirmLabel: `Wipe all ${kind}`,
      });
      if (!first) return;
      const second = await confirmDialog({
        title: "Are you absolutely sure?",
        message: `This wipes ${kind} for every user of this install, globally. There is no backup and no undo.`,
        confirmLabel: `Yes, wipe ${kind}`,
      });
      if (!second) return;
      await api("/api/system/wipe", { method: "POST", body: JSON.stringify({ kind }) });
      await renderSystemPanel(content);
      toast(`All ${kind} wiped`, "success");
    });
    wipeBtns.appendChild(btn);
  }
  wipeRow.appendChild(wipeBtns);
  content.appendChild(wipeRow);
}

// -- Admin: Custom Tabs (Developer Mode, David's ask 2026-09-01) ------------
async function renderCustomTabsPanel(content) {
  const tabs = await api("/api/system/custom-tabs");
  content.innerHTML = "";
  content.append(
    el("div", { class: "title", text: "Custom Tabs" }),
    el("div", { class: "meta", style: "margin-top:6px;", text: "Reorder, keep building, or delete tabs your AI model has built." }),
  );

  if (tabs.length === 0) {
    content.append(el("div", { class: "meta", style: "margin-top:10px;", text: "No custom tabs yet — use \"+ New Tab\" in the sidebar." }));
    return;
  }

  // Real bug found live 2026-09-02: "Keep Building" created a session with
  // no model_endpoint_id, so it silently did nothing but return the canned
  // "you haven't added a model yet" reply. One shared picker for the whole
  // panel — every "Keep Building" click uses whichever model is selected
  // here, same reasoning new-tab.js's own per-build picker fixes for the
  // main "+ New Tab" flow.
  const endpoints = await api("/api/models").catch(() => []);
  const modelOptions = endpoints.map((ep) => el("option", { value: ep.id, text: modelLabel(ep) }));
  const modelSelect = endpoints.length ? customSelect({ style: "min-width:220px;" }, modelOptions) : null;
  content.append(
    el("div", { class: "card-row", style: "margin-top:12px;gap:8px;align-items:center;" }, [
      el("span", { class: "meta", text: "Build with:" }),
      modelSelect || el("span", { class: "meta", style: "color:var(--danger);", text: "No models added — see Settings > Add Models" }),
    ]),
  );

  const list = el("div", { style: "margin-top:12px;" });
  tabs.forEach((tab, i) => {
    const upBtn = el("button", { class: "btn", text: "↑", title: "Move up" });
    const downBtn = el("button", { class: "btn", text: "↓", title: "Move down" });
    upBtn.disabled = i === 0;
    downBtn.disabled = i === tabs.length - 1;
    const reorder = async (delta) => {
      const order = tabs.map((t) => t.id);
      const j = i + delta;
      [order[i], order[j]] = [order[j], order[i]];
      await api("/api/system/custom-tabs/order", { method: "POST", body: JSON.stringify({ order }) });
      await renderCustomTabsPanel(content);
    };
    upBtn.addEventListener("click", () => reorder(-1));
    downBtn.addEventListener("click", () => reorder(1));

    const keepBuildingBtn = el("button", { class: "btn", text: "Keep Building" });
    if (!modelSelect) { keepBuildingBtn.disabled = true; keepBuildingBtn.title = "Add a model first — see Settings > Add Models."; }
    keepBuildingBtn.addEventListener("click", async () => {
      // Real bug found live 2026-09-02: this used to send a message ending
      // mid-sentence ("...then make this change: ") with nothing after the
      // colon — no actual request, just an incomplete prompt. Ask first.
      const change = prompt(`What do you want to change or add to the "${tab.label}" tab?`);
      if (!change || !change.trim()) return;
      const message = [
        `I want to keep working on the "${tab.label}" tab.`,
        "",
        `Its files are routes/tab_${tab.id}.py, static/js/views/${tab.id}.js, and services/${tab.id}_service.py if it has one. Read the current files first.`,
        "",
        `What I want changed: ${change.trim()}`,
      ].join("\n");
      const session = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) });
      await api(`/api/sessions/${session.id}/model`, { method: "POST", body: JSON.stringify({ model_endpoint_id: modelSelect.value }) });
      sessionStorage.setItem("jarvis:pendingChatHandoff", JSON.stringify({ sessionId: session.id, message }));
      closeSettingsWindow();
      const navItem = document.querySelector('.nav-item[data-tab="chat"]');
      if (navItem) navItem.click();
    });

    const deleteBtn = el("button", { class: "btn danger", text: "Delete" });
    deleteBtn.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Delete the "${tab.label}" tab?`,
        message: "This removes the tab's files from disk. A restart is needed for its API routes to fully unmount.",
        confirmLabel: "Delete tab",
      });
      if (!ok) return;
      await api(`/api/system/custom-tabs/${tab.id}`, { method: "DELETE" });
      await renderCustomTabsPanel(content);
      toast(`Tab "${tab.label}" deleted`, "success");
    });

    list.appendChild(
      el("div", { class: "card-row", style: "justify-content:space-between;margin-top:8px;" }, [
        el("span", { class: "meta", style: "color:var(--text);" }, [
          el("span", { style: "display:inline-flex;width:16px;height:16px;vertical-align:middle;margin-right:6px;" }),
          document.createTextNode(tab.label),
        ]),
        el("div", { class: "card-row", style: "gap:6px;" }, [upBtn, downBtn, keepBuildingBtn, deleteBtn]),
      ]),
    );
    // Inline SVG icon into the placeholder span just appended (el() only
    // takes text/children, not a raw HTML fragment inside another element).
    list.lastChild.querySelector("span[style*='inline-flex']").innerHTML = tab.icon_svg || "";
  });
  content.appendChild(list);
}
