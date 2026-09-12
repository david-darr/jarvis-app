import { api, el } from "../api.js";
import { ICONS } from "../icons.js";
import { mount as mountCore } from "../core3d.js";
import { listInFlight, subscribeAll } from "../chatStream.js";

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

// Return cleanup synchronously: navigating away during a pending request
// releases the scene immediately. Home reads cheap summary APIs only.
export function render(container) {
  container.replaceChildren();
  container.classList.add("dashboard");
  let disposed = false, refreshing = false, systemStatus = null, nextTask = null;
  const statusLabel = el("span", { text: "Checking system" });
  const statusDot = el("span", { class: "status-dot" });
  const coreHost = el("div", { class: "core-stage", "aria-label": "JARVIS particle core" });
  const coreState = el("span", { class: "core-state", text: "Connecting" });
  const coreToggle = el("button", { class: "core-motion-toggle", type: "button", text: "Pause motion", "aria-pressed": "false" });
  const nextTaskLabel = el("span", { class: "dashboard-next", text: "Checking schedule…" });
  const content = el("div", { class: "dashboard-content" });
  const header = el("header", { class: "dashboard-header" }, [
    el("div", {}, [el("div", { class: "eyebrow", text: "Overview" }), el("p", { text: new Date().toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" }) })]),
    el("div", { class: "dashboard-system-pill", role: "status" }, [statusDot, statusLabel]),
  ]);
  const hero = el("section", { class: "dashboard-hero" }, [
    el("div", { class: "dashboard-intro" }, [
      el("div", { class: "eyebrow", text: "A little space to think" }),
      el("h1", { text: "Your day, in focus." }),
      el("p", { text: "Your conversations, knowledge, and next steps. All connected, right here." }),
      el("div", { class: "dashboard-actions" }, [action("Start a conversation", "chat", true), action("Explore your vault ↗", "brain", false, { section: "vault" })]),
      nextTaskLabel,
    ]),
    el("div", { class: "dashboard-core" }, [coreHost, el("div", { class: "core-caption" }, [coreState, coreToggle])]),
  ]);
  const stats = el("div", { class: "dashboard-stats" });
  const chats = section("Pick up where you left off", "All chats ↗", "chat");
  const schedule = section("On the horizon", "Calendar ↗", "calendar");
  const projects = section("Your projects", "Open projects ↗", "chat");
  const activity = section("Recent activity", "Tasks ↗", "tasks");
  const system = section("Connected systems", "Settings ↗", "settings");
  const models = section("Your models", "Manage ↗", "settings");
  content.append(header, hero, stats, el("div", { class: "dashboard-grid" }, [chats.panel, schedule.panel, projects.panel, models.panel, activity.panel, system.panel]));
  container.appendChild(content);
  const disposeCore = mountCore(coreHost);
  let paused = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function updateMotion() {
    coreToggle.textContent = paused ? "Resume motion" : "Pause motion";
    coreToggle.setAttribute("aria-pressed", String(paused));
    disposeCore.setPaused?.(paused);
  }
  coreToggle.addEventListener("click", () => { paused = !paused; updateMotion(); });
  updateMotion();
  function updateCoreState() {
    if (disposed) return;
    const running = listInFlight().filter((entry) => entry.status === "processing").length;
    const attention = systemStatus && (!systemStatus.vault_ok || !systemStatus.scheduler_running);
    const state = running ? "working" : attention ? "attention" : systemStatus ? "ready" : "offline";
    coreState.textContent = running ? running + " conversation" + (running === 1 ? "" : "s") + " in progress" : attention ? "System needs attention" : systemStatus ? "Ready when you are" : "Status unavailable";
    disposeCore.setState?.(state);
    coreHost.dataset.state = state;
  }
  const unsubscribe = subscribeAll(updateCoreState);
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
    updateCountdown(); updateCoreState();
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
      const pct = usage?.[endpoint.id]?.percentage;
      const modelRow = row(endpoint.name, endpoint.model || endpoint.kind.replaceAll("_", " "), "brain", () => navigate("settings"));
      if (typeof pct === "number" && Number.isFinite(pct)) modelRow.append(el("span", { class: "dashboard-model-usage", text: Math.round(pct) + "% used" }));
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
  }
  refresh();
  const refreshTimer = setInterval(() => { if (!document.hidden) refresh(); }, 30000);
  const countdownTimer = setInterval(updateCountdown, 1000);
  return () => { disposed = true; disposeCore(); unsubscribe(); clearInterval(refreshTimer); clearInterval(countdownTimer); };
}
