import { api, el, customSelect, toast, confirmDialog } from "../api.js";
import { runHistory } from "../runHistory.js";

// The work board (Hermes track 2026-09-23, after Hermes's kanban): one-off
// cards JARVIS works through by itself. The task loop claims one Ready card at
// a time, runs it and puts the result in Review; a person approves it or sends
// it back with a note. See services/task_service.py.

const COLUMNS = [
  ["backlog", "Backlog"], ["ready", "Ready"], ["running", "Running"],
  ["review", "Review"], ["done", "Done"], ["blocked", "Blocked"],
];
const POLL_MS = 5000;

// The header and the add form are built once; only the columns refresh, so a
// card being typed is never wiped by the board updating itself.
export async function renderBoard(host) {
  const [cards, models, agents] = await Promise.all([loadCards(), api("/api/models").catch(() => []),
                                                     api("/api/agents").catch(() => [])]);
  host.innerHTML = "";
  const columns = el("div", { class: "task-board" });
  host.append(
    el("div", { class: "title", text: "Work board" }),
    el("div", { class: "meta", style: "margin:4px 0 12px;", text:
      "One-off work JARVIS does by itself. Ready cards run one at a time; results wait in Review for you. "
      + "A card can wait for others and gets their results." }),
    addForm(columns, cards, models),
    columns,
  );
  columns.models = models;
  columns.agents = new Map(agents.map((a) => [a.id, a]));
  // Cards whose run history is open, kept open across the board's redraws.
  columns.openHistory = new Set();
  drawColumns(columns, cards);
}

function loadCards() {
  return api("/api/tasks").then((all) => all.filter((t) => t.schedule_kind === "card"));
}

async function refreshColumns(columns) {
  if (!document.body.contains(columns)) return;
  drawColumns(columns, await loadCards());
}

function drawColumns(columns, cards) {
  clearTimeout(columns.pollTimer);
  const byId = new Map(cards.map((c) => [c.id, c]));
  const modelName = new Map((columns.models || []).map((m) => [m.id, m.name]));
  columns.innerHTML = "";
  for (const [status, label] of COLUMNS) {
    const column = cards.filter((c) => c.status === status).sort((a, b) => a.created_at - b.created_at);
    columns.append(el("div", { class: `board-column board-${status}` }, [
      el("div", { class: "board-column-title", text: `${label} ${column.length ? `(${column.length})` : ""}` }),
      ...column.map((card) => cardEl(columns, card, byId, modelName)),
    ]));
  }
  // Keep watching while work is in motion. Wait while someone is writing a
  // change request, rather than redraw it away.
  if (cards.some((c) => c.status === "ready" || c.status === "running")) {
    const tick = () => {
      if (!document.body.contains(columns)) return;
      const typing = [...columns.querySelectorAll(".board-feedback")].some((i) => i.value || i === document.activeElement);
      if (typing) columns.pollTimer = setTimeout(tick, POLL_MS);
      else refreshColumns(columns);
    };
    columns.pollTimer = setTimeout(tick, POLL_MS);
  }
}

function modelOptions(models, selected = "") {
  return [
    el("option", { value: "", text: "Claude (default)", ...(selected ? {} : { selected: "" }) }),
    ...models.map((m) => el("option", { value: m.id, text: m.name, ...(m.id === selected ? { selected: "" } : {}) })),
  ];
}

function addForm(columns, cards, models) {
  const form = el("details", { class: "disclosure-panel" });
  const nameInput = el("input", { placeholder: "e.g. Draft the release notes" });
  const promptInput = el("textarea", { rows: "3", placeholder: "What should JARVIS do? Be as specific as you would with a person." });
  const modelSelect = customSelect({}, modelOptions(models));
  const waitSelect = customSelect({}, [
    el("option", { value: "", text: "Nothing" }),
    ...cards.filter((c) => c.status !== "done").map((c) => el("option", { value: c.id, text: c.name })),
  ]);
  const add = async (status) => {
    const name = nameInput.value.trim();
    const prompt = promptInput.value.trim();
    if (!name || !prompt) { toast("A card needs a name and what to do", "error"); return; }
    await api("/api/tasks", { method: "POST", body: JSON.stringify({
      name, prompt, schedule_kind: "card", status,
      depends_on: waitSelect.value ? [waitSelect.value] : [], endpoint_id: modelSelect.value || null,
    }) });
    toast(status === "ready" ? "Card added; JARVIS will pick it up" : "Card added to the backlog", "success");
    await renderBoard(columns.parentElement);
  };
  form.append(
    el("summary", { text: "+ Add a card" }),
    el("div", { class: "form-grid" }, [
      el("div", { class: "field field-grow" }, [el("label", { text: "Name" }), nameInput]),
      el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [el("label", { text: "What to do" }), promptInput]),
      el("div", { class: "field" }, [el("label", { text: "Model" }), modelSelect]),
      el("div", { class: "field" }, [el("label", { text: "Waits for" }), waitSelect]),
      el("button", { class: "btn primary", text: "Add to Ready", onclick: () => add("ready") }),
      el("button", { class: "btn quiet", text: "Add to Backlog", onclick: () => add("backlog") }),
    ]),
  );
  return form;
}

async function move(columns, card, status, note) {
  try {
    await api(`/api/tasks/${card.id}/status`, { method: "POST", body: JSON.stringify({ status, note: note || null }) });
  } catch (problem) { toast(problem.message, "error"); }
  await refreshColumns(columns);
}

function cardEl(columns, card, byId, modelName) {
  const waiting = (card.depends_on || []).filter((d) => byId.get(d)?.status !== "done");
  const last = [...(card.comments || [])].reverse().find((c) => c.kind === "result" || c.kind === "error");
  const agent = card.agent_id ? columns.agents?.get(card.agent_id) : null;
  const meta = [
    agent ? agent.name : card.agent_id ? "a deleted agent" : card.endpoint_id ? modelName.get(card.endpoint_id) || "a removed model" : "Claude",
    waiting.length ? `waits for ${waiting.map((d) => byId.get(d)?.name || "a deleted card").join(", ")}` : "",
    card.attempts && card.status !== "done" ? `attempt ${card.attempts} of 3` : "",
  ].filter(Boolean).join(" · ");

  const node = el("div", { class: `board-card board-card-${card.status}`, "data-card": card.id }, [
    el("div", { class: "board-card-name", text: card.name }),
    el("div", { class: "meta", text: meta }),
  ]);
  if (last) {
    node.append(el("details", { class: "board-card-output", ...(card.status === "review" || card.status === "blocked" ? { open: "" } : {}) }, [
      el("summary", { text: last.kind === "error" ? "Last error" : "Result" }),
      el("div", { class: `board-card-text${last.kind === "error" ? " is-error" : ""}`, text: last.text }),
    ]));
  }
  if (card.last_run_at) node.append(historyEl(columns, card));

  const actions = el("div", { class: "board-card-actions" });
  const button = (text, onclick, cls = "btn") => el("button", { class: cls, text, onclick });
  const runNow = button("Run now", async (event) => {
    event.currentTarget.disabled = true;
    try { await api(`/api/tasks/${card.id}/run`, { method: "POST" }); } catch (problem) { toast(problem.message, "error"); }
    await refreshColumns(columns);
  });
  if (card.status === "backlog") actions.append(button("Ready", () => move(columns, card, "ready")), runNow);
  if (card.status === "ready") actions.append(button("Hold", () => move(columns, card, "backlog"), "btn quiet"), ...(waiting.length ? [] : [runNow]));
  if (card.status === "running") {
    const since = card.run_started_at
      ? ` (started ${new Date(card.run_started_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })})` : "";
    actions.append(el("span", { class: "meta", text: `Working on it${since}...` }));
  }
  if (card.status === "review") {
    const note = el("input", { class: "board-feedback", placeholder: "What should change?" });
    actions.append(
      button("Approve", () => move(columns, card, "done")),
      note,
      button("Request changes", () => {
        if (!note.value.trim()) { toast("Say what should change", "error"); return; }
        move(columns, card, "ready", note.value.trim());
      }, "btn quiet"),
    );
  }
  if (card.status === "blocked") actions.append(button("Retry", () => move(columns, card, "ready")));
  if (card.status === "done") actions.append(button("Reopen", () => move(columns, card, "ready"), "btn quiet"));
  if (card.status !== "running") {
    actions.append(button("Delete", async () => {
      const ok = await confirmDialog({ title: "Delete this card?", message: `"${card.name}" and its results will be deleted.`, confirmLabel: "Delete card" });
      if (!ok) return;
      await api(`/api/tasks/${card.id}`, { method: "DELETE" });
      await refreshColumns(columns);
    }, "btn danger"));
  }
  node.append(actions);
  return node;
}

// Every run of a card, fetched when opened rather than on each board poll.
function historyEl(columns, card) {
  const body = el("div");
  const history = el("details", { class: "board-card-output" }, [el("summary", { text: "Run history" }), body]);
  const load = async () => {
    history.dataset.loaded = "1";
    try {
      body.replaceChildren(runHistory(await api(`/api/tasks/${card.id}/runs`)));
    } catch (problem) {
      body.replaceChildren(el("div", { class: "board-card-text is-error", text: `Couldn't load the history: ${problem.message}` }));
    }
  };
  history.addEventListener("toggle", () => {
    if (!history.open) { columns.openHistory.delete(card.id); return; }
    columns.openHistory.add(card.id);
    if (!history.dataset.loaded) load();
  });
  if (columns.openHistory.has(card.id)) {
    history.open = true;
    load();
  }
  return history;
}
