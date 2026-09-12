// Stacking top-right overlay showing every chat session currently
// generating a reply, regardless of which tab is active (David's ask
// 2026-09-12 — sending a prompt then switching to Notes to get other
// things done used to give zero indication anything was still happening).
// Mounted once from app.js at startup, independent of app.js's per-tab
// view-swapping (see chatStream.js's module docstring for why the actual
// state lives one level above any single view) — same lazily-created-host
// pattern api.js's toast() already uses for #toast-host, just a separate
// corner and a different lifecycle (driven by chatStream's subscribeAll,
// not a fixed timer).
import * as chatStream from "./chatStream.js";

let host = null;
let _switchTab = null; // injected by init(), same dependency-passing convention commandPalette.js uses
const cards = new Map(); // sessionId -> HTMLElement

function ensureHost() {
  if (host) return host;
  host = document.createElement("div");
  host.id = "chat-progress-host";
  document.body.appendChild(host);
  return host;
}

async function jumpToSession(sessionId) {
  await _switchTab("chat");
  const chatView = await import("./views/chat.js");
  chatView.openSessionById(sessionId);
}

function render(sessionId, entry) {
  ensureHost();
  let card = cards.get(sessionId);
  if (!entry) {
    if (card) { card.remove(); cards.delete(sessionId); }
    return;
  }
  if (!card) {
    card = document.createElement("div");
    card.className = "chat-progress-card";
    card.addEventListener("click", () => jumpToSession(sessionId));
    host.appendChild(card);
    cards.set(sessionId, card);
    // Force layout before the "show" transition so it actually animates in.
    card.getBoundingClientRect();
    card.classList.add("show");
  }
  card.classList.toggle("done", entry.status === "done");
  card.classList.toggle("failed", entry.status === "failed");
  const statusText = entry.status === "processing" ? "responding..."
    : entry.status === "done" ? "finished"
      : "failed";
  card.innerHTML = "";
  card.appendChild(Object.assign(document.createElement("span"), { className: "chat-progress-dot" }));
  const label = document.createElement("span");
  label.className = "chat-progress-label";
  label.textContent = `${entry.sessionTitle} — ${statusText}`;
  card.appendChild(label);
}

export function init({ switchTab }) {
  _switchTab = switchTab;
  chatStream.subscribeAll(render);
  // Cover a turn that was already in flight before this module's first
  // subscribeAll call (shouldn't normally happen — app.js calls init() at
  // startup, before any message could have been sent — but cheap insurance
  // against init-order changes later).
  for (const { sessionId, ...entry } of chatStream.listInFlight()) render(sessionId, entry);
}
