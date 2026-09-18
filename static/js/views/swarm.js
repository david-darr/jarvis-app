import { api, el, toast } from "../api.js";
import { subscribe } from "../swarmEvents.js";
import { createBoard } from "../swarmBoard.js";
import { createGraph } from "../swarmGraph.js";

const drafts = new Map();
const command = () => crypto.randomUUID();
const label = (text, control) => el("label", { class: "swarm-field" }, [el("span", { text }), control]);
const button = (text, onclick, attrs = {}) => el("button", { type: "button", class: "btn", text, onclick, "data-focus-key": text, ...attrs });
const pretty = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);
const time = (seconds) => seconds ? new Date(seconds * 1000).toLocaleString() : "Not recorded";
const initialLimit = (ceiling = 25000) => ({ ceiling, pause_percent: 80, checkpoint_reserve: 0 });

export function render(root, _tab, options = {}) {
  root.className = "view active swarm-view";
  const abort = new AbortController();
  let disposed = false, version = 0, selected = null, current = null, unsubscribe = null, refreshTimer = null;
  let modal = null, panel = "tasks", pages = {}, ui = null;
  let workView = "chat", board = null, graph = null;
  let connections = null, catalog = null;
  const request = (path, opts = {}) => api("/api/swarm" + path, { ...opts, signal: abort.signal });
  const mutate = (path, body, method = "POST") => request(path, { method, body: JSON.stringify(body) });
  const clearStream = () => { unsubscribe?.(); unsubscribe = null; clearTimeout(refreshTimer); };
  const dropViews = () => { board?.dispose(); graph?.dispose(); board = null; graph = null; };
  const cleanup = () => { disposed = true; version++; clearStream(); dropViews(); abort.abort(); modal?.remove(); };
  options.registerCleanup?.(cleanup);

  function error(node, problem, retry) {
    if (disposed || problem.name === "AbortError") return;
    node.replaceChildren(el("p", { role: "alert", class: "swarm-error", text: problem.message }), button("Try again", retry));
  }

  function showDialog(title, build) {
    modal?.remove();
    const previous = document.activeElement;
    const heading = el("h2", { id: "swarm-dialog-title", text: title });
    const body = el("div", { class: "swarm-dialog-body" });
    const dialog = el("dialog", { class: "swarm-dialog", "aria-labelledby": "swarm-dialog-title" }, [
      el("div", { class: "swarm-dialog-header" }, [heading, button("Close", () => dialog.close())]), body,
    ]);
    modal = dialog;
    dialog.addEventListener("close", () => {
      dialog.remove(); if (modal === dialog) modal = null;
      const replacement = previous?.dataset.focusKey && root.querySelector(`[data-focus-key="${CSS.escape(previous.dataset.focusKey)}"]`);
      (previous?.isConnected ? previous : replacement)?.focus();
    });
    root.append(dialog);
    build(body, () => dialog.close());
    dialog.showModal();
  }

  const KINDS = { claude_cli: "Claude CLI", codex_cli: "Codex CLI", local: "Local server", api: "API" };

  // The same two endpoints the chat composer uses: the connection list is
  // admin-gated and degrades to empty, and the catalog carries only public
  // model names and effort levels. Swarm adds no route of its own, so the
  // admin gate on connection records is exactly as it was.
  async function loadConnections() {
    if (connections === null) {
      const [list, models] = await Promise.all([
        api("/api/models", { signal: abort.signal }).catch(() => []),
        api("/api/models/catalog", { signal: abort.signal }).catch(() => ({})),
      ]);
      connections = Array.isArray(list) ? list : [];
      catalog = models && typeof models === "object" ? models : {};
    }
    return connections;
  }

  const connectionName = (id) => (connections || []).find(item => item.id === id)?.name || null;

  function connectionFields(data) {
    const connection = el("select", { "aria-label": "Model connection" },
      [el("option", { value: "", text: connections.length ? "No connection yet" : "No connections available" }),
       ...connections.map(item => el("option", { value: item.id, text: `${item.name} · ${KINDS[item.kind] || item.kind}` }))]);
    connection.value = data?.endpoint_id || "";
    const model = el("select", { "aria-label": "Model" });
    const effort = el("select", { "aria-label": "Reasoning effort" });
    const chosen = () => connections.find(item => item.id === connection.value) || null;

    function fillEffort(keepValue) {
      const entries = catalog[chosen()?.kind] || [];
      // With a model pinned, its own efforts. Without one, every effort the
      // kind advertises - which is exactly what the server validates against
      // when no model is chosen, so the control cannot offer what would be
      // refused on save.
      const entry = entries.find(item => item.id === model.value);
      const supported = entry ? (entry.supported_efforts || [])
        : [...new Map(entries.flatMap(item => item.supported_efforts || [])
            .map(item => [item.effort, item])).values()];
      effort.replaceChildren(el("option", { value: "", text: supported.length ? "Default effort" : "Not supported here" }),
        ...supported.map(item => el("option", { value: item.effort, text: item.effort })));
      effort.disabled = !supported.length;
      if (keepValue && supported.some(item => item.effort === keepValue)) effort.value = keepValue;
    }

    function fillModel(keepModel, keepEffort) {
      const endpoint = chosen();
      const available = catalog[endpoint?.kind] || [];
      model.replaceChildren(el("option", { value: "", text: endpoint?.model ? `Connection default (${endpoint.model})` : "Connection default" }),
        ...available.map(item => el("option", { value: item.id, text: item.display_name || item.id })));
      model.disabled = !endpoint || !available.length;
      if (keepModel && available.some(item => item.id === keepModel)) model.value = keepModel;
      fillEffort(keepEffort);
    }

    connection.addEventListener("change", () => fillModel(null, null));
    model.addEventListener("change", () => fillEffort(null));
    fillModel(data?.model, data?.effort);
    return {
      node: el("fieldset", { class: "swarm-connection" }, [el("legend", { text: "Model connection" }),
        label("Connection", connection), label("Model", model), label("Effort", effort)]),
      read: () => ({ endpoint_id: connection.value || null, model: model.value || null, effort: effort.value || null }),
    };
  }

  function limitFields(title, value) {
    value ||= initialLimit();
    const ceiling = el("input", { type: "number", min: "2", max: "1000000000", step: "1", value: value.ceiling, required: true });
    const threshold = el("input", { type: "number", min: "1", max: "100", step: "1", value: value.pause_percent, required: true });
    const reserve = el("input", { type: "number", min: "0", step: "1", value: value.checkpoint_reserve, required: true });
    return {
      node: el("fieldset", { class: "swarm-limits" }, [el("legend", { text: title }), label("Token ceiling", ceiling), label("Pause at %", threshold), label("Checkpoint reserve", reserve)]),
      read: () => ({ ceiling: Number(ceiling.value), pause_percent: Number(threshold.value), checkpoint_reserve: Number(reserve.value) }),
    };
  }

  async function setup(snapshot = null) {
    const setupVersion = version;
    const editingId = snapshot?.system.id;
    let pools;
    try { [pools] = await Promise.all([request("/pools"), loadConnections()]); }
    catch (problem) { toast(problem.message, "error"); return; }
    if (disposed || setupVersion !== version) return;
    showDialog(snapshot ? "Edit system" : "Create system", (host, close) => {
      const form = el("form", { class: "swarm-setup" });
      const name = el("input", { name: "name", required: true, maxlength: "120", value: snapshot?.system.name || "" });
      const mission = el("textarea", { name: "mission", required: true, maxlength: "10000", rows: "3" });
      mission.value = snapshot?.system.mission || "";
      const mode = el("select", { name: "mode" }, [el("option", { value: "guided", text: "Guided" }), el("option", { value: "autonomous", text: "Autonomous" })]);
      mode.value = snapshot?.system.configuration.mode || "autonomous";
      const findLimit = (scope, id) => snapshot?.budgets.find(b => b.scope === scope && b.target === id);
      const systemLimit = limitFields("Company allocation", findLimit("system", editingId) || initialLimit(100000));
      const runLimit = limitFields("Per-run allocation", snapshot?.system.configuration.run_limit || initialLimit());
      const lead = snapshot?.agents.find(a => a.is_lead);
      const pool = el("select", { name: "pool" }, [el("option", { value: "", text: "New local allocation group" }), ...pools.map(p => el("option", { value: p.id, text: p.name }))]);
      if (snapshot) { pool.value = lead.pool_id; pool.disabled = true; }
      const poolLimit = limitFields("New shared allocation", initialLimit(200000));
      const poolWrap = el("div", {}, [poolLimit.node]);
      const updatePool = () => { poolWrap.hidden = !!snapshot || !!pool.value; };
      pool.addEventListener("change", updatePool); updatePool();
      form.append(label("System name", name), label("Mission", mission), label("Intended operating mode", mode),
        el("p", { class: "muted", text: connections.length
          ? "Give every teammate a connection. Workers have no shell, file or vault access - they plan, research, review and write. A connection that cannot take actions is flagged on the company once you save."
          : "No model connections are available to you. Add one in Settings (or ask an admin) before this company can run." }));
      const members = [];
      const teamHost = el("div", { class: "swarm-form-team" });
      function memberFields(data, isLead) {
        const agentName = el("input", { required: true, maxlength: "120", value: data?.name || (isLead ? "CEO / PM" : "") });
        const role = el("input", { required: true, maxlength: "120", value: data?.role || (isLead ? "Lead" : "") });
        const instructions = el("textarea", { rows: "2", maxlength: "10000" });
        instructions.value = data?.instructions || "";
        const limits = limitFields("Agent allocation", findLimit("agent", data?.id) || initialLimit());
        const connection = connectionFields(data);
        const row = el("fieldset", { class: "swarm-member-form" }, [el("legend", { text: isLead ? "Lead agent" : "Specialist" }),
          label("Name", agentName), label("Role", role), label("Responsibilities", instructions), connection.node, limits.node]);
        const item = { node: row, isLead, read: () => ({ id: data?.id || null, name: agentName.value.trim(), role: role.value.trim(), instructions: instructions.value, limit: limits.read(), ...connection.read() }) };
        members.push(item);
        if (!isLead) row.append(button("Remove specialist", () => { members.splice(members.indexOf(item), 1); row.remove(); }));
        teamHost.append(row);
      }
      memberFields(lead, true);
      for (const agent of snapshot?.agents.filter(a => !a.is_lead && a.enabled) || []) memberFields(agent, false);
      const add = button("Add specialist", () => { if (members.length < 21) memberFields(null, false); });
      const limits = el("details", { class: "disclosure-panel" }, [el("summary", { text: "Usage limits" }),
        el("p", { class: "muted", text: "These are local token allocations, not your provider's remaining allowance. Provider quota is unavailable until connected. Sharing a group combines its allocation across your systems." }),
        systemLimit.node, runLimit.node, label("Shared allocation group", pool), poolWrap]);
      const status = el("p", { role: "alert", class: "swarm-error" });
      const save = el("button", { type: "submit", class: "btn primary", text: "Save system" });
      form.append(teamHost, add, limits, status, el("div", { class: "swarm-actions" }, [button("Cancel", close), save]));
      let pendingCommand = null, pendingPayload = null;
      form.addEventListener("submit", async (event) => {
        event.preventDefault(); if (save.disabled) return;
        const payload = { name: name.value.trim(), mission: mission.value.trim(), mode: mode.value,
          system_limit: systemLimit.read(), run_limit: runLimit.read(), pool_limit: poolLimit.read(), pool_id: pool.value || null,
          lead: members.find(m => m.isLead).read(), specialists: members.filter(m => !m.isLead).map(m => m.read()) };
        if (snapshot) payload.expected_revision = snapshot.system.revision;
        const encoded = JSON.stringify(payload);
        if (pendingPayload !== encoded) { pendingCommand = command(); pendingPayload = encoded; }
        save.disabled = true; status.textContent = "";
        try {
          const result = await mutate(editingId ? `/systems/${editingId}` : "/systems", { ...payload, command_id: pendingCommand }, editingId ? "PATCH" : "POST");
          close(); if (!disposed) await openSystem(result.id);
        } catch (problem) { if (!disposed) status.textContent = problem.message; }
        finally { save.disabled = false; }
      });
      host.append(form);
    });
  }

  async function home(offset = 0) {
    clearStream(); dropViews(); selected = null; current = null; ui = null;
    const token = ++version;
    const list = el("div", { class: "swarm-system-grid", "aria-live": "polite" }, [el("p", { text: "Loading systems…" })]);
    root.replaceChildren(el("header", { class: "view-header" }, [el("div", {}, [el("h1", { text: "Swarm" }), el("p", { class: "muted", text: "A team for every idea." })]), button("Create system", () => setup(), { class: "btn primary" })]), list);
    try {
      const [result, availability] = await Promise.all([request(`/systems?offset=${offset}`), request("/status")]);
      if (disposed || token !== version) return;
      list.replaceChildren();
      if (!availability.available) throw new Error(availability.detail);
      if (!result.total) list.append(el("div", { class: "swarm-empty" }, [el("h2", { text: "Your first team starts here" }), el("p", { text: "Create a system, give it a mission, and choose its lead and specialists." }), button("Create your first system", () => setup())]));
      for (const system of result.items) {
        const card = button("", () => openSystem(system.id), { class: "swarm-system-card", "aria-label": `Open ${system.name}` });
        card.append(el("span", { class: "swarm-state", text: system.state }), el("h2", { text: system.name }),
          el("p", { text: system.mission }), el("span", { class: "muted", text: `${system.active_tasks} active tasks · Provider quota unavailable` }));
        list.append(card);
      }
      const pager = el("div", { class: "swarm-actions" });
      if (offset) pager.append(button("Previous systems", () => home(Math.max(0, offset - 50))));
      if (offset + result.items.length < result.total) pager.append(button("More systems", () => home(offset + result.items.length)));
      list.append(pager);
    } catch (problem) { if (token === version) error(list, problem, () => home(offset)); }
  }

  async function openSystem(id) {
    clearStream(); dropViews(); selected = id; ui = null;
    const token = ++version;
    root.replaceChildren(el("p", { role: "status", text: "Loading system…" }));
    try {
      const [snapshot] = await Promise.all([request(`/systems/${id}`), loadConnections()]);
      if (disposed || token !== version) return;
      current = snapshot; pages = snapshot.pages;
      workspace(); update(snapshot);
      unsubscribe = subscribe(id, snapshot.event_cursor, {
        onStatus: text => { if (ui && token === version) ui.connection.textContent = text; },
        onChange: () => { clearTimeout(refreshTimer); refreshTimer = setTimeout(() => refresh(token), 100); },
      });
    } catch (problem) { if (token === version) error(root, problem, () => openSystem(id)); }
  }

  async function refresh(token = version) {
    try {
      const snapshot = await request(`/systems/${selected}`);
      if (disposed || token !== version) return;
      current = snapshot; pages = snapshot.pages; update(snapshot);
    } catch (problem) { if (!disposed && token === version) ui.connection.textContent = `Refresh failed: ${problem.message}`; }
  }

  async function lifecycle(action) {
    const id = selected, snapshot = current, token = version;
    async function perform() {
      if (!ui || token !== version || ui.busy) return;
      ui.busy = true;
      try {
        await mutate(`/systems/${id}/${action}`, { command_id: command(), expected_revision: snapshot.system.revision });
        await refresh(token);
      } catch (problem) { if (token === version) ui.notice.textContent = problem.message; }
      finally { if (ui && token === version) ui.busy = false; }
    }
    if (action === "stop" || action === "archive") showDialog(action === "stop" ? "Stop this run?" : "Archive this system?", (host, close) => {
      host.append(el("p", { text: action === "stop" ? "The current run will be cancelled. Saved work and uncertain actions remain available." : "The system becomes read-only until restored." }),
        button("Cancel", close), button(action === "stop" ? "Stop run" : "Archive", () => { close(); perform(); }, { class: "btn danger" }));
    });
    else await perform();
  }

  function workspace() {
    const heading = el("h1");
    const state = el("span", { class: "swarm-state" });
    const notice = el("p", { class: "swarm-notice", role: "status" });
    const controls = el("div", { class: "swarm-actions" });
    const team = el("section", { class: "swarm-team", "aria-label": "Team" });
    const messages = el("div", { class: "swarm-messages", "aria-live": "polite" });
    const composer = el("textarea", { "aria-label": "Message your lead", placeholder: "Give your lead an idea…", rows: "3", maxlength: "20000", required: true });
    composer.value = drafts.get(selected) || "";
    const composeStatus = el("p", { role: "status", class: "muted" });
    const send = el("button", { type: "submit", class: "btn primary", text: "Queue idea" });
    const form = el("form", { class: "swarm-composer" }, [composer, el("div", { class: "swarm-actions" }, [composeStatus, send])]);
    const companyId = selected;
    composer.addEventListener("input", () => drafts.set(companyId, composer.value));
    let retryId = null, retryText = null;
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (send.disabled || ui.sending || !composer.value.trim()) return;
      const body = composer.value.trim(), token = version;
      if (retryText !== body) { retryId = command(); retryText = body; }
      send.disabled = true; ui.sending = true;
      try {
        await mutate(`/systems/${companyId}/messages`, { command_id: retryId, body });
        if (disposed || token !== version) return;
        if (composer.value.trim() === body) { composer.value = ""; drafts.delete(companyId); }
        retryId = null; retryText = null;
        composeStatus.textContent = "Saved to the lead inbox. Processing is unavailable until agent execution is connected.";
        await refresh(token);
      } catch (problem) { if (token === version) composeStatus.textContent = problem.message; }
      finally { if (token === version) { ui.sending = false; send.disabled = current?.system.state === "archived"; } }
    });
    const conversation = el("section", { class: "swarm-conversation", "aria-label": "Lead inbox" }, [el("h2", { text: "Lead inbox" }), messages, form]);
    const tabs = el("div", { class: "swarm-panel-tabs", "aria-label": "Work views" });
    const detail = el("div", { class: "swarm-work-content" });
    for (const key of ["tasks", "events", "budgets", "checkpoints"]) {
      tabs.append(button({ tasks: "Tasks", events: "Activity", budgets: "Usage", checkpoints: "Handoffs" }[key], () => { panel = key; renderPanel(); }, { "data-panel": key }));
    }
    const work = el("section", { class: "swarm-work", "aria-label": "Work panel" }, [tabs, detail]);
    const mobile = el("div", { class: "swarm-mobile-tabs", "aria-label": "System panels" });
    const layout = el("div", { class: "swarm-layout", "data-mobile-panel": "inbox" }, [team, conversation, work]);
    for (const key of ["team", "inbox", "work"]) mobile.append(button(key[0].toUpperCase() + key.slice(1), () => {
      layout.dataset.mobilePanel = key;
      for (const b of mobile.children) b.setAttribute("aria-pressed", String(b.dataset.mobile === key));
    }, { "data-mobile": key, "aria-pressed": String(key === "inbox") }));
    const connection = el("span", { class: "muted", role: "status", text: "Connecting live updates…" });
    // Chat keeps B's three panes. Board and Map are whole-width views of the
    // same persisted tasks (David's ask 2026-09-16); only the selected one is
    // rendered, so a hidden view costs nothing on every event refresh.
    board = createBoard({ onSelect: showTask });
    graph = createGraph({ onSelect: showTask });
    const views = el("div", { class: "swarm-view-tabs", "aria-label": "System views" });
    const applyWorkView = () => {
      for (const b of views.children) b.setAttribute("aria-pressed", String(b.dataset.view === workView));
      layout.hidden = workView !== "chat";
      mobile.hidden = workView !== "chat";
      board.node.hidden = workView !== "board";
      graph.node.hidden = workView !== "map";
      renderWorkViews();
    };
    for (const [key, text] of [["chat", "Chat"], ["board", "Board"], ["map", "Map"]]) {
      views.append(button(text, () => { workView = key; applyWorkView(); }, { "data-view": key }));
    }
    root.replaceChildren(el("header", { class: "swarm-header" }, [button("All systems", () => home()), heading, state, controls]), notice, views, mobile, layout,
      board.node, graph.node, el("footer", { class: "swarm-footer" }, [connection, button("Refresh", () => openSystem(selected))]));
    ui = { heading, state, controls, notice, team, messages, composer, send, tabs, detail, connection, applyWorkView, busy: false };
    applyWorkView();
  }

  function workData() {
    return {
      agents: current.agents,
      tasks: pages.tasks.items,
      dependencies: current.dependencies || [],
      attempts: pages.attempts?.items || [],
      loaded: pages.tasks.items.length,
      total: pages.tasks.total,
      onLoadMore: () => loadMore("tasks"),
    };
  }

  function renderWorkViews() {
    if (!ui || !current) return;
    if (workView === "board") board?.render(workData());
    if (workView === "map") graph?.render(workData());
  }

  function showTask(task) {
    showDialog(task.objective, (host) => {
      const owner = current.agents.find(a => a.id === task.agent_id);
      const known = new Map(pages.tasks.items.map(t => [t.id, t]));
      const links = (current.dependencies || []).filter(d => d.task_id === task.id);
      const tries = (pages.attempts?.items || []).filter(a => a.task_id === task.id);
      host.append(el("p", { class: "muted", text: `${task.state} · ${owner ? owner.name : "Unassigned"} · Created ${time(task.created_at)}` }));
      host.append(el("h3", { text: "Depends on" }));
      if (!links.length) host.append(el("p", { class: "muted", text: "Nothing. This task can start as soon as the company is running." }));
      for (const link of links) {
        const parent = known.get(link.depends_on);
        host.append(el("p", { text: parent ? `${parent.objective} — ${parent.state}` : "A task outside the loaded page" }));
      }
      host.append(el("h3", { text: "Attempts" }));
      if (!tries.length) host.append(el("p", { class: "muted", text: "No agent has claimed this task." }));
      for (const attempt of tries) host.append(el("article", { class: "swarm-record" }, [
        el("strong", { text: attempt.state }),
        el("p", { class: "muted", text: `${attempt.used.toLocaleString()} of ${attempt.max_units.toLocaleString()} tokens · ${attempt.stopped ? "Stopped" : "Not confirmed stopped"}` }),
        attempt.error ? el("p", { text: attempt.error }) : null,
      ]));
      host.append(el("details", {}, [el("summary", { text: "Checkpoint and result" }), el("pre", { text: pretty({ checkpoint: task.checkpoint, result: task.result }) })]));
    });
  }

  function update(snapshot) {
    const { system, agents, availability } = snapshot;
    ui.heading.textContent = system.name;
    ui.state.textContent = system.state;
    let reasons = system.reason;
    try { reasons = JSON.parse(reasons || "[]").join(", "); } catch (_) {}
    ui.notice.replaceChildren();
    if (reasons) ui.notice.append(el("p", { class: "swarm-pause-reason", text: `Pause reason: ${reasons}` }));
    // Why a company stopped, in its own words, instead of leaving the owner to
    // infer it from a screen that simply went quiet.
    const ENDINGS = {
      mission_complete: "Mission complete",
      cycle_limit: "Stopped at the cycle ceiling",
      stalled: "Stopped without a conclusion",
    };
    const conclusion = snapshot.conclusion;
    if (conclusion) {
      ui.notice.append(el("p", { class: "swarm-conclusion" }, [
        el("strong", { text: ENDINGS[conclusion.reason] || "Stopped" }),
        conclusion.summary ? el("span", { text: ` — ${conclusion.summary}` }) : null,
      ]));
    }
    const mode = (system.configuration || {}).mode;
    if (mode === "autonomous" && snapshot.cycles) {
      ui.notice.append(el("p", { class: "muted", text: `Autonomous · ${snapshot.cycles} cycle${snapshot.cycles === 1 ? "" : "s"} so far` }));
    }
    const blockers = availability.blockers || [];
    if (blockers.length) {
      ui.notice.append(el("p", { text: "This company cannot start yet:" }),
        el("ul", { class: "swarm-blockers" }, blockers.map(problem => el("li", { text: problem }))));
    } else {
      ui.notice.append(el("p", { text: availability.detail }));
    }
    ui.controls.replaceChildren();
    if (system.state === "archived") ui.controls.append(button("Restore", () => lifecycle("restore")));
    else {
      ui.controls.append(button("Edit setup", () => setup(current)),
        button(system.state === "paused" ? "Resume" : "Start", () => lifecycle(system.state === "paused" ? "resume" : "start"), { disabled: !availability.execution_available, title: availability.detail }),
        button("Pause", () => lifecycle("pause"), { disabled: system.state === "paused" || system.state === "pausing" || system.state === "stopped" }),
        button("Stop", () => lifecycle("stop"), { disabled: system.state === "stopped" }),
        button("Archive", () => lifecycle("archive")));
    }
    ui.composer.disabled = system.state === "archived";
    ui.send.disabled = system.state === "archived" || !!ui.sending;
    ui.team.replaceChildren(el("h2", { text: "Team" }), el("p", { class: "muted", text: system.mission }));
    for (const agent of agents.filter(a => a.enabled)) {
      const state = agentActivity(agent.id);
      const card = el("article", { class: "swarm-agent" }, [
        el("strong", { text: agent.name }),
        el("span", { class: "swarm-agent-state", "data-tone": state.tone, text: state.label }),
        el("span", { text: agent.is_lead ? "CEO / PM" : agent.role }),
      ]);
      if (state.task) card.append(el("small", { class: "swarm-agent-task", text: state.task }));
      card.append(el("small", { class: "muted", text: connectionName(agent.endpoint_id) || "No model connection" }));
      if (agent.instructions) card.append(el("small", { class: "muted", text: agent.instructions }));
      ui.team.append(card);
    }
    renderMessages(); renderPanel(); renderWorkViews();
  }

  // Every state here is read back out of persisted tasks and attempts. No
  // agent is ever shown as busy because a model said so.
  function agentActivity(agentId) {
    const attempts = pages.attempts?.items || [];
    const tasks = (pages.tasks?.items || []).filter(task => task.agent_id === agentId);
    const running = attempts.find(item => item.agent_id === agentId && ["reserved", "started"].includes(item.state));
    const unknown = (current.unknown_attempts || []).find(item => item.agent_id === agentId);
    const pick = (state) => tasks.find(task => task.state === state)?.objective;
    if (running) return { label: "Working", tone: "working", task: pick("running") };
    if (unknown) return { label: "Needs reconciliation", tone: "unknown", task: pick("blocked") };
    if (tasks.some(task => task.state === "review")) return { label: "In review", tone: "review", task: pick("review") };
    if (tasks.some(task => task.state === "blocked")) return { label: "Blocked", tone: "blocked", task: pick("blocked") };
    if (tasks.some(task => task.state === "ready")) return { label: "Queued", tone: "queued", task: pick("ready") };
    return { label: "Idle", tone: "idle", task: null };
  }

  function reconcileDialog(attempt) {
    showDialog("Reconcile this worker", (host, close) => {
      const evidence = el("textarea", { rows: "3", maxlength: "4000", required: true,
        placeholder: "e.g. Checked Task Manager - no claude process is running for this company." });
      const status = el("p", { role: "alert", class: "swarm-error" });
      const confirm = el("button", { type: "submit", class: "btn danger", text: "Confirm stopped and retry" });
      const form = el("form", {}, [
        el("p", { text: "This worker was interrupted and never confirmed that it stopped, so its task is held." }),
        el("p", { class: "muted", text: "Check that nothing is still running for it, then describe what you checked. That text is recorded as the evidence for this decision. Any tool action left open is kept as an unknown outcome, not given a result." }),
        label("What did you check?", evidence), status,
        el("div", { class: "swarm-actions" }, [button("Cancel", close), confirm]),
      ]);
      form.addEventListener("submit", async event => {
        event.preventDefault();
        if (!evidence.value.trim() || confirm.disabled) return;
        const token = version;
        confirm.disabled = true; status.textContent = "";
        try {
          await mutate(`/systems/${selected}/attempts/${attempt.id}/reconcile`,
                       { command_id: command(), evidence: evidence.value.trim() });
          close(); if (!disposed && token === version) await refresh(token);
        } catch (problem) { if (token === version) status.textContent = problem.message; }
        finally { confirm.disabled = false; }
      });
      host.append(form);
    });
  }

  async function loadMore(key) {
    const token = version, id = selected, old = pages[key];
    try {
      const result = await request(`/systems/${id}/${key === "events" ? "activity" : key}?offset=${old.items.length}`);
      if (disposed || token !== version || pages[key] !== old) return;
      const known = new Set(old.items.map(i => i.id));
      pages[key] = { ...result, offset: 0, items: [...old.items, ...result.items.filter(i => !known.has(i.id))] };
      if (key === "messages") renderMessages();
      else { renderPanel(); renderWorkViews(); }
    } catch (problem) { if (!disposed && token === version) ui.notice.textContent = problem.message; }
  }

  function moreButton(key, host) {
    if (pages[key]?.items.length < pages[key]?.total) host.append(button("Load more", () => loadMore(key)));
  }

  // Activity in sentences. Every line names something that was persisted;
  // an event this does not recognise is left out rather than guessed at.
  function describeEvent(event) {
    const agents = new Map((current.agents || []).map(agent => [agent.id, agent.name]));
    const tasks = new Map((pages.tasks?.items || []).map(task => [task.id, task]));
    const attempts = new Map((pages.attempts?.items || []).map(item => [item.id, item]));
    const data = event.data || {};
    const clip = (value, length = 240) => {
      const text = typeof value === "string" ? value : JSON.stringify(value ?? "");
      return text.length > length ? text.slice(0, length) + "…" : text;
    };
    const actorOf = (id) => {
      const task = tasks.get(id);
      if (task) return agents.get(task.agent_id);
      const attempt = attempts.get(id);
      return attempt ? agents.get(attempt.agent_id) : null;
    };
    const who = actorOf(event.entity_id) || "A teammate";
    switch (event.kind) {
      case "system.created": return "Company created.";
      case "run.started": return `Run started: ${clip(data.objective)}`;
      case "run.completed": return "Cycle completed.";
      case "mission.concluded":
        return data.reason === "mission_complete"
          ? `The lead ended the mission: ${clip(data.summary) || "no summary given"}`
          : `The company stopped: ${clip(data.summary) || data.reason}`;
      case "message.queued": return "You sent an idea to the lead.";
      case "message.message": case "message.finding": case "message.proposal": {
        const from = agents.get(data.sender_id) || "A teammate";
        const to = agents.get(data.recipient_id) || "a teammate";
        return `${from} messaged ${to}.`;
      }
      case "task.created": return `${agents.get(data.agent_id) || "A teammate"} was assigned: ${clip(data.objective)}`;
      case "attempt.started": return `${who} started working.`;
      case "visible_text": return `${who}: ${clip(data.text, 400)}`;
      case "action_started": return `${who} used ${data.intent?.tool || "a tool"}.`;
      case "action_finished":
        return data.result?.ok === false ? `${who}'s tool call was rejected: ${clip(data.result.text)}` : null;
      case "usage.recorded": return `${Number(data.units || 0).toLocaleString()} tokens recorded.`;
      case "task.review": return `${who} submitted work for review.`;
      case "task.accepted": return `Accepted: ${clip(data.evidence)}`;
      case "task.revision_requested": return `Revision requested: ${clip(data.note)}`;
      case "attempt.interrupted":
        return data.unknown ? `${who} was interrupted and did not confirm it stopped.` : `${who} was interrupted and stopped.`;
      case "attempt.recovered": return `${who}'s work was recovered after a restart.`;
      case "system.pausing": return `Pausing: ${clip(data.reason)}`;
      case "system.paused": return "Company paused.";
      case "system.resumed": return "Company resumed.";
      case "usage.settled": return "Usage settled for a finished worker.";
      default: return null;
    }
  }

  function renderUnknownWorkers(host) {
    // From the snapshot's own list, not the attempts page: a stuck worker
    // must stay reachable however many attempts came after it.
    const unknown = current.unknown_attempts || [];
    if (!unknown.length) return;
    const names = new Map((current.agents || []).map(agent => [agent.id, agent.name]));
    const block = el("section", { class: "swarm-unknown", "aria-label": "Workers needing reconciliation" }, [
      el("h3", { text: `${unknown.length} worker${unknown.length === 1 ? "" : "s"} stopped without confirming` }),
      el("p", { class: "muted", text: "Their tasks are held until you confirm nothing is still running. This is expected after pausing mid-turn." }),
    ]);
    for (const attempt of unknown) {
      block.append(el("article", { class: "swarm-record" }, [
        el("strong", { text: names.get(attempt.agent_id) || "A teammate" }),
        el("p", { class: "muted", text: attempt.error || "No reason recorded" }),
        button("Reconcile", () => reconcileDialog(attempt), { "data-focus-key": `reconcile-${attempt.id}` }),
      ]));
    }
    host.append(block);
  }

  function renderResults() {
    const accepted = (pages.tasks?.items || []).filter(task => task.state === "done" && task.result);
    if (!accepted.length) return null;
    const section = el("section", { class: "swarm-results", "aria-label": "Accepted work" },
      [el("h3", { text: "Accepted work" })]);
    for (const task of accepted) {
      let result = {};
      try { result = JSON.parse(task.result) || {}; } catch (_) {}
      if (!result.output && !result.summary) continue;   // a coordination step, not a deliverable
      section.append(el("article", { class: "swarm-result" }, [
        el("strong", { text: task.objective }),
        el("p", { text: result.output || result.summary }),
        result.evidence ? el("small", { class: "muted", text: `Evidence: ${result.evidence}` }) : null,
      ]));
    }
    return section.children.length > 1 ? section : null;
  }

  function renderMessages() {
    ui.messages.replaceChildren();
    const names = new Map(current.agents.map(a => [a.id, a.name]));
    if (!pages.messages.items.length) ui.messages.append(el("p", { class: "swarm-empty", text: "Your ideas and the lead's replies will appear here. Saved ideas stay queued until execution is available." }));
    moreButton("messages", ui.messages);
    for (const message of [...pages.messages.items].reverse()) ui.messages.append(el("article", { class: "swarm-message", "data-message": message.id }, [
      el("div", { class: "muted", text: `${message.sender_id ? names.get(message.sender_id) || "Agent" : "You"} · ${message.read_at ? "Read" : "Queued"} · ${time(message.created_at)}` }),
      el("p", { text: message.body })]));
    // What came back, where the owner is already looking. Only accepted work
    // appears: a result still in review is not an answer yet.
    const results = renderResults();
    if (results) ui.messages.append(results);
  }

  function renderPanel() {
    if (!ui) return;
    for (const b of ui.tabs.children) b.setAttribute("aria-pressed", String(b.dataset.panel === panel));
    const host = ui.detail; host.replaceChildren();
    if (panel === "budgets") {
      host.append(el("h2", { text: "Usage" }), el("p", { class: "muted", text: "Local token allocations and provider account quota are separate. Held usage includes unfinished or unconfirmed work." }));
      if (current.allocation_ownership_unresolved) host.append(el("p", { role: "alert", text: "A legacy allocation group's ownership needs reconciliation. Its account totals are hidden." }));
      for (const budget of current.budgets) {
        const name = budget.scope === "agent" ? current.agents.find(a => a.id === budget.target)?.name : { system: "Company", run: "Run", pool: "Shared allocation group" }[budget.scope];
        const row = el("article", { class: "swarm-budget" }, [el("strong", { text: name || budget.scope }),
          el("p", { text: `${budget.used.toLocaleString()} recorded + ${budget.held.toLocaleString()} held / ${budget.ceiling.toLocaleString()} tokens` }),
          el("small", { class: "muted", text: `Pause at ${budget.pause_percent}%; checkpoint reserve ${budget.checkpoint_reserve}` })]);
        host.append(row);
      }
      host.append(el("h3", { text: "Provider account quota" }));
      if (!current.quotas.length) host.append(el("p", { text: "Unavailable. No provider allowance has been observed." }));
      for (const quota of current.quotas) host.append(el("p", { text: `${quota.bucket}: ${quota.used_percent == null ? "Unknown" : quota.used_percent + "% used"} · ${quota.status} · Observed ${time(quota.observed_at)} · ${quota.valid_until * 1000 <= Date.now() ? "Expired" : "Valid until " + time(quota.valid_until)}` }));
      return;
    }
    if (panel === "checkpoints") {
      host.append(el("h2", { text: "Saved handoffs" }), button("Save handoff", async event => {
        const node = event.currentTarget, token = version; node.disabled = true;
        try { await mutate(`/systems/${selected}/checkpoints`, { command_id: command() }); await refresh(token); }
        catch (problem) { if (token === version) ui.notice.textContent = problem.message; }
        finally { node.disabled = false; }
      }, { disabled: current.system.state === "archived", title: current.system.state === "archived" ? "Restore the system to save a new handoff" : "Save current progress without a model call" }));
      for (const item of pages.checkpoints.items) {
        const row = el("article", { class: "swarm-record" }, [el("strong", { text: time(item.created_at) }), el("p", { class: "muted", text: `Event cursor ${item.cursor}` })]);
        for (const format of ["md", "json"]) row.append(el("a", { class: "btn", text: format === "md" ? "Markdown" : "JSON", download: "", href: `/api/swarm/systems/${selected}/checkpoints/${item.id}/download?format=${format}` }));
        host.append(row);
      }
    } else if (panel === "events") {
      host.append(el("h2", { text: "Activity" }));
      for (const item of pages.events.items) {
        const line = describeEvent(item);
        if (!line) continue;
        host.append(el("article", { class: "swarm-activity" }, [
          el("span", { class: "muted", text: time(item.created_at) }),
          el("p", { text: line }),
        ]));
      }
    } else {
      host.append(el("h2", { text: "Tasks" }));
      renderUnknownWorkers(host);
      for (const item of pages.tasks.items) {
        const row = el("article", { class: "swarm-record" });
        row.append(el("strong", { text: item.objective }), el("p", { class: "muted", text: item.state }));
        row.append(el("details", {}, [el("summary", { text: "Details" }),
          el("pre", { text: pretty({ checkpoint: item.checkpoint, result: item.result }) })]));
        host.append(row);
      }
    }
    if (!pages[panel].items.length) host.append(el("p", { class: "swarm-empty", text: panel === "tasks" ? "No tasks have been created." : panel === "checkpoints" ? "No saved handoffs yet." : "No activity recorded." }));
    moreButton(panel, host);
  }

  home();
  return cleanup;
}
