import { api, el, customSelect, toast, confirmDialog, emptyState } from "../api.js";
import { ICONS } from "../icons.js";
import { runHistory } from "../runHistory.js";
import { mountAgentChat } from "../agentChat.js";

// Agents (2026-10-04, after xAI's Grok Bot and OpenAI's Dots): named workers
// with a role, standing goals, their own memory and an inbox. Their work runs
// as ordinary Tasks and board cards; services/agent_service.py and the vault
// spec "Agents - Phase 1 Persistent Agents (Build Spec)" have the design.
// Teams (phase 5, 2026-10-05) are Swarm companies, shown here through
// views/swarm.js in its embedded mode; Swarm has no tab of its own any more.

const COLORS = ["#b3a7f5", "#7dd3c0", "#f0b37e", "#e88f8f", "#8fb8e8", "#c9d67a"];
const STATUS = { idle: "Idle", working: "Working", needs_you: "Needs you", off: "Off", capped: "Done for today" };
const POLL_MS = 10000;

export async function render(container, tabId, options = {}) {
  let timer = null;
  const cleanups = [];
  const root = el("div", { class: "view-constrained agents-view" });
  container.innerHTML = "";
  container.append(root);
  const schedule = (fn) => { clearTimeout(timer); timer = setTimeout(() => document.body.contains(root) && fn(), POLL_MS); };
  if (options.team) {
    const swarm = await import("./swarm.js");
    return swarm.render(container, tabId, { ...options, embedded: true, systemId: options.team });
  }
  if (options.agentId) await agentPage(root, options.agentId, schedule, cleanups, options.agentTab || "chat");
  else await listPage(root, schedule);
  return () => { clearTimeout(timer); cleanups.forEach((cleanup) => cleanup()); };
}

export function avatar(agent, size = 28) {
  return el("span", { class: "agent-avatar", "aria-hidden": "true", text: (agent.name || "?").slice(0, 1).toUpperCase(),
    style: `background:${agent.color};width:${size}px;height:${size}px;font-size:${Math.round(size * 0.45)}px;` });
}

function open(agentId) {
  document.dispatchEvent(new CustomEvent("jarvis:navigate", { detail: { tab: "agents", agentId } }));
}

function openTeam(team) {
  document.dispatchEvent(new CustomEvent("jarvis:navigate", { detail: { tab: "agents", team } }));
}

const TEAM_STATE = { idle: "Idle", active: "Working", pausing: "Pausing", paused: "Paused", stopped: "Stopped", archived: "Archived" };

function modelOptions(models, selected = "") {
  return [
    el("option", { value: "", text: "Claude (default)", ...(selected ? {} : { selected: "" }) }),
    ...models.map((m) => el("option", { value: m.id, text: m.name, ...(m.id === selected ? { selected: "" } : {}) })),
  ];
}

function channelOptions(channels, selected = "") {
  return [
    el("option", { value: "", text: "In the app only", ...(selected ? {} : { selected: "" }) }),
    ...channels.map((c) => el("option", { value: c.id, text: c.label, ...(c.id === selected ? { selected: "" } : {}) })),
  ];
}

// -- the list ---------------------------------------------------------------

async function listPage(root, schedule) {
  const [agents, inbox, models, channels, teams] = await Promise.all([
    api("/api/agents"), api("/api/agents/inbox"), api("/api/models").catch(() => []), api("/api/channels").catch(() => []),
    api("/api/swarm/systems").catch(() => null),
  ]);
  if (!document.body.contains(root)) return;
  root.innerHTML = "";
  root.append(el("div", { class: "view-header" }, [el("div", {}, [
    el("h2", { text: "Agents" }),
    el("div", { class: "sub", text: "Named helpers that work in the background and come back when they need you." }),
  ])]));
  const byId = new Map(agents.map((a) => [a.id, a]));
  if (inbox.count) root.append(inboxPanel("Waiting on you", inbox.items, inbox.reviews, byId, () => listPage(root, schedule)));
  root.append(newAgentForm(models, channels, root, schedule));
  if (!agents.length) {
    root.append(emptyState({ icon: ICONS.agents, title: "No agents yet",
      hint: "Create one above: give it a name, a role, and something to watch or do." }));
    if (teams) root.append(teamsSection(teams));
    return;
  }
  const grid = el("div", { class: "agent-grid" });
  for (const agent of agents) {
    grid.append(el("button", { type: "button", class: "agent-tile", onclick: () => open(agent.id) }, [
      el("div", { class: "agent-tile-head" }, [avatar(agent, 34), el("div", { class: "agent-tile-name" }, [
        el("div", { class: "title", text: agent.name }),
        el("div", { class: "meta", text: agent.role || "No role yet" }),
      ])]),
      el("div", { class: "agent-tile-foot" }, [
        el("span", { class: `agent-status agent-status-${agent.status}`, text: STATUS[agent.status] || agent.status }),
        el("span", { class: "meta", text: agent.status === "working" ? agent.status_detail
          : `${agent.runs_today} of ${agent.daily_run_cap} runs today` }),
      ]),
    ]));
  }
  root.append(grid);
  if (teams) root.append(teamsSection(teams));
  if (agents.some((a) => a.status === "working")) schedule(() => listPage(root, schedule));
}

// Teams: a mission worked on by a lead and teammates, who can be your agents.
// null when Swarm is unavailable, so the section is left out, not faked.
function teamsSection(teams) {
  const grid = el("div", { class: "agent-grid" });
  for (const team of teams.items) {
    grid.append(el("button", { type: "button", class: "agent-tile team-tile", onclick: () => openTeam(team.id) }, [
      el("div", { class: "agent-tile-name" }, [
        el("div", { class: "title", text: team.name }),
        el("div", { class: "meta team-mission", text: team.mission }),
      ]),
      el("div", { class: "agent-tile-foot" }, [
        el("span", { class: `agent-status team-state-${team.state}`, text: TEAM_STATE[team.state] || team.state }),
        el("span", { class: "meta", text: team.active_tasks ? `${team.active_tasks} working now` : "" }),
      ]),
    ]));
  }
  return el("section", { class: "agent-teams", "aria-label": "Teams" }, [
    el("div", { class: "view-header agent-teams-header" }, [
      el("div", {}, [el("h3", { text: "Teams" }),
        el("div", { class: "sub", text: "Agents working together on one mission: a lead plans and reviews, teammates do the work." })]),
      el("button", { class: "btn", text: "+ New team", onclick: () => openTeam("new") }),
    ]),
    teams.items.length ? grid : el("div", { class: "meta", text: "No teams yet." }),
  ]);
}

function newAgentForm(models, channels, root, schedule) {
  const form = el("details", { class: "disclosure-panel" });
  const name = el("input", { placeholder: "e.g. Scout" });
  const role = el("input", { placeholder: "e.g. Watches my job applications and new postings" });
  const instructions = el("textarea", { rows: "3", placeholder: "How it should work: sources to use, what to leave out, how to report." });
  const model = customSelect({}, modelOptions(models));
  const channel = customSelect({}, channelOptions(channels));
  let color = COLORS[0];
  const swatches = el("div", { class: "agent-swatches", role: "radiogroup", "aria-label": "Color" });
  const paint = () => swatches.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.color === color)));
  for (const c of COLORS) {
    swatches.append(el("button", { type: "button", role: "radio", "data-color": c, "aria-label": c, style: `background:${c}`,
      onclick: () => { color = c; paint(); } }));
  }
  paint();
  const create = async () => {
    if (!name.value.trim()) { toast("Give the agent a name", "error"); return; }
    try {
      const agent = await api("/api/agents", { method: "POST", body: JSON.stringify({
        name: name.value.trim(), role: role.value.trim(), instructions: instructions.value.trim(),
        endpoint_id: model.value || null, color, deliver_to_channel: channel.value || null }) });
      toast(`${agent.name} is ready`, "success");
      open(agent.id);
    } catch (problem) { toast(problem.message, "error"); }
  };
  form.append(el("summary", { text: "+ New agent" }), el("div", { class: "form-grid" }, [
    el("div", { class: "field" }, [el("label", { text: "Name" }), name]),
    el("div", { class: "field field-grow" }, [el("label", { text: "Role" }), role]),
    el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [el("label", { text: "How it should work" }), instructions]),
    el("div", { class: "field" }, [el("label", { text: "Model" }), model]),
    el("div", { class: "field" }, [el("label", { text: "Tell me on" }), channel]),
    el("div", { class: "field" }, [el("label", { text: "Color" }), swatches]),
    el("button", { class: "btn primary", text: "Create agent", onclick: create }),
  ]));
  return form;
}

// -- the inbox (shared by the list and the agent page) ----------------------

function inboxPanel(title, items, reviews, byId, refresh) {
  const panel = el("div", { class: "glass card agent-inbox" }, [el("div", { class: "title", text: title })]);
  for (const item of items) panel.append(inboxItem(item, byId.get(item.agent_id), refresh));
  for (const card of reviews) panel.append(reviewItem(card, byId.get(card.agent_id), refresh));
  if (!items.length && !reviews.length) panel.append(el("div", { class: "meta", text: "Nothing is waiting on you." }));
  return panel;
}

async function answer(item, choice, text, refresh) {
  try {
    const result = await api(`/api/agents/inbox/${item.id}/answer`, { method: "POST", body: JSON.stringify({ choice, text: text || null }) });
    toast(result.next ? result.next[0].toUpperCase() + result.next.slice(1) : "Done", "success");
  } catch (problem) { toast(problem.message, "error"); }
  document.dispatchEvent(new CustomEvent("jarvis:agents-changed"));
  await refresh();
}

function inboxItem(item, agent, refresh) {
  const kind = { question: "Question", report: "Report" }[item.kind];
  const reply = el("input", { class: "agent-reply", placeholder: item.kind === "question" ? "Your answer" : "Reply or guidance (optional)" });
  const button = (text, onclick, cls = "btn") => el("button", { class: cls, text, onclick });
  const send = (choice) => () => {
    if (choice === "reply" && !reply.value.trim()) { toast("Write a reply first", "error"); return; }
    answer(item, choice, reply.value.trim(), refresh);
  };
  const actions = el("div", { class: "agent-inbox-actions" });
  if (item.kind === "question") {
    actions.append(reply, button("Answer", send("reply"), "btn primary"), button("Dismiss", send("dismiss"), "btn quiet"));
  } else {
    actions.append(button("Dismiss", send("dismiss")), reply, button("Reply", send("reply"), "btn quiet"));
  }
  return el("div", { class: `agent-inbox-item agent-inbox-${item.kind}` }, [
    el("div", { class: "agent-inbox-head" }, [
      agent ? avatar(agent, 22) : el("span"),
      el("span", { class: "agent-inbox-kind", text: kind }),
      el("span", { class: "agent-inbox-title", text: `${agent?.name || "Agent"}: ${item.title}` }),
      el("span", { class: "meta", text: new Date(item.created_at * 1000).toLocaleString() }),
    ]),
    item.body ? el("div", { class: "agent-inbox-body", text: item.body }) : el("span"),
    actions,
  ]);
}

function reviewItem(card, agent, refresh) {
  const result = [...(card.comments || [])].reverse().find((c) => c.kind === "result");
  const note = el("input", { class: "agent-reply", placeholder: "What should change? (it remembers this)" });
  const move = async (status, text) => {
    try {
      await api(`/api/tasks/${card.id}/status`, { method: "POST", body: JSON.stringify({ status, note: text || null }) });
    } catch (problem) { toast(problem.message, "error"); }
    document.dispatchEvent(new CustomEvent("jarvis:agents-changed"));
    await refresh();
  };
  return el("div", { class: "agent-inbox-item agent-inbox-review" }, [
    el("div", { class: "agent-inbox-head" }, [
      agent ? avatar(agent, 22) : el("span"),
      el("span", { class: "agent-inbox-kind", text: "Result" }),
      el("span", { class: "agent-inbox-title", text: `${agent?.name || "Agent"}: ${card.name}` }),
    ]),
    el("div", { class: "agent-inbox-body", text: result ? result.text : "(no result text)" }),
    el("div", { class: "agent-inbox-actions" }, [
      el("button", { class: "btn primary", text: "Approve", onclick: () => move("done") }),
      note,
      el("button", { class: "btn quiet", text: "Request changes", onclick: () => {
        if (!note.value.trim()) { toast("Say what should change", "error"); return; }
        move("ready", note.value.trim());
      } }),
    ]),
  ]);
}

// -- one agent ----------------------------------------------------------------

async function agentPage(root, agentId, schedule, cleanups, tab = "chat") {
  let detail;
  try {
    detail = await api(`/api/agents/${agentId}`);
  } catch {
    root.innerHTML = "";
    root.append(emptyState({ icon: ICONS.agents, title: "That agent no longer exists", hint: "Go back to Agents to see the others." }));
    return;
  }
  const [models, channels] = await Promise.all([api("/api/models").catch(() => []), api("/api/channels").catch(() => [])]);
  if (!document.body.contains(root)) return;
  root.innerHTML = "";

  // Built once: the header and the two tabs. Only the header's status and
  // the Work tab redraw while the agent is busy, so an open chat is never
  // rebuilt under the person typing in it.
  const header = el("div", { class: "agent-header" });
  const chatTab = el("button", { type: "button", role: "tab", class: "agent-tab", text: "Chat" });
  const workTab = el("button", { type: "button", role: "tab", class: "agent-tab" });
  const chatHost = el("div", { role: "tabpanel", class: "agent-tab-panel agent-chat-host" });
  const workHost = el("div", { role: "tabpanel", class: "agent-tab-panel agent-work-host" });
  let chatMounted = false;
  const show = (which) => {
    for (const [button, host, name] of [[chatTab, chatHost, "chat"], [workTab, workHost, "work"]]) {
      button.setAttribute("aria-selected", String(which === name));
      button.classList.toggle("active", which === name);
      host.hidden = which !== name;
    }
    if (which === "chat" && !chatMounted) {
      chatMounted = true;
      mountAgentChat(chatHost, detail.agent).then((cleanup) => cleanups.push(cleanup));
    }
  };
  chatTab.addEventListener("click", () => show("chat"));
  workTab.addEventListener("click", () => show("work"));
  root.append(
    el("button", { class: "btn quiet agent-back", text: "← All agents", onclick: () => document.dispatchEvent(new CustomEvent("jarvis:navigate", { detail: { tab: "agents" } })) }),
    header,
    el("div", { class: "agent-tabs", role: "tablist", "aria-label": "Agent" }, [chatTab, workTab]),
    chatHost, workHost,
  );

  const drawHeader = (agent) => {
    const toggle = el("button", { class: agent.enabled ? "btn" : "btn primary", text: agent.enabled ? "Turn off" : "Turn on",
      onclick: async () => { await api(`/api/agents/${agentId}`, { method: "PATCH", body: JSON.stringify({ enabled: !agent.enabled }) }); redraw(); } });
    header.replaceChildren(
      avatar(agent, 48),
      el("div", { class: "agent-header-text" }, [
        el("h2", { text: agent.name }),
        el("div", { class: "sub", text: agent.role || "No role yet" }),
        el("div", { class: "agent-header-meta" }, [
          el("span", { class: `agent-status agent-status-${agent.status}`, text: STATUS[agent.status] || agent.status }),
          el("span", { class: "meta", text: [agent.status_detail, `${agent.runs_today} of ${agent.daily_run_cap} runs today`]
            .filter(Boolean).join(" · ") }),
        ]),
      ]),
      el("div", { class: "agent-header-actions" }, [toggle]),
    );
    workTab.textContent = agent.needs_you ? `Work (${agent.needs_you})` : "Work";
  };

  const drawWork = () => {
    const { agent } = detail;
    const reviews = detail.cards.filter((c) => c.status === "review");
    const byId = new Map([[agent.id, agent]]);
    workHost.replaceChildren(
      inboxPanel("Inbox", detail.inbox, reviews, byId, redraw),
      goalsPanel(agentId, detail.goals, redraw),
      workPanel(agentId, detail.cards, redraw),
      memoryPanel(agentId, detail.memory),
      el("div", { class: "glass card" }, [el("div", { class: "title", text: "History" }), runHistory(detail.runs)]),
      teamsPanel(detail.teams || []),
      settingsPanel(agent, models, channels, redraw),
    );
  };

  async function redraw() {
    try { detail = await api(`/api/agents/${agentId}`); } catch { return; }
    if (!document.body.contains(root)) return;
    drawHeader(detail.agent);
    drawWork();
    if (detail.agent.status === "working") schedule(redraw);
  }

  drawHeader(detail.agent);
  drawWork();
  show(tab);
  if (detail.agent.status === "working") schedule(redraw);
}

function teamsPanel(teams) {
  const panel = el("div", { class: "glass card agent-teams-panel" }, [
    el("div", { class: "title", text: "Teams" }),
    el("div", { class: "meta", style: "margin:4px 0 10px;", text: teams.length
      ? "On a team it works only with the team's tools. What it learns there comes back here as reports and corrections."
      : "Not on a team. Add it to one from Teams on the Agents page." }),
  ]);
  for (const team of teams) {
    panel.append(el("div", { class: "card-row", style: "justify-content:space-between;gap:10px;" }, [
      el("span", { text: `${team.name} · ${team.is_lead ? "lead" : "teammate"} · ${TEAM_STATE[team.state] || team.state}` }),
      el("button", { class: "btn quiet", text: "Open", onclick: () => openTeam(team.id) }),
    ]));
  }
  return panel;
}

function cadence(goal) {
  if (goal.schedule_kind === "daily") return `Every day at ${goal.run_time}`;
  const minutes = Math.round((goal.interval_seconds || 0) / 60);
  return minutes % 60 === 0 ? `Every ${minutes / 60 === 1 ? "hour" : `${minutes / 60} hours`}` : `Every ${minutes} minutes`;
}

function goalsPanel(agentId, goals, refresh) {
  const panel = el("div", { class: "glass card" }, [
    el("div", { class: "title", text: "Standing goals" }),
    el("div", { class: "meta", style: "margin:4px 0 10px;", text: "Checked on a schedule. Quiet goals only report when something turned up." }),
  ]);
  for (const goal of goals) {
    panel.append(el("div", { class: "agent-row" }, [
      el("div", {}, [el("div", { text: goal.name }), el("div", { class: "meta", text:
        `${cadence(goal)} · ${goal.report_when === "always" ? "always reports" : "reports when notable"}${goal.enabled ? "" : " · off"}` })]),
      el("div", { class: "agent-row-actions" }, [
        el("button", { class: "btn quiet", text: "Check now", onclick: async () => {
          try { await api(`/api/tasks/${goal.id}/run`, { method: "POST" }); toast("Checked", "success"); }
          catch (problem) { toast(problem.message, "error"); }
          refresh();
        } }),
        el("button", { class: "btn danger", text: "Remove", onclick: async () => {
          if (!await confirmDialog({ title: "Remove this goal?", message: `"${goal.name}" will stop being checked.`, confirmLabel: "Remove" })) return;
          await api(`/api/tasks/${goal.id}`, { method: "DELETE" }); refresh();
        } }),
      ]),
    ]));
  }
  const name = el("input", { placeholder: "e.g. New postings" });
  const prompt = el("textarea", { rows: "2", placeholder: "What to check, and what counts as worth telling you" });
  const kind = customSelect({}, [el("option", { value: "daily", text: "Every day at" }), el("option", { value: "interval", text: "Every N hours" })]);
  const time = el("input", { type: "time", value: "08:00" });
  const hours = el("input", { type: "number", min: "1", value: "6" });
  const timeField = el("div", { class: "field field-sm" }, [el("label", { text: "At" }), time]);
  const hoursField = el("div", { class: "field field-sm", style: "display:none;" }, [el("label", { text: "Hours" }), hours]);
  kind.addEventListener("change", () => {
    timeField.style.display = kind.value === "daily" ? "" : "none";
    hoursField.style.display = kind.value === "interval" ? "" : "none";
  });
  const report = customSelect({}, [el("option", { value: "notable", text: "Only when notable" }), el("option", { value: "always", text: "Every time" })]);
  const add = async () => {
    if (!name.value.trim() || !prompt.value.trim()) { toast("A goal needs a name and what to check", "error"); return; }
    const body = { name: name.value.trim(), prompt: prompt.value.trim(), schedule_kind: kind.value, report_when: report.value };
    if (kind.value === "daily") body.run_time = time.value; else body.interval_seconds = Math.max(1, parseInt(hours.value, 10) || 1) * 3600;
    try { await api(`/api/agents/${agentId}/goals`, { method: "POST", body: JSON.stringify(body) }); refresh(); }
    catch (problem) { toast(problem.message, "error"); }
  };
  panel.append(el("details", { class: "disclosure-panel" }, [el("summary", { text: "+ Add a goal" }), el("div", { class: "form-grid" }, [
    el("div", { class: "field field-grow" }, [el("label", { text: "Name" }), name]),
    el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [el("label", { text: "What to check" }), prompt]),
    el("div", { class: "field" }, [el("label", { text: "When" }), kind]), timeField, hoursField,
    el("div", { class: "field" }, [el("label", { text: "Report" }), report]),
    el("button", { class: "btn primary", text: "Add goal", onclick: add }),
  ])]));
  return panel;
}

function workPanel(agentId, cards, refresh) {
  const panel = el("div", { class: "glass card" }, [el("div", { class: "title", text: "Work" })]);
  const open = cards.filter((c) => c.status !== "done");
  if (!open.length) panel.append(el("div", { class: "meta", style: "margin:6px 0;", text: "Nothing assigned right now." }));
  const label = { backlog: "Backlog", ready: "Up next", running: "Working on it", review: "Waiting for your review", blocked: "Blocked" };
  for (const card of open) {
    panel.append(el("div", { class: "agent-row" }, [
      el("div", {}, [el("div", { text: card.name }), el("div", { class: "meta", text: label[card.status] || card.status })]),
    ]));
  }
  const name = el("input", { placeholder: "e.g. Shortlist five roles" });
  const prompt = el("textarea", { rows: "2", placeholder: "What to do, as you would tell a person" });
  panel.append(el("details", { class: "disclosure-panel" }, [el("summary", { text: "+ Give it a job" }), el("div", { class: "form-grid" }, [
    el("div", { class: "field field-grow" }, [el("label", { text: "Name" }), name]),
    el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [el("label", { text: "What to do" }), prompt]),
    el("button", { class: "btn primary", text: "Assign", onclick: async () => {
      if (!name.value.trim() || !prompt.value.trim()) { toast("A job needs a name and what to do", "error"); return; }
      try { await api(`/api/agents/${agentId}/cards`, { method: "POST", body: JSON.stringify({ name: name.value.trim(), prompt: prompt.value.trim() }) }); refresh(); }
      catch (problem) { toast(problem.message, "error"); }
    } }),
  ])]));
  return panel;
}

function memoryPanel(agentId, memory) {
  const area = el("textarea", { class: "agent-memory", rows: "12", spellcheck: "false" });
  area.value = memory;
  const count = el("span", { class: "meta" });
  const update = () => { count.textContent = `${area.value.length} / 8000`; count.classList.toggle("is-error", area.value.length > 8000); };
  area.addEventListener("input", update);
  update();
  return el("div", { class: "glass card" }, [
    el("div", { class: "title", text: "Memory" }),
    el("div", { class: "meta", style: "margin:4px 0 10px;", text: "What it carries into every run. It adds to this itself; corrections land here too. Edit freely." }),
    area,
    el("div", { class: "agent-row-actions", style: "margin-top:8px;" }, [count, el("button", { class: "btn", text: "Save memory", onclick: async () => {
      try { await api(`/api/agents/${agentId}/memory`, { method: "PUT", body: JSON.stringify({ text: area.value }) }); toast("Memory saved", "success"); }
      catch (problem) { toast(problem.message, "error"); }
    } })]),
  ]);
}

function settingsPanel(agent, models, channels, refresh) {
  const role = el("input", { value: agent.role || "" });
  const instructions = el("textarea", { rows: "3" });
  instructions.value = agent.instructions || "";
  const model = customSelect({}, modelOptions(models, agent.endpoint_id || ""));
  const channel = customSelect({}, channelOptions(channels, agent.deliver_to_channel || ""));
  const cap = el("input", { type: "number", min: "1", max: "200", value: String(agent.daily_run_cap) });
  const save = async () => {
    try {
      await api(`/api/agents/${agent.id}`, { method: "PATCH", body: JSON.stringify({ role: role.value.trim(), instructions: instructions.value.trim(),
        endpoint_id: model.value || null, deliver_to_channel: channel.value || null, daily_run_cap: parseInt(cap.value, 10) }) });
      toast("Saved", "success"); refresh();
    } catch (problem) { toast(problem.message, "error"); }
  };
  const remove = async () => {
    const ok = await confirmDialog({ title: `Delete ${agent.name}?`,
      message: "Its memory, goals, unfinished work and inbox go. Its chats move to Chats; finished results and run history stay.", confirmLabel: "Delete agent" });
    if (!ok) return;
    try {
      await api(`/api/agents/${agent.id}`, { method: "DELETE" });
      document.dispatchEvent(new CustomEvent("jarvis:navigate", { detail: { tab: "agents" } }));
    } catch (problem) { toast(problem.message, "error"); }
  };
  return el("details", { class: "disclosure-panel" }, [el("summary", { text: "Settings" }), el("div", { class: "form-grid" }, [
    el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [el("label", { text: "Role" }), role]),
    el("div", { class: "field field-grow", style: "flex-basis:100%;" }, [el("label", { text: "How it should work" }), instructions]),
    el("div", { class: "field" }, [el("label", { text: "Model" }), model]),
    el("div", { class: "field" }, [el("label", { text: "Tell me on" }), channel]),
    el("div", { class: "field field-sm" }, [el("label", { text: "Runs per day" }), cap]),
    el("button", { class: "btn primary", text: "Save", onclick: save }),
    el("button", { class: "btn danger", text: "Delete agent", onclick: remove }),
  ])]);
}
