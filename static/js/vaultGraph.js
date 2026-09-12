import { api, el, toast } from "./api.js";

// Brain tab's Vault view (David's ask 2026-09-01) — force-directed graph of
// the vault's folders/notes, ported from the original JARVIS kiosk
// (voice-visualizer/index.html's Vault tab), restyled onto this app's own
// design tokens (see specs/frontend-style.md). Same radial-tree-plus-light-
// physics layout, same canvas rendering approach — a real port, not a
// from-scratch reinvention, since the original was already live-tuned.

const PALETTE = [
  "#a894eb", "#7e9ed0", "#ce91b7", "#83b8a4",
  "#d7b27c", "#84b9c5", "#bd8d92", "#b7b38a",
];
const RING = 95;

function topFolder(node) { return (node.folder || "").split("/")[0] || "(root)"; }
function colorForFolder(name) {
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return PALETTE[hash % PALETTE.length];
}
function nodeRadius(node) { return node.type === "folder" ? (node.id === "" ? 11 : 7) : 4; }
function nodeColor(node) {
  if (node.type === "folder") return node.id === "" ? "#e2d9f3" : "#d7b27c";
  return colorForFolder(topFolder(node));
}

function layout(graph) {
  const byId = {};
  graph.nodes.forEach((n) => { byId[n.id] = n; });
  const childrenOf = {};
  graph.edges.forEach((e) => {
    if (e.kind !== "contains") return;
    (childrenOf[e.source] = childrenOf[e.source] || []).push(e.target);
  });
  const weightCache = {};
  function weight(id) {
    if (weightCache[id] !== undefined) return weightCache[id];
    const kids = childrenOf[id];
    const w = !kids || !kids.length ? 1 : kids.reduce((sum, c) => sum + weight(c), 0);
    weightCache[id] = w;
    return w;
  }
  function place(id, depth, a0, a1) {
    const node = byId[id];
    if (!node) return;
    const angle = (a0 + a1) / 2;
    const r = depth * RING;
    node.x = Math.cos(angle) * r;
    node.y = Math.sin(angle) * r;
    node.vx = 0; node.vy = 0; node.fixed = false;
    const kids = (childrenOf[id] || []).slice().sort((p, q) => {
      const pn = byId[p], qn = byId[q];
      if (pn.type !== qn.type) return pn.type === "folder" ? -1 : 1;
      return pn.name.localeCompare(qn.name);
    });
    if (!kids.length) return;
    let total = 0;
    const weights = kids.map((c) => { const w = weight(c); total += w; return w; });
    const span = a1 - a0;
    let cursor = a0;
    kids.forEach((c, i) => {
      const slice = span * (weights[i] / total);
      place(c, depth + 1, cursor, cursor + slice);
      cursor += slice;
    });
  }
  place("", 0, 0, Math.PI * 2);
  return byId;
}

export function createVaultGraph(container) {
  let graph = null;
  let nodeById = {};
  let simRunning = false;
  let raf = null;
  const transform = { x: 0, y: 0, scale: 1 };
  let drag = null;
  let pointerDown = null;
  let selectedId = null;
  let hoveredId = null;
  let disposed = false;
  let settledTicks = 0;
  let autoFit = true;
  let viewportWidth = 0, viewportHeight = 0;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const canvas = el("canvas", { id: "vault-graph-canvas", "aria-label": "Vault constellation. Use search or Browse vault to open notes." });
  const panelHeader = el("div", { class: "vault-panel-header" });
  const panelTitle = el("span", { text: "Select a note" });
  const editBtn = el("button", { class: "btn", text: "Edit", style: "display:none;" });
  const saveBtn = el("button", { class: "btn", text: "Save", style: "display:none;" });
  const cancelBtn = el("button", { class: "btn", text: "Cancel", style: "display:none;" });
  const closeBtn = el("button", { class: "btn", text: "Close" });
  panelHeader.append(panelTitle, el("div", { class: "vault-panel-actions" }, [editBtn, saveBtn, cancelBtn, closeBtn]));
  const panelBreadcrumb = el("div", { class: "vault-panel-breadcrumb" });
  const panelBody = el("div", { class: "vault-panel-body" });
  const resizeHandle = el("div", { class: "vault-panel-resize-handle" });
  const panel = el("div", { class: "glass bracket vault-panel" }, [resizeHandle, panelHeader, panelBreadcrumb, panelBody]);
  let panelRawContent = "";

  let resizeDrag = null;
  resizeHandle.addEventListener("pointerdown", (e) => {
    resizeHandle.setPointerCapture(e.pointerId);
    resizeHandle.classList.add("dragging");
    resizeDrag = { startX: e.clientX, startWidth: panel.getBoundingClientRect().width };
  });
  resizeHandle.addEventListener("pointermove", (e) => {
    if (!resizeDrag) return;
    const delta = resizeDrag.startX - e.clientX;
    panel.style.width = (resizeDrag.startWidth + delta) + "px";
  });
  resizeHandle.addEventListener("pointerup", () => {
    resizeDrag = null;
    resizeHandle.classList.remove("dragging");
  });

  const wrap = el("div", { class: "vault-graph-wrap" }, [canvas, panel]);
  const search = el("input", { class: "vault-search", placeholder: "Find a note or folder…", "aria-label": "Search vault" });
  const searchResults = el("div", { class: "vault-search-results" });
  const counts = el("span", { text: "Loading your constellation…" });
  const toolbar = el("div", { class: "vault-toolbar" }, [
    search,
    el("button", { class: "btn", type: "button", text: "Fit view", onclick: () => { autoFit = true; fitToView(); redraw(); } }),
    el("button", { class: "btn", type: "button", text: "Browse vault", onclick: () => { if (nodeById[""]) selectNode(nodeById[""]); } }),
  ]);
  wrap.append(toolbar, searchResults, el("div", { class: "vault-caption" }, [
    counts, el("span", { text: "Drag to explore · Scroll to zoom · Select to read" }),
  ]));
  container.appendChild(wrap);
  search.addEventListener("input", () => {
    searchResults.replaceChildren();
    const query = search.value.trim().toLowerCase();
    if (!query || !graph) return;
    const matches = graph.nodes.filter((node) => node.name.toLowerCase().includes(query)).slice(0, 20);
    if (!matches.length) searchResults.append(el("div", { class: "dashboard-empty", text: "No matching notes or folders" }));
    for (const node of matches) searchResults.append(el("button", {
      type: "button", class: "vault-search-result", text: node.name,
      onclick: () => { autoFit = false; searchResults.replaceChildren(); transform.x = -node.x * transform.scale; transform.y = -node.y * transform.scale; selectNode(node); },
    }));
  });
  search.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { search.value = ""; searchResults.replaceChildren(); }
    if (event.key === "Enter") searchResults.querySelector("button")?.click();
  });

  function fitToView() {
    if (!graph || !graph.nodes.length || !viewportWidth || !viewportHeight) return;
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    graph.nodes.forEach((n) => {
      if (n.x < minX) minX = n.x;
      if (n.x > maxX) maxX = n.x;
      if (n.y < minY) minY = n.y;
      if (n.y > maxY) maxY = n.y;
    });
    const w = Math.max(maxX - minX, 1), h = Math.max(maxY - minY, 1);
    const scale = Math.min(viewportWidth / (w + 160), viewportHeight / (h + 190));
    transform.scale = Math.min(1.4, Math.max(0.2, scale));
    transform.x = -((minX + maxX) / 2) * transform.scale;
    transform.y = -((minY + maxY) / 2) * transform.scale;
  }

  function tick() {
    const nodes = graph.nodes;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const distSq = Math.max(dx * dx + dy * dy, 25);
        const dist = Math.sqrt(distSq);
        const force = 700 / distSq;
        const fx = (dx / dist) * force, fy = (dy / dist) * force;
        if (!a.fixed) { a.vx += fx; a.vy += fy; }
        if (!b.fixed) { b.vx -= fx; b.vy -= fy; }
      }
    }
    graph.edges.forEach((e) => {
      const a = nodeById[e.source], b = nodeById[e.target];
      if (!a || !b) return;
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const target = e.kind === "contains" ? RING : 170;
      const k = e.kind === "contains" ? 0.03 : 0.0025;
      const force = (dist - target) * k;
      const fx = (dx / dist) * force, fy = (dy / dist) * force;
      if (!a.fixed) { a.vx += fx; a.vy += fy; }
      if (!b.fixed) { b.vx -= fx; b.vy -= fy; }
    });
    nodes.forEach((n) => {
      if (n.fixed) return;
      n.vx *= 0.8; n.vy *= 0.8;
      n.x += n.vx; n.y += n.vy;
    });
  }

  function draw() {
    if (!graph || disposed) return;
    const ctx = canvas.getContext("2d");
    const w = viewportWidth, h = viewportHeight;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.translate(w / 2 + transform.x, h / 2 + transform.y);
    ctx.scale(transform.scale, transform.scale);

    graph.edges.forEach((e) => {
      const a = nodeById[e.source], b = nodeById[e.target];
      if (!a || !b) return;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      const connected = selectedId !== null && (e.source === selectedId || e.target === selectedId);
      ctx.strokeStyle = connected ? "rgba(193,173,242,.65)" : e.kind === "link" ? "rgba(167,148,211,.18)" : "rgba(191,174,147,.09)";
      ctx.lineWidth = (e.kind === "link" ? 0.9 : 0.6) / transform.scale;
      ctx.stroke();
    });

    graph.nodes.forEach((n) => {
      const r = nodeRadius(n);
      ctx.beginPath();
      if (n.type === "folder") ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
      else {
        ctx.moveTo(n.x, n.y - r);
        ctx.lineTo(n.x + r * .866, n.y + r * .5);
        ctx.lineTo(n.x - r * .866, n.y + r * .5);
        ctx.closePath();
      }
      ctx.fillStyle = nodeColor(n);
      ctx.globalAlpha = selectedId === null || n.id === selectedId || n.id === hoveredId ? .85 : .4;
      if (n.type === "folder") ctx.fill();
      else { ctx.strokeStyle = nodeColor(n); ctx.lineWidth = 1 / transform.scale; ctx.stroke(); }
      ctx.globalAlpha = 1;
      if (n.id === selectedId || n.id === hoveredId) {
        ctx.lineWidth = 2 / transform.scale;
        ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
        ctx.stroke();
      }
      const showLabel = n.id === selectedId
        || n.id === hoveredId || (n.type === "folder" ? transform.scale > 0.35 : transform.scale > 1.6);
      if (showLabel) {
        ctx.font = (n.type === "folder" ? 11 : 10) / transform.scale + "px Segoe UI, sans-serif";
        ctx.fillStyle = n.id === selectedId ? "#eeedf0" : "#aaa5b5";
        ctx.fillText(n.name, n.x + r + 3, n.y + 3);
      }
    });
    ctx.restore();
  }

  function animate() {
    raf = null;
    if (!simRunning || disposed || document.hidden) return;
    if (settledTicks < 240 && !reducedMotion.matches) { tick(); settledTicks++; }
    if (autoFit) fitToView();
    draw();
    if (drag || (settledTicks < 240 && !reducedMotion.matches)) raf = requestAnimationFrame(animate);
  }
  function redraw() { if (!disposed && graph && raf === null && !document.hidden) raf = requestAnimationFrame(animate); }

  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    viewportWidth = rect.width; viewportHeight = rect.height;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(rect.width * dpr);
    canvas.height = Math.round(rect.height * dpr);
    redraw();
  }

  function screenToWorld(clientX, clientY) {
    const rect = canvas.getBoundingClientRect();
    const x = clientX - rect.left - viewportWidth / 2 - transform.x;
    const y = clientY - rect.top - viewportHeight / 2 - transform.y;
    return { x: x / transform.scale, y: y / transform.scale };
  }

  function hitTest(clientX, clientY) {
    const p = screenToWorld(clientX, clientY);
    let best = null, bestDist = Infinity;
    graph.nodes.forEach((n) => {
      const r = Math.max(nodeRadius(n) + 4, 12 / transform.scale);
      const dx = n.x - p.x, dy = n.y - p.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist <= r && dist < bestDist) { best = n; bestDist = dist; }
    });
    return best;
  }

  function renderBreadcrumb(folderId, allClickable) {
    panelBreadcrumb.innerHTML = "";
    panelBreadcrumb.classList.add("show");
    const parts = folderId ? folderId.split("/") : [];
    let pathSoFar = "";
    const crumbs = [{ id: "", label: "Vault" }];
    parts.forEach((part) => {
      pathSoFar = pathSoFar ? `${pathSoFar}/${part}` : part;
      crumbs.push({ id: pathSoFar, label: part });
    });
    crumbs.forEach((crumb, i) => {
      const isLast = i === crumbs.length - 1;
      const isCurrent = isLast && !allClickable;
      const attrs = { class: "vault-breadcrumb-crumb" + (isCurrent ? " current" : ""), text: crumb.label };
      if (!isCurrent) attrs.onclick = () => { const n = nodeById[crumb.id]; if (n) selectNode(n); };
      if (!isCurrent) attrs.type = "button";
      panelBreadcrumb.appendChild(el(isCurrent ? "span" : "button", attrs));
      if (!isLast) panelBreadcrumb.appendChild(el("span", { class: "vault-breadcrumb-sep", text: "/" }));
    });
  }

  function selectNode(node) {
    selectedId = node.id;
    redraw();
    panel.classList.add("open");
    editBtn.style.display = "none";
    saveBtn.style.display = "none";
    cancelBtn.style.display = "none";

    if (node.type === "folder") {
      panelTitle.textContent = node.id === "" ? "Vault" : node.name;
      renderBreadcrumb(node.id);
      const children = graph.edges
        .filter((e) => e.kind === "contains" && e.source === node.id)
        .map((e) => nodeById[e.target])
        .filter(Boolean)
        .sort((a, b) => (a.type !== b.type ? (a.type === "folder" ? -1 : 1) : a.name.localeCompare(b.name)));
      panelBody.innerHTML = "";
      if (!children.length) {
        panelBody.appendChild(el("div", { class: "meta", text: "Empty folder." }));
        return;
      }
      children.forEach((child) => {
        const row = el("button", {
          type: "button",
          class: "vault-child-row",
          text: (child.type === "folder" ? "▸  " : "◇  ") + child.name,
          onclick: () => selectNode(child),
        });
        panelBody.appendChild(row);
      });
      return;
    }

    renderBreadcrumb(node.folder || "", true);
    panelTitle.textContent = node.name;
    panelBody.innerHTML = "";
    panelBody.appendChild(el("div", { class: "meta", text: "Loading..." }));
    api(`/api/vault/note?path=${encodeURIComponent(node.id)}`)
      .then((data) => {
        if (selectedId !== node.id) return;
        panelBody.textContent = data.content;
        panelRawContent = data.content;
        editBtn.style.display = "";
      })
      .catch(() => {
        if (selectedId !== node.id) return;
        panelBody.innerHTML = "";
        panelBody.appendChild(el("div", { class: "meta", text: "Couldn't load this note." }));
      });
  }

  editBtn.addEventListener("click", () => {
    panelBody.innerHTML = "";
    const textarea = el("textarea", { id: "vault-note-editor" });
    textarea.value = panelRawContent;
    panelBody.appendChild(textarea);
    editBtn.style.display = "none";
    saveBtn.style.display = "";
    cancelBtn.style.display = "";
  });

  cancelBtn.addEventListener("click", () => {
    const node = nodeById[selectedId];
    if (node) selectNode(node);
  });

  saveBtn.addEventListener("click", async () => {
    const textarea = document.getElementById("vault-note-editor");
    if (!textarea || !selectedId) return;
    const content = textarea.value;
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
    try {
      await api("/api/vault/note", { method: "POST", body: JSON.stringify({ path: selectedId, content }) });
      panelRawContent = content;
      const node = nodeById[selectedId];
      if (node) selectNode(node);
    } catch (error) {
      toast("Couldn't save this note: " + error.message, "error");
    } finally {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save";
    }
  });

  closeBtn.addEventListener("click", () => {
    panel.classList.remove("open");
    selectedId = null;
    redraw();
  });

  canvas.addEventListener("pointerdown", (e) => {
    autoFit = false;
    canvas.setPointerCapture(e.pointerId);
    settledTicks = 180;
    redraw();
    pointerDown = { x: e.clientX, y: e.clientY, moved: false };
    const hit = graph ? hitTest(e.clientX, e.clientY) : null;
    if (hit) {
      hit.fixed = true;
      drag = { node: hit };
    } else {
      drag = { pan: true, startX: e.clientX, startY: e.clientY, startTX: transform.x, startTY: transform.y };
      canvas.classList.add("dragging");
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag) {
      const hit = graph ? hitTest(e.clientX, e.clientY) : null;
      if ((hit?.id ?? null) !== hoveredId) { hoveredId = hit?.id ?? null; redraw(); }
      canvas.style.cursor = hit ? "pointer" : "grab";
      return;
    }
    if (pointerDown) {
      const dx0 = e.clientX - pointerDown.x, dy0 = e.clientY - pointerDown.y;
      if (Math.abs(dx0) > 3 || Math.abs(dy0) > 3) pointerDown.moved = true;
    }
    if (drag.node) {
      const p = screenToWorld(e.clientX, e.clientY);
      drag.node.x = p.x;
      drag.node.y = p.y;
      drag.node.vx = 0;
      drag.node.vy = 0;
    } else if (drag.pan) {
      transform.x = drag.startTX + (e.clientX - drag.startX);
      transform.y = drag.startTY + (e.clientY - drag.startY);
    }
  });
  canvas.addEventListener("pointerup", () => {
    if (drag && drag.node) drag.node.fixed = false;
    if (pointerDown && !pointerDown.moved) {
      const hit = graph ? hitTest(pointerDown.x, pointerDown.y) : null;
      if (hit) selectNode(hit);
    }
    drag = null;
    pointerDown = null;
    canvas.classList.remove("dragging");
    redraw();
  });
  canvas.addEventListener("pointercancel", () => {
    if (drag?.node) drag.node.fixed = false;
    drag = null; pointerDown = null; canvas.classList.remove("dragging"); redraw();
  });
  canvas.addEventListener("wheel", (e) => {
    e.preventDefault();
    autoFit = false;
    const factor = e.deltaY < 0 ? 1.1 : 0.9;
    transform.scale = Math.min(3, Math.max(0.15, transform.scale * factor));
    redraw();
  }, { passive: false });

  const observer = new ResizeObserver(resizeCanvas);
  observer.observe(wrap);
  document.addEventListener("visibilitychange", redraw);
  reducedMotion.addEventListener("change", redraw);

  async function load() {
    const data = await api("/api/vault/graph");
    if (disposed) return;
    graph = data;
    resizeCanvas();
    nodeById = layout(data);
    fitToView();
    counts.textContent = data.nodes.filter((node) => node.type !== "folder").length + " notes · " + data.edges.filter((edge) => edge.kind === "link").length + " connections";
    if (!simRunning) {
      simRunning = true;
      animate();
    }
  }
  load().catch(() => { if (!disposed) counts.textContent = "Couldn't load the vault. Reopen this view to retry."; });

  return function destroy() {
    simRunning = false;
    disposed = true;
    if (raf) cancelAnimationFrame(raf);
    observer.disconnect();
    document.removeEventListener("visibilitychange", redraw);
    reducedMotion.removeEventListener("change", redraw);
  };
}
