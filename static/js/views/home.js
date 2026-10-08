import { api, el, modelMark } from "../api.js";
import { ICONS } from "../icons.js";
import { mountDither } from "../dither.js";

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
  const panel = el("section", { class: "dashboard-section" }, [
    el("div", { class: "dashboard-section-header" }, [el("h2", { text: title }), action(link, tab)]), body,
  ]);
  return { panel, body };
}
function empty(body, message, failed = false) {
  body.replaceChildren(el("div", { class: "dashboard-empty", text: failed ? "Couldn't load this section. Try opening it again." : message }));
}
function row(title, detail, iconName, onclick) {
  return el(onclick ? "button" : "div", { class: "dashboard-row", ...(onclick ? { type: "button", onclick } : {}) }, [
    icon(iconName), el("span", { class: "dashboard-row-copy" }, [
      el("span", { class: "dashboard-row-title", text: title }),
      el("span", { class: "dashboard-row-detail", text: detail }),
    ]), ...(onclick ? [el("span", { class: "dashboard-row-arrow", text: "↗", "aria-hidden": "true" })] : []),
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
  let disposed = false, refreshing = false, systemStatus = null, nextTask = null, filled = false;
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
  const chats = section("Pick up where you left off", "All chats ↗", "chat");
  const schedule = section("On the horizon", "Calendar ↗", "calendar");
  const projects = section("Your projects", "Open projects ↗", "chat");
  const activity = section("Recent activity", "Tasks ↗", "tasks");
  const system = section("Connected systems", "Settings ↗", "settings");
  const models = section("Your models", "Manage ↗", "settings");
  content.append(hero, stats, el("div", { class: "dashboard-grid" }, [chats.panel, schedule.panel, projects.panel, models.panel, activity.panel, system.panel]));
  container.appendChild(content);
  // Chat to Home (app.js switchTab) waits for the banner before animating into it.
  const disposeBanner = mountDither(banner, "/static/img/home-figure.webp", { cell: 4, fade: [0.86, 1.0], focusX: 0.7, focusY: 0.4, onReady: options.transitionReady });
  for (const item of [chats, schedule, projects, activity, system, models]) {
    item.body.append(el("div", { class: "skeleton skeleton-line" }), el("div", { class: "skeleton skeleton-line" }));
  }
  function updateCountdown() {
    if (!nextTask) return;
    const minutes = Math.ceil((new Date(nextTask.next_run_at) - Date.now()) / 60000);
    const time = minutes <= 0 ? "due now" : minutes < 60 ? "in " + minutes + "m" : minutes < 1440 ? "in " + Math.floor(minutes / 60) + "h " + minutes % 60 + "m" : "in " + Math.floor(minutes / 1440) + "d";
    nextTaskLabel.textContent = "Next up: " + nextTask.name + " · " + time;
  }
  async function refresh() {
    if (disposed || refreshing) return;
    refreshing = true;
    const start = new Date(); start.setHours(0, 0, 0, 0);
    const end = new Date(start); end.setDate(end.getDate() + 7);
    const paths = ["/api/sessions", "/api/notes?include_completed=false", "/api/tasks", "/api/calendar/events?start=" + start.toISOString() + "&end=" + end.toISOString(), "/api/projects", "/api/models", "/api/models/usage", "/api/system/status", "/api/system/events?limit=5"];
    const results = await Promise.allSettled(paths.map((path) => api(path)));
    refreshing = false;
    if (disposed) return;
    const [sessions, notes, tasks, events, projectList, endpoints, usage, status, feed] = results.map((result) => result.status === "fulfilled" ? result.value : null);
    systemStatus = status;
    statusDot.className = "status-dot " + (!status ? "warn" : !status.vault_ok || !status.scheduler_running ? "err" : "ok");
    statusLabel.textContent = !status ? "Status unavailable" : !status.vault_ok || !status.scheduler_running ? "Needs attention" : "Systems operational";
    nextTask = status?.next_task;
    nextTaskLabel.textContent = !status ? "Schedule unavailable" : nextTask ? "" : "No scheduled runs ahead";
    updateCountdown();
    stats.replaceChildren();
    for (const [label, value, tab, name] of [
      ["Conversations", sessions?.length, "chat", "chats"], ["Open notes", notes?.length, "notes", "notes"],
      ["Active automations", tasks?.filter((t) => t.enabled).length, "tasks", "tasks"], ["This week", events?.length, "calendar", "calendar"],
    ]) stats.append(el("button", { type: "button", class: "dashboard-stat", onclick: () => navigate(tab) }, [
      icon(name), el("span", { class: "dashboard-stat-value", text: value == null ? "—" : String(value) }), el("span", { class: "dashboard-stat-label", text: label }),
    ]));
    chats.body.replaceChildren();
    if (!sessions?.length) empty(chats.body, "Your next conversation starts here.", sessions === null);
    else [...sessions].sort((a, b) => b.updated_at - a.updated_at).slice(0, 4).forEach((s) => chats.body.append(row(s.title || "Untitled conversation", relativeTime(s.updated_at), "chats", () => navigate("chat", { sessionId: s.id }))));
    schedule.body.replaceChildren();
    const upcoming = events?.filter((e) => !e.completed && eventDate(e.end || e.start) >= (e.all_day ? start : new Date())).sort((a, b) => eventDate(a.start) - eventDate(b.start)).slice(0, 4);
    if (!upcoming?.length) empty(schedule.body, "A little breathing room. No upcoming events this week.", events === null);
    else upcoming.forEach((event) => schedule.body.append(row(event.title, eventDate(event.start).toLocaleString([], { weekday: "short", month: "short", day: "numeric", ...(event.all_day ? {} : { hour: "numeric", minute: "2-digit" }) }), "calendar", () => navigate("calendar"))));
    projects.body.replaceChildren();
    if (!projectList?.length) empty(projects.body, "Group your chats and shared knowledge into a project.", projectList === null);
    else projectList.slice(0, 3).forEach((p) => projects.body.append(row(p.name, (p.document_ids?.length || 0) + " shared documents", "library", () => navigate("chat", { projectId: p.id }))));
    models.body.replaceChildren();
    if (!endpoints?.length) empty(models.body, "Connect a model in Settings to get started.", endpoints === null);
    else endpoints.forEach((endpoint) => {
      const modelRow = row(endpoint.name, endpoint.model || endpoint.kind.replaceAll("_", " "), "brain", () => navigate("settings"));
      // The provider's own logo in place of the generic brain icon, when one is known
      const mark = modelMark(endpoint.mark, endpoint.name);
      if (mark) modelRow.firstElementChild.replaceWith(mark);
      const label = usageLabel(endpoint, usage?.[endpoint.id]);
      if (label) modelRow.append(el("span", { class: "dashboard-model-usage", text: label.text, title: label.title }));
      models.body.append(modelRow);
    });
    activity.body.replaceChildren();
    if (!feed?.length) empty(activity.body, "Task runs and channel activity will appear here.", feed === null);
    else feed.forEach((event) => activity.body.append(el("div", { class: "dashboard-activity" }, [
      el("span", { class: "status-dot " + (event.level === "error" ? "err" : event.level === "warn" ? "warn" : "ok") }),
      el("span", { text: event.message }), el("time", { text: relativeTime(event.ts) }),
    ])));
    system.body.replaceChildren();
    if (!status) empty(system.body, "", true);
    else for (const [label, detail, state] of [
      ["Memory vault", status.vault_ok ? "Connected" : "Unavailable", status.vault_ok ? "ok" : "err"],
      ["Scheduler", status.scheduler_running ? status.enabled_task_count + " active tasks" : "Stopped", status.scheduler_running ? "ok" : "err"],
      ["Discord", status.discord_connected_bots.length ? status.discord_connected_bots.length + " connected" : "Not connected", status.discord_connected_bots.length ? "ok" : "warn"],
      ["Model endpoints", status.model_endpoint_count + " configured", status.model_endpoint_count ? "ok" : "warn"],
    ]) system.body.append(el("div", { class: "dashboard-system-row" }, [el("span", { class: "status-dot " + state }), el("span", { text: label }), el("span", { class: "meta", text: detail })]));
    // The first fill arrives with a little motion (David, 2026-10-07): the
    // panels' contents fade in one after another (style.css .is-filled) and
    // the four numbers count up. The 30-second refreshes change in place.
    if (!filled) {
      filled = true;
      content.classList.add("is-filled");
      if (!matchMedia("(prefers-reduced-motion: reduce)").matches) stats.querySelectorAll(".dashboard-stat-value").forEach(countUp);
    }
  }
  refresh();
  const refreshTimer = setInterval(() => { if (!document.hidden) refresh(); }, 30000);
  const countdownTimer = setInterval(updateCountdown, 1000);
  return () => { disposed = true; disposeBanner(); clearInterval(refreshTimer); clearInterval(countdownTimer); };
}
