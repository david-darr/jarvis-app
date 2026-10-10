import { api, el, modelMark } from "../api.js";
import { openModelSetup } from "../modelSetup.js";
import { ICONS } from "../icons.js";
import { mountDither } from "../dither.js";
import { halftonePalette, halftoneSource, halftoneFocus, halftoneVersion } from "../appearance.js";
import { homeLayout } from "../layout.js";
import { SIGN_IN, toSnap, toneOf, usedCopy, pctText, resetCopy, ago, staleOf } from "../quotaReadings.js";

const navigate = (tab, options = {}) => document.dispatchEvent(new CustomEvent("jarvis:navigate", { detail: { tab, ...options } }));
function icon(name) {
  const node = el("span", { class: "dashboard-icon", "aria-hidden": "true" });
  node.innerHTML = ICONS[name] || ICONS.notes;
  return node;
}
function action(label, tab, primary = false, options = {}) {
  return el("button", { type: "button", class: primary ? "btn primary" : "btn quiet", text: label, onclick: () => navigate(tab, options) });
}
function section(title, link, tab) {
  const body = el("div", { class: "dashboard-section-body" });
  const arrow = action("↗", tab);
  arrow.title = link;
  arrow.setAttribute("aria-label", link);
  const panel = el("section", { class: "dashboard-section" }, [
    el("div", { class: "dashboard-section-header" }, [el("h2", { text: title }), arrow]), body,
  ]);
  return { panel, body };
}
function empty(body, message, failed = false) {
  body.replaceChildren(el("div", { class: "dashboard-empty", text: failed ? "Couldn't load this section. Try opening it again." : message }));
}
function row(title, detail, onclick) {
  return el(onclick ? "button" : "div", { class: "dashboard-row", ...(onclick ? { type: "button", onclick } : {}) }, [
    el("span", { class: "dashboard-row-title", text: title, title }),
    el("span", { class: "dashboard-row-detail", text: detail, title: detail }),
  ]);
}
function compactTokens(n) {
  if (n >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

// What a model row may honestly claim (2026-09-22). This used to show each
// model's share of all Kairos-recorded tokens labelled "% used", which read
// as a quota and was inflated by cache reads. A subscription CLI's real usage
// comes only from the provider's own limits, so those rows show nothing here
// until that source is wired in (the parked desktop usage overlay reads it).
// Everything else shows tokens actually spent through Kairos, with cache
// reuse - cheap re-reads of a prompt the provider already holds - kept apart.
function usageLabel(endpoint, usage) {
  if (!usage || endpoint.kind === "claude_cli" || endpoint.kind === "codex_cli") return null;
  const spent = usage.fresh_tokens + usage.unsplit_tokens;
  if (!spent) return null;
  const cached = usage.cache_read_tokens ? ` · ${compactTokens(usage.cache_read_tokens)} reused from cache` : "";
  return {
    text: `${compactTokens(spent)} tokens via Kairos${cached}`,
    title: "Tokens this Kairos install sent and received through this model: new input, cache writes and output. Not a quota.",
  };
}

// A subscription's account limits, as the desktop overlay shows them (David,
// 2026-10-07): each window's bar, how much is used and left, and when it
// resets; or why there's no reading. Shared wording: quotaReadings.js.
const QUOTA_PROVIDER = { claude_cli: "claude", codex_cli: "codex" };
function quotaBlock(providerId, reading) {
  const snap = toSnap(reading);
  const block = el("div", { class: "dashboard-quota", "data-provider": providerId });
  if (staleOf(snap) && snap.fetched_at) block.append(el("span", { class: "dashboard-quota-note", text: "Updated " + ago(snap.fetched_at) }));
  if (snap.status === "needsAuth") {
    block.append(el("span", { class: "dashboard-quota-note", text: [SIGN_IN[providerId], snap.note].filter(Boolean).join(" ") }));
    return block;
  }
  if (!snap.windows.length) {
    block.append(el("span", { class: "dashboard-quota-note", text: snap.note || "Waiting for the first reading…" }));
    return block;
  }
  for (const w of snap.windows) {
    const percent = Math.min(w.used, 1) * 100;
    block.append(el("div", { class: "dashboard-quota-window" }, [
      el("div", { class: "dashboard-quota-head", title: usedCopy(w) }, [el("span", { text: `${w.id === "session" ? "5h" : w.id === "weekly" ? "Week" : w.label} · ${pctText(w.used)}%` }), el("span", { class: "dashboard-quota-reset", text: resetCopy(w.resets_at).replace(/^Resets at /, "resets ").replace(/^Resets/, "resets") })]),
      el("div", { class: "dashboard-quota-track", role: "meter", "aria-label": w.label, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(percent)) }, [
        el("span", { class: "dashboard-quota-fill tone-" + toneOf(w.used), style: `width:${percent.toFixed(0)}%` }),
      ]),
    ]));
  }
  return block;
}

function relativeTime(timestamp) {
  const minutes = Math.max(0, Math.floor((Date.now() - timestamp * 1000) / 60000));
  if (minutes < 1) return "Just now";
  if (minutes < 60) return minutes + "m ago";
  if (minutes < 1440) return Math.floor(minutes / 60) + "h ago";
  return Math.floor(minutes / 1440) + "d ago";
}
// Date-only calendar entries are local days, not UTC instants.
function eventDate(value) {
  return new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? value + "T00:00:00" : value);
}

// Counts a stat's number up from zero over 600 ms, easing out.
function countUp(node) {
  const target = Number(node.textContent);
  if (!Number.isFinite(target) || target <= 0) return;
  const started = performance.now();
  const step = (now) => {
    if (!node.isConnected) return;
    const t = Math.min(1, (now - started) / 600);
    node.textContent = String(Math.round(target * (1 - (1 - t) ** 3)));
    if (t < 1) requestAnimationFrame(step);
  };
  node.textContent = "0";
  requestAnimationFrame(step);
}

// Return cleanup synchronously: navigating away during a pending request
// releases the scene immediately. Home reads cheap summary APIs only.
export function render(container, tabId, options = {}) {
  container.replaceChildren();
  container.classList.add("dashboard");
  let disposed = false, refreshing = false, nextTask = null;
  const statusLabel = el("span", { text: "Checking system" });
  const statusDot = el("span", { class: "status-dot" });
  // The banner: the Kairos figure as halftone (BRAND.md 6.8), still, with
  // the text over a parchment wash on its left.
  const banner = el("div", { class: "dashboard-core", "aria-hidden": "true" });
  const nextTaskLabel = el("span", { class: "dashboard-next", text: "Checking schedule…" });
  const content = el("div", { class: "dashboard-content" });
  const header = el("header", { class: "dashboard-header" }, [
    el("div", {}, [el("div", { class: "eyebrow", text: "Overview" }), el("p", { text: new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" }) })]),
    el("div", { class: "dashboard-system-pill", role: "status" }, [statusDot, statusLabel]),
  ]);
  const hero = el("section", { class: "dashboard-hero" }, [
    banner,
    header,
    el("div", { class: "dashboard-intro" }, [
      el("div", { class: "eyebrow", text: "Kairos, the opportune moment" }),
      el("h1", { class: "k-display" }, ["Not more time. ", el("span", { text: "The right time." })]),
      el("p", { text: "Your conversations, notes and next steps, ready for the moment you need them." }),
      el("div", { class: "dashboard-actions" }, [action("Start a conversation", "chat", true), action("Explore your vault ↗", "library", false, { section: "vault" })]),
      nextTaskLabel,
    ]),
  ]);
  const stats = el("div", { class: "dashboard-stats" });
  const chats = section("Recent chats", "All chats", "chat");
  const schedule = section("Upcoming", "Calendar", "calendar");
  const projects = section("Projects", "Open projects", "chat");
  const activity = section("Activity", "Tasks", "tasks");
  const system = section("Systems", "Settings", "settings");
  const models = section("Models", "Manage models", "settings");
  const agents = section("Agents", "Agents", "agents");
  const store = section("Tool Store", "Tool Store", "tool-store");
  agents.panel.hidden = store.panel.hidden = true;
  system.panel.classList.add("dashboard-wide");
  // Order and visibility below the card: Settings > Layout (layout.js). The
  // numbers strip spans the grid; hidden parts still refresh, unseen.
  const parts = { stats, chats: chats.panel, schedule: schedule.panel, agents: agents.panel, store: store.panel, projects: projects.panel, models: models.panel, activity: activity.panel, system: system.panel };
  for (const [id, panel] of Object.entries(parts)) panel.dataset.homeSection = id;
  const grid = el("div", { class: "dashboard-grid" });
  const arrange = () => {
    const { order, hidden } = homeLayout();
    grid.replaceChildren(...order.filter((id) => !hidden.includes(id)).map((id) => parts[id]));
  };
  arrange();
  document.addEventListener("kairos:layout", arrange);
  content.append(hero, grid);
  container.appendChild(content);
  // Chat to Home (app.js switchTab) waits for the banner before animating
  // into it. It's drawn from the Home halftone slot (a custom picture, or
  // the Kairos figure), in the appearance's halftone colors, and redrawn
  // when any of those change.
  const bannerKey = () => JSON.stringify({ src: halftoneSource("home"), focus: halftoneFocus("home"), palette: halftonePalette() });
  const bannerOptions = () => {
    const focus = halftoneFocus("home");
    return { cell: 4, fade: [0.86, 1.0], focusX: focus ? focus.x : 0.7, focusY: focus ? focus.y : 0.4,
      palette: halftonePalette(), version: halftoneVersion("home") };
  };
  let bannerState = bannerKey();
  banner.dataset.halftoneSrc = halftoneSource("home");  // read by scripts/ui-smoke.cjs
  let disposeBanner = mountDither(banner, halftoneSource("home"), { ...bannerOptions(), onReady: options.transitionReady });
  const onAppearance = () => {
    const next = bannerKey();
    if (next === bannerState || disposed) return;
    bannerState = next;
    banner.dataset.halftoneSrc = halftoneSource("home");
    disposeBanner();
    disposeBanner = mountDither(banner, halftoneSource("home"), bannerOptions());
  };
  document.addEventListener("kairos:appearance", onAppearance);
  for (const item of [chats, schedule, projects, activity, system, models, agents, store]) {
    item.body.append(el("div", { class: "skeleton skeleton-line" }), el("div", { class: "skeleton skeleton-line" }));
  }
  function updateCountdown() {
    if (!nextTask) return;
    const minutes = Math.ceil((new Date(nextTask.next_run_at) - Date.now()) / 60000);
    const time = minutes <= 0 ? "due now" : minutes < 60 ? "in " + minutes + "m" : minutes < 1440 ? "in " + Math.floor(minutes / 60) + "h " + minutes % 60 + "m" : "in " + Math.floor(minutes / 1440) + "d";
    nextTaskLabel.textContent = "Next up: " + nextTask.name + " · " + time;
  }
  // Each request fills its own card. Quota or store reads cannot hold up chats.
  const data = {};
  const statSpecs = [
    ["sessions", "chats", "chat"], ["notes", "open notes", "notes"],
    ["tasks", "automations on", "tasks"], ["events", "this week", "calendar"],
  ];
  const statValues = new Map();
  for (const [key, label, tab] of statSpecs) {
    const value = el("span", { class: "dashboard-stat-value", text: "…" });
    statValues.set(key, value);
    stats.append(el("button", { type: "button", class: "dashboard-stat", "data-stat": key, onclick: () => navigate(tab) }, [
      value, el("span", { class: "dashboard-stat-label", text: label }),
    ]));
  }
  const waiting = el("button", { type: "button", class: "dashboard-stat dashboard-attention", "data-stat": "waiting", hidden: true, onclick: () => navigate("agents") });
  stats.append(waiting);

  function drawModels() {
    if (data.endpoints === undefined) return;
    const { endpoints, usage, quotas } = data;
    models.body.replaceChildren();
    if (endpoints && !endpoints.length) models.body.append(el("div", { class: "model-setup-empty" }, [
      el("h3", { text: "Set up a model" }), el("p", { text: "Choose how Kairos thinks. We'll help you connect it." }),
      el("button", { class: "btn", text: "Set up a model", onclick: () => openModelSetup() }),
    ]));
    else if (endpoints === null) empty(models.body, "", true);
    else {
      const quotaShown = new Set();
      endpoints.forEach((endpoint) => {
        const block = el("div", { class: "dashboard-model" });
        const modelRow = el("button", { type: "button", class: "dashboard-row dashboard-model-top", onclick: () => navigate("settings") }, [
          modelMark(endpoint.mark, endpoint.name) || icon("brain"),
          el("span", { class: "dashboard-model-name", text: endpoint.name, title: endpoint.name }),
          el("span", { class: "dashboard-model-variant", text: endpoint.model || endpoint.kind.replaceAll("_", " ") }),
        ]);
        block.append(modelRow);
        const label = usageLabel(endpoint, usage?.[endpoint.id]);
        if (label) block.append(el("span", { class: "dashboard-model-usage", text: label.text, title: label.title }));
        const providerId = QUOTA_PROVIDER[endpoint.kind];
        const reading = providerId && quotas?.providers?.find((p) => p.provider === providerId);
        if (reading && !quotaShown.has(providerId)) {
          quotaShown.add(providerId);
          block.append(quotaBlock(providerId, reading));
        } else if (providerId && !quotaShown.has(providerId) && quotas !== undefined) {
          block.append(el("span", { class: "dashboard-quota-note", text: "Usage limits unavailable." }));
        }
        models.body.append(block);
      });
    }
  }

  function drawSystems() {
    if (data.status === undefined) return;
    const { status, settings } = data;
    system.body.replaceChildren();
    if (!status) return empty(system.body, "", true);
    const chips = el("div", { class: "dashboard-system-chips" });
    const values = [
      ["Vault", status.vault_ok ? "connected" : "unavailable", status.vault_ok ? "ok" : "err"],
      ["Scheduler", status.scheduler_running ? status.enabled_task_count + (status.enabled_task_count === 1 ? " task" : " tasks") : "stopped", status.scheduler_running ? "ok" : "err"],
      ["Discord", status.discord_connected_bots.length ? status.discord_connected_bots.length + " connected" : "off", status.discord_connected_bots.length ? "ok" : "warn"],
      ["Models", String(status.model_endpoint_count), status.model_endpoint_count ? "ok" : "warn"],
    ];
    if (settings?.computer_use && typeof settings.computer_use.enabled === "boolean") {
      values.push(["Computer use", settings.computer_use.enabled ? "on" : "off", settings.computer_use.enabled ? "ok" : "warn"]);
    }
    for (const [name, value, state] of values) chips.append(el("span", { class: "dashboard-system-chip" }, [
      el("span", { class: "status-dot " + state }), el("span", { text: name }), el("span", { class: "meta", text: value }),
    ]));
    system.body.append(chips);
  }

  function drawStore() {
    if (!data.auth?.is_admin) return;
    const { skills, integrations, tabs, catalog } = data;
    store.body.replaceChildren();
    const add = (title, detail) => store.body.append(row(title, detail, () => navigate("tool-store")));
    if (Array.isArray(skills) && Array.isArray(integrations) && Array.isArray(tabs)) {
      // Match Tool Store's installed entries: MCP servers, configured services,
      // and installed user tabs or enabled built-in templates.
      const tools = integrations.filter((i) => i.kind === "mcp_server" || i.configured);
      const installedTabs = tabs.filter((t) => t.kind === "user" || t.enabled);
      add("Installed", `${skills.length} skills · ${tools.length} tools · ${installedTabs.length} tabs`);
    }
    if (Array.isArray(catalog?.items)) {
      const updates = catalog.items.filter((i) => i.installed && i.update_available);
      if (updates.length) add("Update available", updates.length <= 2 ? updates.map((i) => i.name).join(", ") : `${updates.length} items`);
      add("New in the store", `${catalog.items.filter((i) => !i.installed && !i.install_blocked).length} items`);
    }
    // Shares require GitHub and have no local cache. Home never requests them.
    if (!store.body.childElementCount) {
      if ([skills, integrations, tabs, catalog].some((v) => v === null)) empty(store.body, "", true);
      else store.body.append(el("div", { class: "skeleton skeleton-line" }));
    }
    store.panel.hidden = false;
  }

  function draw(key, start) {
    const value = data[key];
    if (statValues.has(key)) {
      const n = value === null ? null : key === "tasks" ? value.filter((t) => t.enabled).length : value.length;
      const node = statValues.get(key);
      const first = node.textContent === "…";
      node.textContent = n === null ? "Unavailable" : String(n);
      if (first && !matchMedia("(prefers-reduced-motion: reduce)").matches) countUp(node);
    }
    if (key === "sessions") {
      chats.body.replaceChildren();
      if (!value?.length) empty(chats.body, "Your next conversation starts here.", value === null);
      else [...value].sort((a, b) => b.updated_at - a.updated_at).slice(0, 4).forEach((s) => chats.body.append(row(s.title || "Untitled conversation", relativeTime(s.updated_at), () => navigate("chat", { sessionId: s.id }))));
    }
    if (key === "events") {
      schedule.body.replaceChildren();
      const upcoming = value?.filter((e) => !e.completed && eventDate(e.end || e.start) >= (e.all_day ? start : new Date())).sort((a, b) => eventDate(a.start) - eventDate(b.start)).slice(0, 4);
      if (!upcoming?.length) empty(schedule.body, "A little breathing room. No upcoming events this week.", value === null);
      else upcoming.forEach((event) => {
        const date = eventDate(event.start);
        const short = date.toLocaleDateString([], { weekday: "short" }) + (event.all_day ? "" : " " + date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
        const item = row(event.title, "", () => navigate("calendar"));
        item.lastElementChild.remove();
        item.prepend(el("time", { class: "dashboard-event-date", datetime: event.start, text: short, title: date.toLocaleString() }));
        schedule.body.append(item);
      });
    }
    if (key === "projects") {
      projects.body.replaceChildren();
      if (!value?.length) empty(projects.body, "Group your chats and shared knowledge into a project.", value === null);
      else value.slice(0, 3).forEach((p) => projects.body.append(row(p.name, (p.document_ids?.length || 0) + " docs", () => navigate("chat", { projectId: p.id }))));
    }
    if (["endpoints", "usage", "quotas"].includes(key)) drawModels();
    if (key === "feed") {
      activity.body.replaceChildren();
      if (!value?.length) empty(activity.body, "Task runs and channel activity will appear here.", value === null);
      else value.forEach((e) => {
        const item = row(e.message, relativeTime(e.ts));
        item.classList.add("dashboard-activity");
        item.title = e.level + ": " + e.message;
        activity.body.append(item);
      });
    }
    if (key === "status") {
      statusDot.className = "status-dot " + (!value ? "warn" : !value.vault_ok || !value.scheduler_running ? "err" : "ok");
      statusLabel.textContent = !value ? "Status unavailable" : !value.vault_ok || !value.scheduler_running ? "Needs attention" : "Systems operational";
      nextTask = value?.next_task;
      nextTaskLabel.textContent = !value ? "Schedule unavailable" : nextTask ? "" : "No scheduled runs ahead";
      updateCountdown();
    }
    if (["status", "settings"].includes(key)) drawSystems();
    if (key === "agents") {
      agents.panel.hidden = !data.auth?.is_admin || (Array.isArray(value) && !value.length);
      agents.body.replaceChildren();
      if (value === null) empty(agents.body, "", true);
      else {
        const order = ["needs_you", "working", "idle", "capped", "off"];
        [...value].sort((a, b) => order.indexOf(a.status) - order.indexOf(b.status)).slice(0, 5).forEach((agent) => {
          const detail = agent.status === "working" ? agent.status_detail : agent.status === "needs_you" ? `${agent.needs_you} waiting on you` : agent.status === "idle" ? `Idle · ${agent.runs_today} runs today` : agent.status === "capped" ? `Paused: ${agent.status_detail}` : "Off";
          const item = row(agent.name, detail, () => navigate("agents", { agentId: agent.id }));
          item.prepend(el("span", { class: "dashboard-agent-dot " + agent.status, "aria-label": agent.status.replaceAll("_", " ") }));
          agents.body.append(item);
        });
      }
    }
    if (key === "inbox") {
      waiting.hidden = !data.auth?.is_admin || !(value?.count > 0);
      waiting.replaceChildren(el("span", { class: "dashboard-stat-value", text: String(value?.count || 0) }), el("span", { class: "dashboard-stat-label", text: "waiting on you" }));
    }
    if (["skills", "integrations", "tabs", "catalog"].includes(key)) drawStore();
    content.classList.add("is-filled");
  }

  async function refresh() {
    if (disposed || refreshing) return;
    refreshing = true;
    const start = new Date(); start.setHours(0, 0, 0, 0);
    const end = new Date(start); end.setDate(end.getDate() + 7);
    const request = async (key, path) => {
      const value = await api(path).catch(() => null);
      if (disposed) return;
      data[key] = value;
      draw(key, start);
    };
    const publicReads = [
      ["sessions", "/api/sessions"], ["notes", "/api/notes?include_completed=false"], ["tasks", "/api/tasks"],
      ["events", "/api/calendar/events?start=" + start.toISOString() + "&end=" + end.toISOString()],
      ["projects", "/api/projects"], ["endpoints", "/api/models"], ["usage", "/api/models/usage"],
      ["status", "/api/system/status"], ["feed", "/api/system/events?limit=5"], ["quotas", "/api/models/quotas"],
    ].map(([key, path]) => request(key, path));
    const adminReads = (async () => {
      const auth = await api("/api/auth/status").catch(() => null);
      if (disposed) return;
      data.auth = auth;
      if (!auth?.is_admin) {
        agents.panel.hidden = store.panel.hidden = waiting.hidden = true;
        delete data.settings;
        drawSystems();
        return;
      }
      await Promise.allSettled([
        ["agents", "/api/agents"], ["inbox", "/api/agents/inbox"], ["skills", "/api/skills"],
        ["integrations", "/api/integrations"], ["tabs", "/api/system/tabs"],
        ["catalog", "/api/store/catalog?cache_only=true"], ["settings", "/api/settings"],
      ].map(([key, path]) => request(key, path)));
    })();
    await Promise.allSettled([...publicReads, adminReads]);
    refreshing = false;
  }
  refresh();
  document.addEventListener("kairos:models-changed", refresh);
  const refreshTimer = setInterval(() => { if (!document.hidden) refresh(); }, 30000);
  const countdownTimer = setInterval(updateCountdown, 1000);
  return () => { disposed = true; disposeBanner(); document.removeEventListener("kairos:models-changed", refresh); document.removeEventListener("kairos:appearance", onAppearance); document.removeEventListener("kairos:layout", arrange); clearInterval(refreshTimer); clearInterval(countdownTimer); };
}
