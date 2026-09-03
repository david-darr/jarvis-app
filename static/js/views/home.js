import { api, el } from "../api.js";
import { mount as mountCore } from "../core3d.js";

// Mission Control Home (David's ask 2026-09-02: "best possible mission
// control") — the 3D core stays the centerpiece; a left status column now
// shows live system health (/api/system/status: scheduler, Discord gateway,
// vault, models), a next-scheduled-run countdown, and a real activity feed
// (core/events.py via /api/system/events). Right column keeps This Week /
// Recent Chats / AI Models. Health + feed refresh every 30s while the tab
// is open; the countdown ticks every second — both cleaned up by the
// dispose function render() returns (same contract app.js already relies
// on for the WebGL scene).
//
// Dashboard summaries still pull cheap, already-loaded-elsewhere data only —
// email summary is account count, not a live IMAP fetch, and /status is
// documented as in-memory/local-file reads, so Home never blocks on a real
// network call just to render.

const STATUS_REFRESH_MS = 30_000;

export async function render(container) {
  container.innerHTML = "";
  container.classList.add("home-layout");

  const coreHost = el("div", { class: "core3d-host" });
  const title = el("div", { class: "home-title", style: "letter-spacing: 6px; color: var(--accent); font-weight: 300; margin: 10px 0 6px;", text: "JARVIS" });
  const sub = el("div", { class: "sub", text: "Mission Control" });
  const stats = el("div", { class: "home-stats" });
  const overlay = el("div", { class: "home-overlay" }, [title, sub, stats]);
  const sidePanel = el("div", { class: "home-side-panel" });
  const leftPanel = el("div", { class: "home-side-panel home-left-panel" });

  container.append(coreHost, overlay, leftPanel, sidePanel);

  const dispose3d = mountCore(coreHost);

  // Skeletons while everything loads (proper-app polish, task F).
  leftPanel.append(skeletonPanel(4), skeletonPanel(5));
  sidePanel.append(skeletonPanel(3), skeletonPanel(3), skeletonPanel(3));

  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0);
  const weekEnd = new Date(todayStart);
  weekEnd.setDate(weekEnd.getDate() + 7);

  const [sessions, notes, tasks, events, emailAccounts, models, usage] = await Promise.all([
    api("/api/sessions").catch(() => []),
    api("/api/notes?include_completed=false").catch(() => []),
    api("/api/tasks").catch(() => []),
    api(`/api/calendar/events?start=${todayStart.toISOString()}&end=${weekEnd.toISOString()}`).catch(() => []),
    api("/api/email/accounts").catch(() => []),
    api("/api/models").catch(() => []),
    api("/api/models/usage").catch(() => ({})),
  ]);

  const todayEnd = new Date(todayStart);
  todayEnd.setDate(todayEnd.getDate() + 1);
  const todaysEvents = events.filter((e) => {
    const start = new Date(e.start);
    return start >= todayStart && start < todayEnd;
  });

  stats.append(
    statCard("Chats", sessions.length),
    statCard("Open Notes", notes.length),
    statCard("Scheduled Tasks", tasks.filter((t) => t.enabled).length),
    statCard("Today's Events", todaysEvents.length),
    statCard("Email Accounts", emailAccounts.length),
  );

  const upcoming = events
    .filter((e) => new Date(e.start) >= todayStart)
    .sort((a, b) => new Date(a.start) - new Date(b.start))
    .slice(0, 4);

  const recentSessions = [...sessions]
    .sort((a, b) => b.updated_at - a.updated_at)
    .slice(0, 4);

  sidePanel.innerHTML = "";
  sidePanel.append(
    recentPanel("This Week", upcoming, (e) => `${e.title} — ${new Date(e.start).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`),
    recentPanel("Recent Chats", recentSessions, (s) => s.title),
    modelsUsagePanel(models, usage),
  );

  // -- live left column: system health + activity feed ------------------
  let countdownTimer = null;
  let refreshTimer = null;

  async function refreshLive() {
    const [status, feed] = await Promise.all([
      api("/api/system/status").catch(() => null),
      api("/api/system/events?limit=8").catch(() => []),
    ]);
    leftPanel.innerHTML = "";
    if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null; }
    const sys = systemPanel(status);
    leftPanel.append(sys.panel, activityPanel(feed));
    countdownTimer = sys.timer;
  }

  await refreshLive();
  refreshTimer = setInterval(refreshLive, STATUS_REFRESH_MS);

  return () => {
    dispose3d();
    if (refreshTimer) clearInterval(refreshTimer);
    if (countdownTimer) clearInterval(countdownTimer);
  };
}

function systemPanel(status) {
  const panel = el("div", { class: "glass bracket home-recent-panel" });
  panel.append(el("div", { class: "title", style: "margin-bottom:8px;", text: "System" }));
  if (!status) {
    panel.append(el("div", { class: "empty-state", style: "padding:10px 0;", text: "Status unavailable" }));
    return { panel, timer: null };
  }

  const discordUp = status.discord_connected_bots.length > 0;
  panel.append(
    statusRow(status.scheduler_running ? "ok" : "err", "Scheduler",
      status.scheduler_running ? `running · ${status.enabled_task_count} task${status.enabled_task_count === 1 ? "" : "s"}` : "stopped"),
    statusRow(discordUp ? "ok" : "warn", "Discord",
      discordUp ? `${status.discord_connected_bots.join(", ")} connected` : "no bots connected"),
    statusRow(status.vault_ok ? "ok" : "err", "Vault", status.vault_ok ? "reachable" : "missing"),
    statusRow(status.model_endpoint_count > 0 ? "ok" : "warn", "Models",
      status.model_endpoint_count > 0 ? `${status.model_endpoint_count} endpoint${status.model_endpoint_count === 1 ? "" : "s"}` : "none added"),
  );

  let timer = null;
  if (status.next_task) {
    const value = el("span", { class: "meta" });
    const tick = () => {
      const ms = new Date(status.next_task.next_run_at) - Date.now();
      value.textContent = ms <= 0 ? "due now" : `in ${formatDuration(ms)}`;
    };
    tick();
    timer = setInterval(tick, 1000);
    panel.append(el("div", { class: "home-recent-item", style: "display:flex;justify-content:space-between;gap:8px;" }, [
      el("span", { text: `Next: ${status.next_task.name}` }),
      value,
    ]));
  }
  return { panel, timer };
}

function statusRow(state, label, detail) {
  return el("div", { class: "status-row" }, [
    el("span", { class: `status-dot ${state}` }),
    el("span", { class: "status-label", text: label }),
    el("span", { class: "meta status-detail", text: detail }),
  ]);
}

function activityPanel(feed) {
  const panel = el("div", { class: "glass bracket home-recent-panel" });
  panel.append(el("div", { class: "title", style: "margin-bottom:8px;", text: "Activity" }));
  if (!feed.length) {
    panel.append(el("div", { class: "empty-state", style: "padding:10px 0;", text: "Nothing yet — task runs and channel events will show here" }));
    return panel;
  }
  for (const ev of feed) {
    panel.append(el("div", { class: "home-recent-item activity-item" }, [
      el("span", { class: `status-dot ${ev.level === "error" ? "err" : ev.level === "warn" ? "warn" : "ok"}` }),
      el("span", { class: "activity-msg", text: ev.message }),
      el("span", { class: "meta activity-time", text: relativeTime(ev.ts * 1000) }),
    ]));
  }
  return panel;
}

function formatDuration(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

function relativeTime(tsMs) {
  const s = Math.floor((Date.now() - tsMs) / 1000);
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function skeletonPanel(rows) {
  const panel = el("div", { class: "glass bracket home-recent-panel" });
  panel.append(el("div", { class: "skeleton skeleton-title" }));
  for (let i = 0; i < rows; i++) panel.append(el("div", { class: "skeleton skeleton-line" }));
  return panel;
}

function modelsUsagePanel(models, usage) {
  const panel = el("div", { class: "glass bracket home-recent-panel" });
  panel.append(el("div", { class: "title", style: "margin-bottom:8px;", text: "AI Models" }));
  if (models.length === 0) {
    panel.append(el("div", { class: "empty-state", style: "padding:10px 0;", text: "None added — see Settings" }));
    return panel;
  }
  for (const m of models) {
    const pct = usage[m.id]?.percentage;
    const row = el("div", { class: "home-recent-item", style: "display:flex;flex-direction:column;gap:4px;" }, [
      el("div", { style: "display:flex;justify-content:space-between;gap:8px;" }, [
        el("span", { text: m.name }),
        el("span", { class: "meta", text: pct != null ? `${pct}%` : "—" }),
      ]),
      el("div", { class: "usage-bar" }, [el("div", { class: "usage-bar-fill", style: `width:${pct || 0}%;` })]),
    ]);
    panel.appendChild(row);
  }
  return panel;
}

function statCard(label, value) {
  return el("div", { class: "glass stat-card" }, [
    el("div", { class: "stat-value", text: String(value) }),
    el("div", { class: "meta", text: label }),
  ]);
}

function recentPanel(title, items, formatItem) {
  const panel = el("div", { class: "glass bracket home-recent-panel" });
  panel.append(el("div", { class: "title", style: "margin-bottom:8px;", text: title }));
  if (items.length === 0) {
    panel.append(el("div", { class: "empty-state", style: "padding:10px 0;", text: "Nothing here yet" }));
  } else {
    for (const item of items) {
      panel.append(el("div", { class: "home-recent-item", text: formatItem(item) }));
    }
  }
  return panel;
}
