// Kanban board over the store's real task states. David's ask 2026-09-16:
// see work move between columns. Columns are the task state machine as it
// exists in core/swarm/store.py - ready, running, blocked, review, done, with
// cancelled behind a filter - so nothing here is a display-only status.
//
// A card travels only when a real event changed the task between snapshots.
// The board is deliberately read-only: task state belongs to the coordinator
// through revision-checked commands, and a drag into "running" would be the
// UI inventing state the engine never agreed to.
import { el } from "./api.js";

const COLUMNS = [
  { key: "ready", label: "Ready", hint: "Eligible once dependencies finish" },
  { key: "running", label: "Running", hint: "Claimed by an agent" },
  { key: "blocked", label: "Blocked", hint: "Needs a decision or reconciliation" },
  { key: "review", label: "Review", hint: "Result waiting on evidence" },
  { key: "done", label: "Done", hint: "Accepted with evidence" },
];
const CANCELLED = { key: "cancelled", label: "Cancelled", hint: "Stopped before completion" };
const reduced = () => !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

export function createBoard({ onSelect } = {}) {
  const columnsHost = el("div", { class: "swarm-board" });
  const status = el("p", { class: "muted swarm-board-status", role: "status" });
  const showCancelled = el("button", { type: "button", class: "btn", text: "Show cancelled", "aria-pressed": "false", "data-focus-key": "swarm-show-cancelled" });
  const node = el("section", { class: "swarm-board-wrap", "aria-label": "Task board" }, [
    el("div", { class: "swarm-board-bar" }, [status, el("div", { class: "swarm-actions" }, [showCancelled])]),
    columnsHost,
  ]);

  let cancelledVisible = false;
  let lastColumn = new Map();   // task id -> column it was last drawn in
  let latest = null;
  let timers = [];

  showCancelled.addEventListener("click", () => {
    cancelledVisible = !cancelledVisible;
    showCancelled.setAttribute("aria-pressed", String(cancelledVisible));
    showCancelled.textContent = cancelledVisible ? "Hide cancelled" : "Show cancelled";
    if (latest) render(latest);
  });

  function describe(task, { unmet, attempts }) {
    const waiting = unmet.get(task.id) || 0;
    const attempt = attempts.get(task.id);
    if (task.state === "ready") return waiting ? `Waiting on ${waiting} task${waiting === 1 ? "" : "s"}` : "Eligible";
    if (task.state === "running") return attempt?.used ? `Claimed · ${attempt.used.toLocaleString()} of ${attempt.max_units.toLocaleString()} tokens` : "Claimed";
    if (task.state === "blocked") return attempt?.error || "Blocked; no reason recorded";
    if (task.state === "review") return "Awaiting review evidence";
    if (task.state === "cancelled") return "Cancelled with the run";
    return "Accepted";
  }

  function render(data) {
    latest = data;
    const { agents = [], tasks = [], dependencies = [], attempts = [], loaded = tasks.length, total = tasks.length, onLoadMore } = data;
    const names = new Map(agents.map(agent => [agent.id, agent.name]));
    const byId = new Map(tasks.map(task => [task.id, task]));
    const unmet = new Map(), degree = new Map();
    for (const edge of dependencies) {
      if (!byId.has(edge.task_id)) continue;
      degree.set(edge.task_id, (degree.get(edge.task_id) || 0) + 1);
      if (byId.get(edge.depends_on)?.state !== "done") unmet.set(edge.task_id, (unmet.get(edge.task_id) || 0) + 1);
    }
    // Attempts have no timestamp column; the snapshot page returns them
    // newest first (rowid DESC), so the first row for a task is its current one.
    const lastAttempt = new Map();
    for (const attempt of attempts) if (!lastAttempt.has(attempt.task_id)) lastAttempt.set(attempt.task_id, attempt);

    // Measure where every card sits before the rebuild, so a card that moves
    // can be animated from where the user last saw it.
    const before = new Map();
    for (const card of columnsHost.querySelectorAll(".swarm-card")) before.set(card.dataset.task, card.getBoundingClientRect());

    const columns = cancelledVisible ? [...COLUMNS, CANCELLED] : COLUMNS;
    const grouped = new Map(columns.map(column => [column.key, []]));
    let hiddenCancelled = 0;
    for (const task of [...tasks].sort((a, b) => (a.created_at || 0) - (b.created_at || 0) || a.id.localeCompare(b.id))) {
      if (!grouped.has(task.state)) { if (task.state === "cancelled") hiddenCancelled++; continue; }
      grouped.get(task.state).push(task);
    }

    columnsHost.replaceChildren();
    const moved = [];
    for (const column of columns) {
      const items = grouped.get(column.key);
      const list = el("div", { class: "swarm-column-body" });
      const section = el("section", { class: "swarm-column", "data-column": column.key, "aria-label": `${column.label}, ${items.length}` }, [
        el("header", { class: "swarm-column-head" }, [
          el("h3", { text: column.label }), el("span", { class: "swarm-column-count", text: String(items.length) }),
          el("small", { class: "muted", text: column.hint }),
        ]), list,
      ]);
      if (!items.length) list.append(el("p", { class: "swarm-empty", text: "None" }));
      for (const task of items) {
        const card = el("button", {
          type: "button", class: "swarm-card", "data-task": task.id, "data-state": task.state,
          "data-focus-key": `swarm-card-${task.id}`, title: task.objective,
          onclick: () => onSelect?.(task),
        }, [
          el("strong", { text: task.objective }),
          el("span", { class: "muted", text: names.get(task.agent_id) || "Unassigned" }),
          el("small", { class: "muted", text: describe(task, { unmet, attempts: lastAttempt }) }),
        ]);
        if (degree.get(task.id)) card.append(el("small", { class: "swarm-card-deps", text: `${degree.get(task.id)} dependenc${degree.get(task.id) === 1 ? "y" : "ies"}` }));
        list.append(card);
        if (before.has(task.id)) moved.push({ card, from: before.get(task.id), changed: lastColumn.get(task.id) !== task.state });
      }
      columnsHost.append(section);
    }

    const travel = !reduced();
    for (const entry of moved) {
      const now = entry.card.getBoundingClientRect();
      const dx = entry.from.left - now.left, dy = entry.from.top - now.top;
      if (!travel || (Math.abs(dx) < 1 && Math.abs(dy) < 1)) {
        if (entry.changed) {
          entry.card.classList.add(travel ? "swarm-card-arrived" : "swarm-card-changed");
          timers.push(setTimeout(() => entry.card.classList.remove("swarm-card-arrived", "swarm-card-changed"), 1600));
        }
        continue;
      }
      entry.card.style.transform = `translate(${dx}px, ${dy}px)`;
      entry.card.style.transition = "none";
      entry.travel = true;
    }
    if (travel && moved.some(entry => entry.travel)) {
      void columnsHost.offsetWidth;     // commit the starting offsets first
      for (const entry of moved) {
        if (!entry.travel) continue;
        entry.card.style.transition = "";
        entry.card.style.transform = "";
        if (entry.changed) {
          entry.card.classList.add("swarm-card-arrived");
          timers.push(setTimeout(() => entry.card.classList.remove("swarm-card-arrived"), 1600));
        }
      }
    }

    lastColumn = new Map(tasks.map(task => [task.id, task.state]));
    const parts = [`${tasks.length} task${tasks.length === 1 ? "" : "s"}`];
    if (loaded < total) parts.push(`${total} total`);
    if (hiddenCancelled) parts.push(`${hiddenCancelled} cancelled hidden`);
    status.replaceChildren(document.createTextNode(tasks.length ? parts.join(" · ") : "No tasks have been created."));
    if (loaded < total && onLoadMore) {
      status.append(document.createTextNode(" "));
      status.append(el("button", { type: "button", class: "btn", text: "Load more tasks", "data-focus-key": "swarm-board-more", onclick: onLoadMore }));
    }
  }

  function dispose() {
    for (const timer of timers) clearTimeout(timer);
    timers = [];
    latest = null;
  }

  return { node, render, dispose };
}
