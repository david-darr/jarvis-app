// Swimlane work graph: horizontal position is dependency depth, the vertical
// lane is the owning agent. David's ask 2026-09-16 - the work panel was a
// list, which is the wrong shape for a dependency DAG. Hand-rolled SVG edges
// over DOM node cards: the app carries no frontend framework and this feature
// does not add one.
//
// Everything drawn here comes from persisted state. Nodes move only when a
// task's real state or owner changed between snapshots; nothing animates on
// its own, and there are no invented progress values.
import { el } from "./api.js";

const NODE_W = 196, NODE_H = 78, GAP_X = 64, GAP_Y = 12, PAD = 26;
const LANE_PAD = 14, LABEL_W = 132, MIN_SCALE = 0.35, MAX_SCALE = 1.8;
const SVG_NS = "http://www.w3.org/2000/svg";
const STATES = ["ready", "running", "blocked", "review", "done", "cancelled"];

const svgEl = (tag, attrs = {}) => {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
};
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const reduced = () => !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

// Longest-path layering. The store rejects cyclic dependencies, but a read
// path must not hang if that ever regresses, so the walk carries its own
// visiting set and treats a cycle edge as depth 0 rather than recursing.
export function layer(tasks, edges) {
  const incoming = new Map(tasks.map(task => [task.id, []]));
  for (const edge of edges) incoming.get(edge.task_id)?.push(edge.depends_on);
  const depth = new Map(), visiting = new Set();
  const walk = (id) => {
    if (depth.has(id)) return depth.get(id);
    if (visiting.has(id)) return 0;
    visiting.add(id);
    let value = 0;
    for (const parent of incoming.get(id) || []) {
      if (incoming.has(parent)) value = Math.max(value, walk(parent) + 1);
    }
    visiting.delete(id);
    depth.set(id, value);
    return value;
  };
  for (const task of tasks) walk(task.id);
  return depth;
}

export function createGraph({ onSelect } = {}) {
  const edgeLayer = svgEl("svg", { class: "swarm-edges", "aria-hidden": "true" });
  const nodeLayer = el("div", { class: "swarm-nodes" });
  const laneLayer = el("div", { class: "swarm-lanes", "aria-hidden": "true" });
  const surface = el("div", { class: "swarm-canvas-surface" }, [laneLayer, edgeLayer, nodeLayer]);
  const labels = el("div", { class: "swarm-lane-labels", "aria-hidden": "true" });
  const empty = el("p", { class: "swarm-empty swarm-canvas-empty" });
  const status = el("p", { class: "muted swarm-canvas-status", role: "status" });
  const viewport = el("div", { class: "swarm-canvas", tabindex: "-1" }, [surface, labels, empty]);
  const zoomOut = el("button", { type: "button", class: "btn", text: "−", "aria-label": "Zoom out", "data-focus-key": "swarm-zoom-out" });
  const zoomIn = el("button", { type: "button", class: "btn", text: "+", "aria-label": "Zoom in", "data-focus-key": "swarm-zoom-in" });
  const fitButton = el("button", { type: "button", class: "btn", text: "Fit", "data-focus-key": "swarm-fit" });
  const node = el("section", { class: "swarm-canvas-wrap", "aria-label": "Work graph" }, [
    el("div", { class: "swarm-canvas-bar" }, [status, el("div", { class: "swarm-actions" }, [zoomOut, zoomIn, fitButton])]),
    viewport,
  ]);

  let view = { x: PAD, y: PAD, scale: 1 };
  let placed = new Map();      // task id -> last drawn position, for movement only
  let positions = new Map();   // task id -> current position
  let elements = new Map();    // task id -> node element
  let grid = [];               // [lane][depth] -> task id, for arrow-key travel
  let content = { width: 0, height: 0 };
  let fitted = false;
  let timers = [];
  let dragging = null;

  const applyView = () => {
    surface.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
    // Labels ride vertical pan and zoom but stay pinned to the left gutter,
    // so lane names remain readable however far the graph is panned sideways.
    labels.style.transform = `translate(0px, ${view.y}px) scale(${view.scale})`;
    // The dot grid lives on the viewport so it extends past the content box.
    viewport.style.backgroundSize = `${24 * view.scale}px ${24 * view.scale}px`;
    viewport.style.backgroundPosition = `${view.x}px ${view.y}px`;
  };

  function zoomTo(scale, originX, originY) {
    const next = clamp(scale, MIN_SCALE, MAX_SCALE);
    const rect = viewport.getBoundingClientRect();
    const px = (originX ?? rect.width / 2), py = (originY ?? rect.height / 2);
    // Keep the point under the cursor fixed while the scale changes.
    view.x = px - (px - view.x) * (next / view.scale);
    view.y = py - (py - view.y) * (next / view.scale);
    view.scale = next;
    applyView();
  }

  // Returns false when the canvas is not on screen yet (a hidden view has no
  // box to measure), so the caller can try again once it is shown.
  function fit() {
    const rect = viewport.getBoundingClientRect();
    if (!content.width || !content.height || !rect.width) return false;
    const scale = clamp(Math.min((rect.width - LABEL_W - PAD) / content.width, (rect.height - PAD * 2) / content.height, 1), MIN_SCALE, MAX_SCALE);
    const slack = rect.height - content.height * scale;
    view = { x: LABEL_W + PAD / 2, y: slack > 0 ? slack / 2 : PAD, scale };
    applyView();
    return true;
  }

  function ensureVisible(element) {
    const box = element.getBoundingClientRect(), rect = viewport.getBoundingClientRect();
    const left = rect.left + LABEL_W + 8;
    if (box.left < left) view.x += left - box.left;
    if (box.right > rect.right - 8) view.x -= box.right - (rect.right - 8);
    if (box.top < rect.top + 8) view.y += rect.top + 8 - box.top;
    if (box.bottom > rect.bottom - 8) view.y -= box.bottom - (rect.bottom - 8);
    applyView();
  }

  viewport.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".swarm-node") || event.button !== 0) return;
    dragging = { id: event.pointerId, x: event.clientX - view.x, y: event.clientY - view.y };
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add("swarm-canvas-dragging");
  });
  viewport.addEventListener("pointermove", (event) => {
    if (!dragging || dragging.id !== event.pointerId) return;
    view.x = event.clientX - dragging.x;
    view.y = event.clientY - dragging.y;
    applyView();
  });
  const endDrag = (event) => {
    if (!dragging || dragging.id !== event.pointerId) return;
    dragging = null;
    viewport.classList.remove("swarm-canvas-dragging");
  };
  viewport.addEventListener("pointerup", endDrag);
  viewport.addEventListener("pointercancel", endDrag);
  viewport.addEventListener("wheel", (event) => {
    event.preventDefault();
    const rect = viewport.getBoundingClientRect();
    zoomTo(view.scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12), event.clientX - rect.left, event.clientY - rect.top);
  }, { passive: false });
  zoomIn.addEventListener("click", () => zoomTo(view.scale * 1.2));
  zoomOut.addEventListener("click", () => zoomTo(view.scale / 1.2));
  fitButton.addEventListener("click", fit);

  // Arrow keys travel the layout, not the DOM: right/left by dependency
  // depth within a lane, up/down between lanes at the nearest depth.
  nodeLayer.addEventListener("keydown", (event) => {
    const card = event.target.closest(".swarm-node");
    if (!card || event.altKey || event.ctrlKey || event.metaKey) return;
    const lane = Number(card.dataset.lane), depth = Number(card.dataset.depth);
    let target = null;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      const step = event.key === "ArrowRight" ? 1 : -1;
      const row = grid[lane] || [];
      for (let d = depth + step; d >= 0 && d < row.length; d += step) if (row[d]) { target = row[d]; break; }
    } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      const step = event.key === "ArrowDown" ? 1 : -1;
      for (let l = lane + step; l >= 0 && l < grid.length; l += step) {
        const row = grid[l] || [];
        if (!row.some(Boolean)) continue;
        let best = null, bestGap = Infinity;
        row.forEach((id, d) => { if (id && Math.abs(d - depth) < bestGap) { best = id; bestGap = Math.abs(d - depth); } });
        if (best) { target = best; break; }
      }
    } else return;
    event.preventDefault();
    const next = target && elements.get(target);
    if (next) { next.focus(); ensureVisible(next); }
  });

  function render(data) {
    const { agents = [], tasks = [], dependencies = [], attempts = [], loaded = tasks.length, total = tasks.length } = data;
    const byId = new Map(tasks.map(task => [task.id, task]));
    // A partially loaded page can reference a task that is not on screen.
    // Those edges are dropped and the node says so, rather than drawing a
    // line to nothing or silently implying no dependency exists.
    const edges = dependencies.filter(edge => byId.has(edge.task_id) && byId.has(edge.depends_on));
    const hidden = new Map();
    for (const edge of dependencies) {
      if (byId.has(edge.task_id) && !byId.has(edge.depends_on)) hidden.set(edge.task_id, (hidden.get(edge.task_id) || 0) + 1);
    }
    const depth = layer(tasks, edges);
    const unmet = new Map();
    for (const edge of edges) {
      if (byId.get(edge.depends_on)?.state !== "done") unmet.set(edge.task_id, (unmet.get(edge.task_id) || 0) + 1);
    }
    // Attempts carry no timestamp column; the snapshot page returns them
    // newest first (rowid DESC), so the first row seen for a task is current.
    const lastAttempt = new Map();
    for (const attempt of attempts) if (!lastAttempt.has(attempt.task_id)) lastAttempt.set(attempt.task_id, attempt);

    const owners = new Set(tasks.map(task => task.agent_id));
    const lanes = agents.filter(agent => agent.enabled || owners.has(agent.id))
      .map(agent => ({ id: agent.id, name: agent.name, role: agent.is_lead ? "Lead" : agent.role, removed: !agent.enabled }));
    if (tasks.some(task => !lanes.some(lane => lane.id === task.agent_id))) {
      lanes.push({ id: null, name: "Unassigned", role: "No current owner", removed: false });
    }
    const laneIndex = new Map(lanes.map((lane, index) => [lane.id, index]));
    const place = new Map();
    const stacks = lanes.map(() => new Map());
    grid = lanes.map(() => []);
    const ordered = [...tasks].sort((a, b) => (a.created_at || 0) - (b.created_at || 0) || a.id.localeCompare(b.id));
    for (const task of ordered) {
      const lane = laneIndex.has(task.agent_id) ? laneIndex.get(task.agent_id) : laneIndex.get(null) ?? 0;
      const column = depth.get(task.id) || 0;
      const stack = stacks[lane];
      const row = stack.get(column) || 0;
      stack.set(column, row + 1);
      place.set(task.id, { lane, column, row });
      grid[lane][column] = grid[lane][column] || task.id;
    }
    const laneRows = stacks.map(stack => Math.max(1, ...stack.values()));
    const laneTops = [];
    let y = PAD;
    laneRows.forEach((rows, index) => {
      laneTops[index] = y;
      y += rows * NODE_H + (rows - 1) * GAP_Y + LANE_PAD * 2;
    });
    const columns = Math.max(1, ...[...place.values()].map(spot => spot.column + 1));
    content = { width: PAD * 2 + columns * NODE_W + (columns - 1) * GAP_X, height: Math.max(y + PAD, 160) };

    positions = new Map();
    for (const [id, spot] of place) {
      positions.set(id, {
        x: PAD + spot.column * (NODE_W + GAP_X),
        y: laneTops[spot.lane] + LANE_PAD + spot.row * (NODE_H + GAP_Y),
      });
    }

    surface.style.width = `${content.width}px`;
    surface.style.height = `${content.height}px`;
    laneLayer.replaceChildren();
    labels.replaceChildren();
    lanes.forEach((lane, index) => {
      const height = laneRows[index] * NODE_H + (laneRows[index] - 1) * GAP_Y + LANE_PAD * 2;
      const band = el("div", { class: "swarm-lane" });
      band.style.transform = `translateY(${laneTops[index]}px)`;
      band.style.height = `${height}px`;
      // Bands run well past the last node so a lane still reads as a lane in
      // the empty space to the right of the graph.
      band.style.width = `${content.width + 3000}px`;
      laneLayer.append(band);
      const label = el("div", { class: "swarm-lane-label" }, [
        el("strong", { text: lane.name }),
        el("small", { class: "muted", text: lane.removed ? `${lane.role} · removed` : lane.role }),
      ]);
      label.style.transform = `translateY(${laneTops[index]}px)`;
      label.style.height = `${height}px`;
      labels.append(label);
    });

    edgeLayer.replaceChildren();
    edgeLayer.setAttribute("width", content.width);
    edgeLayer.setAttribute("height", content.height);
    edgeLayer.setAttribute("viewBox", `0 0 ${content.width} ${content.height}`);
    const marker = svgEl("marker", { id: "swarm-arrow", viewBox: "0 0 8 8", refX: "7", refY: "4", markerWidth: "7", markerHeight: "7", orient: "auto" });
    marker.append(svgEl("path", { d: "M0 0 L8 4 L0 8 z", fill: "currentColor" }));
    const defs = svgEl("defs");
    defs.append(marker);
    edgeLayer.append(defs);
    for (const edge of edges) {
      const from = positions.get(edge.depends_on), to = positions.get(edge.task_id);
      if (!from || !to) continue;
      const x1 = from.x + NODE_W, y1 = from.y + NODE_H / 2, x2 = to.x, y2 = to.y + NODE_H / 2;
      const bend = Math.max(28, (x2 - x1) / 2);
      edgeLayer.append(svgEl("path", {
        class: `swarm-edge${byId.get(edge.depends_on)?.state === "done" ? " swarm-edge-met" : ""}`,
        d: `M${x1} ${y1} C${x1 + bend} ${y1} ${x2 - bend} ${y2} ${x2} ${y2}`,
        "marker-end": "url(#swarm-arrow)",
      }));
    }

    const previous = placed;
    const moved = [];
    elements = new Map();
    nodeLayer.replaceChildren();
    for (const task of ordered) {
      const spot = place.get(task.id), at = positions.get(task.id);
      const state = STATES.includes(task.state) ? task.state : "ready";
      const waiting = unmet.get(task.id) || 0, offscreen = hidden.get(task.id) || 0;
      const attempt = lastAttempt.get(task.id);
      let meta = state;
      if (state === "ready") meta = waiting ? `Waiting on ${waiting} task${waiting === 1 ? "" : "s"}` : "Eligible";
      else if (state === "running") meta = attempt?.used ? `Claimed · ${attempt.used.toLocaleString()} of ${attempt.max_units.toLocaleString()} tokens` : "Claimed";
      else if (state === "blocked") meta = attempt?.error || "Blocked; no reason recorded";
      else if (state === "review") meta = "Awaiting review evidence";
      const card = el("button", {
        type: "button", class: "swarm-node", "data-task": task.id, "data-state": state,
        "data-lane": String(spot.lane), "data-depth": String(spot.column),
        "data-focus-key": `swarm-node-${task.id}`,
        title: task.objective,
        onclick: () => onSelect?.(task),
      }, [
        el("span", { class: "swarm-node-state", "aria-hidden": "true" }),
        el("strong", { text: task.objective }),
        el("small", { class: "muted", text: meta }),
      ]);
      if (offscreen) card.append(el("small", { class: "muted", text: `+${offscreen} dependency not loaded` }));
      const before = previous.get(task.id);
      card.style.transform = `translate(${(before || at).x}px, ${(before || at).y}px)`;
      if (before && (before.x !== at.x || before.y !== at.y)) moved.push(card);
      nodeLayer.append(card);
      elements.set(task.id, card);
    }

    if (moved.length && !reduced()) {
      void nodeLayer.offsetWidth;   // commit the old positions before transitioning
      edgeLayer.classList.add("swarm-edges-settling");
      timers.push(setTimeout(() => edgeLayer.classList.remove("swarm-edges-settling"), 420));
      for (const card of moved) {
        card.classList.add("swarm-node-moved");
        timers.push(setTimeout(() => card.classList.remove("swarm-node-moved"), 900));
      }
    }
    for (const [id, at] of positions) {
      const card = elements.get(id);
      if (card) card.style.transform = `translate(${at.x}px, ${at.y}px)`;
    }
    if (moved.length && reduced()) for (const card of moved) {
      card.classList.add("swarm-node-changed");
      timers.push(setTimeout(() => card.classList.remove("swarm-node-changed"), 1800));
    }
    placed = positions;

    const counted = tasks.length;
    empty.hidden = !!counted;
    empty.textContent = "No tasks yet. Work appears here once the lead plans a milestone.";
    status.textContent = counted
      ? `${counted} task${counted === 1 ? "" : "s"}${loaded < total ? ` of ${total} loaded` : ""} · ${lanes.length} lane${lanes.length === 1 ? "" : "s"}`
      : "No tasks to draw.";
    if (!fitted && counted) fitted = fit();
    else applyView();
  }

  function dispose() {
    for (const timer of timers) clearTimeout(timer);
    timers = [];
    elements.clear();
  }

  return { node, render, dispose, fit };
}
