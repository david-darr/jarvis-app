import { api, el, customSelect, toast, emptyState, confirmDialog } from "/static/js/api.js";
import { mountSessionChat } from "/static/js/sessionChat.js";
import { ICONS } from "/static/js/icons.js";

const BASE = "/api/tab-crm";
const labels = { active: "Active", in_progress: "In progress", waiting: "Waiting", needs_review: "Needs review", done: "Done", dismissed: "Dismissed" };
const priorities = ["urgent", "high", "normal", "low"];
const cap = (text) => text ? text[0].toUpperCase() + text.slice(1) : "";
const closed = (task) => ["done", "dismissed"].includes(task.status);
const todayKey = () => {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
};
function dueLabel(value) {
  if (!value) return "No deadline";
  return value.length === 10 ? new Date(value + "T12:00:00").toLocaleDateString()
    : new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}
function dueDay(value) {
  if (!value) return "";
  if (value.length === 10) return value;
  const date = new Date(value);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
const overdue = (task) => task.due_date && (task.due_date.length === 10
  ? task.due_date < todayKey() : new Date(task.due_date).getTime() < Date.now());
function select(options, value, name) {
  const node = customSelect({ "aria-label": name }, options.map(([id, text]) => el("option", { value: id, text })));
  node.value = value || "";
  return node;
}
function field(label, control) {
  // Custom select supplies its own accessible name; other controls get an id.
  if (!control.id) control.id = "crm-field-" + Math.random().toString(36).slice(2);
  return el("div", { class: "field" }, [el("label", { text: label, for: control.id }), control]);
}
function button(text, action, kind = "") {
  const node = el("button", { class: "btn " + kind, text, type: "button" });
  node.addEventListener("click", async () => {
    if (node.disabled) return;
    node.disabled = true;
    try { await action(); }
    catch (error) { toast(error.message, "error"); }
    finally { node.disabled = node.getAttribute("aria-busy") === "true"; }
  });
  return node;
}

export async function render(container) {
  container.replaceChildren();

  let data, connections = { connections: [], models: [] }, agents = [];
  let mode = "tasks", filter = "open", query = "", contactFilter = "", projectFilter = "", sourceFilter = "", priorityFilter = "";
  let selected = null, disposed = false, pollTimer = null, editing = false, closeSource = null, closeChat = () => {};
  const openWork = new Set();
  const wrap = el("div", { class: "view-constrained crm-view" });
  const heading = el("div", { class: "view-header" }, [el("div", {}, [
    el("h2", { text: "CRM" }), el("div", { class: "sub", text: "Follow through on the work your conversations create." }),
  ])]);
  const headerActions = el("div", { class: "crm-actions" });
  const scanButton = button("Scan now", async () => {
    await api(BASE + "/scan", { method: "POST", body: JSON.stringify({}) });
    toast("Scan started. Results will appear as each source finishes.");
    await load();
  });
  headerActions.append(button("Add task", () => { mode = "tasks"; selected = {}; draw(); }, "primary"), scanButton);
  heading.append(headerActions);
  const tabs = el("nav", { class: "crm-tabs", "aria-label": "CRM sections" });
  const content = el("div");
  content.addEventListener("input", (event) => { if (event.target.closest(".crm-detail, .crm-settings, .disclosure-panel")) editing = true; });
  wrap.append(heading, tabs, content);
  container.append(wrap);

  async function load(poll = false) {
    clearTimeout(pollTimer);
    data = await api(BASE);
    if (data.can_connect) {
      const results = await Promise.allSettled([api(BASE + "/connections"), api("/api/agents")]);
      if (results[0].status === "fulfilled") connections = results[0].value;
      if (results[1].status === "fulfilled") agents = results[1].value;
    }
    if (disposed) return;
    if (selected?.id) selected = data.tasks.find((t) => t.id === selected.id) || null;
    if (!poll) editing = false;
    if (!poll || !editing) draw();
    else {
      scanButton.textContent = data.scanning ? "Scanning…" : "Scan now";
      scanButton.disabled = data.scanning;
      scanButton.setAttribute("aria-busy", String(data.scanning));
    }
    if (data.scanning) pollTimer = setTimeout(() => load(true).catch((error) => toast(error.message, "error")), 3000);
  }

  function draw() {
    tabs.replaceChildren();
    for (const [id, text] of [["tasks", "Tasks"], ["contacts", "Contacts"], ["sources", "Sources"]]) {
      tabs.append(button(text, () => { mode = id; draw(); }, mode === id ? "primary" : "quiet"));
    }
    scanButton.hidden = !data.can_connect;
    scanButton.textContent = data.scanning ? "Scanning…" : "Scan now";
    scanButton.setAttribute("aria-busy", String(data.scanning));
    scanButton.disabled = data.scanning || !data.settings.endpoint_id || !data.sources.some((s) => s.enabled);
    content.replaceChildren();
    closeChat(); closeChat = () => {};
    if (mode === "sources") return drawSources();
    if (mode === "contacts") return drawContacts();
    drawTasks();
  }

  function drawTasks() {
    const open = data.tasks.filter((t) => !closed(t));
    const summaries = [
      ["Today", open.filter((t) => dueDay(t.due_date) === todayKey()).length, "today"],
      ["Overdue", open.filter(overdue).length, "overdue"],
      ["Waiting", open.filter((t) => t.status === "waiting").length, "waiting"],
      ["Needs review", open.filter((t) => t.status === "needs_review" || t.proposal).length, "review"],
    ];
    content.append(el("div", { class: "crm-summary" }, summaries.map(([name, count, id]) =>
      el("button", { class: "glass crm-stat", type: "button", onclick: () => { filter = id; selected = null; draw(); } }, [
        el("span", { class: "meta", text: name }), el("strong", { text: String(count) }),
      ]))));
    const search = el("input", { type: "search", placeholder: "Search tasks, people or projects", value: query, "aria-label": "Search CRM tasks" });
    const status = select([["open", "Open work"], ["today", "Today"], ["overdue", "Overdue"], ["upcoming", "Upcoming"],
      ["waiting", "Waiting"], ["review", "Needs review"], ["snoozed", "Snoozed"], ["done", "Done"], ["dismissed", "Dismissed"], ["all", "All tasks"]], filter, "Task view");
    const priority = select([["", "All priorities"], ...priorities.map((p) => [p, cap(p)])], priorityFilter, "Priority filter");
    const project = select([["", "All projects"], ...[...new Set(data.tasks.map((t) => t.project).filter(Boolean))].sort().map((p) => [p, p])], projectFilter, "Project filter");
    const source = select([["", "All sources"], ...data.sources.map((s) => [s.id, s.label])], sourceFilter, "Source filter");
    const controls = el("div", { class: "crm-filters" }, [search, status, priority, project, source]);
    content.append(controls);
    const layout = el("div", { class: "crm-layout" });
    const list = el("div", { class: "crm-task-list", "aria-label": "CRM task list" });
    const detail = el("section", { class: "glass card crm-detail", "aria-label": "Task details" });
    layout.append(list);
    if (selected) layout.append(detail);
    else layout.classList.add("crm-layout-single");
    content.append(layout);
    const snoozed = (task) => task.snoozed_until && new Date(task.snoozed_until.length === 10 ? task.snoozed_until + "T23:59:59" : task.snoozed_until) > new Date();
    function fillList() {
      list.replaceChildren();
      const items = data.tasks.filter((task) => {
        if (contactFilter && task.contact !== contactFilter) return false;
        if (projectFilter && task.project !== projectFilter) return false;
        if (sourceFilter && task.source_id !== sourceFilter) return false;
        if (priorityFilter && task.priority !== priorityFilter) return false;
        if (query && ![task.title, task.contact, task.project, task.notes].join(" ").toLowerCase().includes(query.toLowerCase())) return false;
        if (filter === "all") return true;
        if (["done", "dismissed"].includes(filter)) return task.status === filter;
        if (closed(task)) return false;
        if (filter === "snoozed") return snoozed(task);
        if (snoozed(task)) return false;
        if (filter === "today") return dueDay(task.due_date) === todayKey();
        if (filter === "overdue") return overdue(task);
        if (filter === "waiting") return task.status === "waiting";
        if (filter === "review") return task.status === "needs_review" || !!task.proposal;
        if (filter === "upcoming") return task.due_date && dueDay(task.due_date) > todayKey();
        return true;
      }).sort((a, b) => Number(!!overdue(b)) - Number(!!overdue(a)) || priorities.indexOf(a.priority) - priorities.indexOf(b.priority)
        || (a.due_date || "9999").localeCompare(b.due_date || "9999"));
      if (contactFilter) list.append(button(`Clear contact: ${contactFilter}`, () => { contactFilter = ""; draw(); }, "quiet"));
      if (!items.length) {
        list.append(emptyState({ icon: ICONS.notes, title: data.tasks.length ? "No matching follow-ups" : "Your follow-ups start here",
          hint: data.tasks.length ? "Try another filter or add a task." : "Select a connection in Sources and scan it, or add a task yourself.",
          actionLabel: "Choose sources", onAction: () => { mode = "sources"; draw(); } }));
      }
      for (const task of items) {
        const node = el("button", { class: "glass crm-task" + (selected?.id === task.id ? " selected" : ""), type: "button",
          onclick: () => { selected = task; draw(); } }, [
          el("div", { class: "crm-task-top" }, [el("strong", { text: task.title }), el("span", { class: "crm-priority " + task.priority, text: cap(task.priority) })]),
          el("div", { class: "meta crm-task-meta", text: [task.contact, task.project].filter(Boolean).join(" · ") || "Personal follow-up" }),
          el("div", { class: "crm-task-bottom" }, [el("span", { class: overdue(task) && !closed(task) ? "crm-overdue" : "meta", text: dueLabel(task.due_date) }),
            el("span", { class: "meta", text: task.proposal ? "Change to review" : labels[task.status] })]),
        ]);
        list.append(node);
      }
    }
    search.addEventListener("input", () => { query = search.value; fillList(); });
    for (const [node, setter] of [[status, (v) => filter = v], [priority, (v) => priorityFilter = v], [project, (v) => projectFilter = v], [source, (v) => sourceFilter = v]]) {
      node.addEventListener("change", () => { setter(node.value); fillList(); });
    }
    fillList();
    if (selected) drawDetail(detail, selected);
  }

  function drawDetail(host, task) {
    host.append(el("div", { class: "crm-detail-heading" }, [el("h3", { text: task.id ? "Follow-up" : "New task" }),
      button("Close", () => { selected = null; draw(); }, "quiet")]));
    const title = el("input", { value: task.title || "", maxlength: "300", required: true });
    const notes = el("textarea", { rows: "3", text: task.notes || "" });
    const contact = el("input", { value: task.contact || "" });
    const project = el("input", { value: task.project || "" });
    const priority = select(priorities.map((p) => [p, cap(p)]), task.priority || "normal", "Task priority");
    const status = select(Object.entries(labels), task.status || "active", "Task status");
    const dateOnly = el("input", { type: "checkbox", checked: !task.due_date || task.due_date.length === 10 });
    const due = el("input", { type: dateOnly.checked ? "date" : "datetime-local" });
    function inputDue() {
      if (!task.due_date) return "";
      if (dateOnly.checked) return dueDay(task.due_date);
      const d = new Date(task.due_date.length === 10 ? task.due_date + "T12:00:00" : task.due_date);
      return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    }
    due.value = inputDue();
    dateOnly.addEventListener("change", () => { const day = due.value.slice(0, 10); due.type = dateOnly.checked ? "date" : "datetime-local"; due.value = day ? day + (dateOnly.checked ? "" : "T12:00") : ""; });
    host.append(field("Task", title), field("Contact", contact), field("Project", project),
      el("div", { class: "crm-pair" }, [field("Priority", priority), field("Status", status)]),
      field("Deadline", due), el("label", { class: "crm-check" }, [dateOnly, "Date only"]), field("Notes", notes));
    const save = button("Save task", async () => {
      if (!title.value.trim()) { title.focus(); throw new Error("Give the task a title"); }
      const fields = { title: title.value.trim(), notes: notes.value, contact: contact.value, project: project.value,
        priority: priority.value, status: status.value, due_date: due.value ? (dateOnly.checked ? due.value : new Date(due.value).toISOString()) : null };
      const saved = await api(BASE + "/tasks" + (task.id ? "/" + task.id : ""), { method: task.id ? "PATCH" : "POST", body: JSON.stringify(fields) });
      selected = saved;
      await load();
      toast("Task saved", "success");
    }, "primary");
    const actions = el("div", { class: "crm-actions" }, [save]);
    if (task.id) {
      actions.append(button(closed(task) ? "Reopen" : "Mark done", () => mutateTask(task, { status: closed(task) ? "active" : "done" })));
      actions.append(button(task.status === "dismissed" ? "Restore" : "Dismiss", () => mutateTask(task, { status: task.status === "dismissed" ? "active" : "dismissed" }), "quiet"));
    }
    host.append(actions);
    if (!task.id) return;
    const snooze = el("input", { type: "date", value: task.snoozed_until?.slice(0, 10) || "" });
    host.append(el("div", { class: "crm-pair" }, [field("Snooze until", snooze),
      button("Set snooze", () => mutateTask(task, { snoozed_until: snooze.value || null }))]));
    if (task.review_reason) host.append(el("p", { class: "crm-review", text: task.review_reason }));
    if (task.deadline_text) host.append(el("p", { class: "meta", text: `Deadline evidence: “${task.deadline_text}” · ${task.deadline_kind}` }));
    if (task.priority_reason) host.append(el("p", { class: "meta", text: `Priority: ${task.priority_reason}` }));
    if (task.proposal) {
      const proposal = task.proposal;
      host.append(el("div", { class: "crm-proposal" }, [el("strong", { text: "Suggested change" }),
        el("p", { text: `${proposal.title} · ${dueLabel(proposal.due_date)} · ${labels[proposal.status]}` }),
        el("div", { class: "crm-actions" }, [button("Accept change", () => reviewTask(task, true)), button("Keep current", () => reviewTask(task, false), "quiet")])]));
    }
    host.append(el("h4", { text: "Source evidence" }));
    if (!task.evidence.length) host.append(el("p", { class: "meta", text: "Added manually. No source message is attached." }));
    for (const evidence of task.evidence) {
      host.append(el("div", { class: "crm-evidence" }, [el("div", { class: "meta", text: evidence.label + (evidence.sent_at ? " · " + dueLabel(evidence.sent_at) : "") }),
        el("blockquote", { text: evidence.quote }), button("Open source", () => showSource(evidence), "quiet")]));
    }
    host.append(workPanel(task, "draft", "Draft a reply", drawReply), workPanel(task, "chat", "Chat about this task", drawChat));
    if (data.can_connect && agents.length && !closed(task)) {
      const agent = select(agents.map((a) => [a.id, a.name]), agents[0].id, "Agent to assign");
      host.append(el("details", { class: "disclosure-panel" }, [el("summary", { text: "Assign to an agent" }),
        el("p", { class: "meta", text: "Creates a backlog card on the Work Board. Move it to Ready when you want the agent to begin." }),
        agent, button(task.agent_card_id ? "View assignment" : "Create backlog card", async () => {
          const response = await api(BASE + `/tasks/${task.id}/agent`, { method: "POST", body: JSON.stringify({ agent_id: agent.value }) });
          toast(`Backlog card ${response.card_id} is on the Work Board`, "success");
          await load();
        })]));
    }
  }

  // Draft and chat sections load only when opened, and stay open across the
  // redraws a save or a scan poll causes.
  function workPanel(task, kind, title, drawBody) {
    const body = el("div", { class: "crm-work-body" });
    const panel = el("details", { class: "disclosure-panel crm-work", "data-work": kind }, [el("summary", { text: title }), body]);
    const key = task.id + ":" + kind;
    const show = () => drawBody(body, task).catch((error) => {
      if (!body.isConnected) return;
      body.replaceChildren(el("p", { class: "crm-review", text: error.message }),
        button("Try again", () => show(), "quiet"));
    });
    panel.addEventListener("toggle", () => {
      if (panel.open) { openWork.add(key); show(); }
      else { openWork.delete(key); if (kind === "chat") { closeChat(); body.replaceChildren(); } }
    });
    if (openWork.has(key)) { panel.open = true; show(); }
    return panel;
  }

  async function drawReply(body, task) {
    body.replaceChildren(el("p", { class: "meta", text: "Loading…" }));
    const state = await api(BASE + `/tasks/${task.id}/reply`);
    if (!body.isConnected) return;
    const draft = el("textarea", { rows: "8", "aria-label": "Reply draft", placeholder: "Draft a reply with AI, or write one yourself." });
    draft.value = state.draft;
    draft.addEventListener("change", () => api(BASE + `/tasks/${task.id}/reply`, { method: "PUT", body: JSON.stringify({ draft: draft.value }) })
      .catch((error) => toast(error.message, "error")));
    const where = state.target
      ? `Replies to ${state.target.to} from ${state.target.account} · ${state.target.subject}`
      : "This task has no email to reply to. Copy the draft and send it where the conversation is.";
    const actions = el("div", { class: "crm-actions" }, [
      button(state.draft ? "Redraft with AI" : "Draft with AI", async () => {
        if (draft.value.trim() && !await confirmDialog({ title: "Replace this draft?", message: "The AI draft replaces what's in the box.", confirmLabel: "Replace", danger: false })) return;
        body.querySelector(".crm-work-status").textContent = "Drafting…";
        try { await api(BASE + `/tasks/${task.id}/draft`, { method: "POST" }); }
        finally { if (body.isConnected) body.querySelector(".crm-work-status").textContent = ""; }
        if (body.isConnected) await drawReply(body, task);
      }, "primary"),
      button("Copy", async () => { await navigator.clipboard.writeText(draft.value); toast("Draft copied", "success"); }, "quiet"),
    ]);
    if (state.can_send) {
      actions.append(button("Send", async () => {
        if (!draft.value.trim()) throw new Error("Write or draft a reply first");
        if (!await confirmDialog({ title: "Send this reply?", message: `To ${state.target.to}, from ${state.target.account}, subject “${state.target.subject}”.`, confirmLabel: "Send email", danger: false })) return;
        await api(BASE + `/tasks/${task.id}/send`, { method: "POST", body: JSON.stringify({ draft: draft.value }) });
        toast(`Reply sent to ${state.target.to}`, "success");
        if (body.isConnected) await drawReply(body, task);
      }));
    }
    body.replaceChildren(el("p", { class: "meta", text: where }), draft, actions,
      el("p", { class: "meta crm-work-status", role: "status" }),
      state.last_reply ? el("p", { class: "meta", text: `Last reply sent to ${state.last_reply.to} · ${dueLabel(new Date(state.last_reply.at * 1000).toISOString())}` }) : "",
      el("p", { class: "meta", text: "Drafts use the model chosen in Sources and see this task's notes and source messages." }));
  }

  async function drawChat(body, task) {
    closeChat();
    body.replaceChildren(el("p", { class: "meta", text: "Opening the chat…" }));
    const { session_id: sessionId } = await api(BASE + `/tasks/${task.id}/chat`, { method: "POST" });
    if (!body.isConnected) return;
    const host = el("div", { class: "crm-chat" });
    body.replaceChildren(el("p", { class: "meta", text: "A Kairos chat that starts with this task's context. Its source messages are untrusted, so shell commands ask you first." }), host);
    closeChat = await mountSessionChat(host, { sessionId, title: "CRM: " + task.title, placeholder: "Ask Kairos about this task",
      emptyTitle: "Work on this task", emptyText: "Ask for a plan, a reply, research or a summary.", modelPicker: true, openInChats: true });
  }

  async function mutateTask(task, fields) {
    await api(BASE + "/tasks/" + task.id, { method: "PATCH", body: JSON.stringify(fields) });
    await load();
  }
  async function reviewTask(task, accept) {
    await api(BASE + `/tasks/${task.id}/review`, { method: "POST", body: JSON.stringify({ accept }) });
    await load();
  }
  async function showSource(evidence) {
    const message = await api(BASE + "/messages/" + evidence.message_id);
    if (disposed) return;
    const close = button("Close", () => finish(), "quiet");
    const panel = el("section", { class: "glass modal-panel crm-source-modal", role: "dialog", "aria-modal": "true", "aria-label": "Source message" }, [
      el("div", { class: "crm-detail-heading" }, [el("h3", { text: message.subject || "Source message" }), close]),
      el("div", { class: "meta", text: [message.sender, message.account, message.folder, message.sent_at && dueLabel(message.sent_at)].filter(Boolean).join(" · ") }),
      el("p", { class: "meta", text: "Captured source text. Links and embedded content were not loaded." }),
    ]);
    if (message.url && /^https:\/\//i.test(message.url)) panel.append(el("a", { href: message.url, target: "_blank", rel: "noopener noreferrer", class: "btn", text: "Open original message" }));
    if (message.context) panel.append(el("details", {}, [el("summary", { text: "Earlier thread context" }), el("pre", { text: message.context })]));
    panel.append(el("pre", { class: "crm-source-text", text: message.body }));
    if (message.truncated || message.context_incomplete) panel.append(el("p", { class: "crm-review", text: "The captured text is incomplete. Check the original before relying on it." }));
    const backdrop = el("div", { class: "modal-backdrop" }, [panel]);
    const previous = document.activeElement;
    closeSource?.();
    const finish = () => { backdrop.remove(); document.removeEventListener("keydown", keys, true); closeSource = null; previous?.focus(); };
    closeSource = finish;
    const keys = (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopImmediatePropagation(); finish(); }
      if (event.key === "Tab") {
        const nodes = [...panel.querySelectorAll('button, a[href], summary')];
        const first = nodes[0], last = nodes[nodes.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    backdrop.addEventListener("click", (event) => { if (event.target === backdrop) finish(); });
    document.addEventListener("keydown", keys, true);
    document.body.append(backdrop);
    close.focus();
  }

  function drawContacts() {
    const contacts = new Map();
    for (const task of data.tasks) {
      if (!task.contact) continue;
      if (!contacts.has(task.contact)) contacts.set(task.contact, []);
      contacts.get(task.contact).push(task);
    }
    if (!contacts.size) return content.append(emptyState({ icon: ICONS.notes, title: "No contact history yet", hint: "Contacts appear as you add or scan follow-ups." }));
    const grid = el("div", { class: "document-grid" });
    for (const [contact, tasks] of [...contacts].sort(([a], [b]) => a.localeCompare(b))) {
      grid.append(el("div", { class: "glass card" }, [el("h3", { text: contact }),
        el("p", { class: "meta", text: `${tasks.filter((t) => !closed(t)).length} open · ${tasks.filter((t) => t.status === "waiting").length} waiting · ${tasks.filter((t) => t.status === "done").length} completed` }),
        ...tasks.slice(-3).reverse().map((t) => el("p", { class: "meta", text: `${t.title} · ${dueLabel(t.due_date)}` })),
        button("View follow-ups", () => { mode = "tasks"; filter = "all"; contactFilter = contact; selected = null; draw(); })]));
    }
    content.append(grid);
  }

  function drawSources() {
    content.append(el("p", { class: "meta", text: "Scan only the connections you select. Messaging sources capture new messages admitted by Kairos; they do not import private account history." }));
    if (data.can_connect) {
      const model = select([["", "Choose an extraction model"], ...connections.models.map((m) => [m.id, m.name])], data.settings.endpoint_id, "Extraction model");
      let specificModel = select([["", "Connection default"]], "", "Model");
      const modelField = el("div", {}, [field("Model", specificModel)]);
      const modelStatus = el("p", { class: "meta", role: "status" });
      let modelRequest = 0;
      async function loadModels(selected = null) {
        const request = ++modelRequest;
        const endpointId = model.value;
        specificModel = select([["", "Connection default"]], "", "Model");
        specificModel.disabled = !!endpointId;
        modelField.replaceChildren(field("Model", specificModel), modelStatus);
        modelStatus.textContent = endpointId ? "Loading models…" : "Choose a connection to see its models.";
        if (!endpointId) return;
        try {
          const choices = await api(BASE + "/models/" + encodeURIComponent(endpointId));
          if (disposed || !modelField.isConnected || request !== modelRequest) return;
          specificModel = select([["", "Connection default"], ...choices.map((m) => [m.id, m.name])],
            choices.some((m) => m.id === selected) ? selected : "", "Model");
          specificModel.addEventListener("change", () => { editing = true; });
          modelField.replaceChildren(field("Model", specificModel), modelStatus);
          const kind = connections.models.find((m) => m.id === endpointId)?.kind;
          modelStatus.textContent = ["local", "api"].includes(kind) ? "Local/API model selection is configured in Settings." : "";
        } catch {
          if (disposed || !modelField.isConnected || request !== modelRequest) return;
          specificModel.disabled = false;
          modelStatus.textContent = "The model list couldn't load. Using Connection default; reselect the connection to retry.";
        }
      }
      model.addEventListener("change", () => { editing = true; void loadModels(); });
      const timezone = el("input", { value: data.settings.endpoint_id ? data.settings.timezone : Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC" });
      const lookback = el("input", { type: "number", min: "1", max: "90", value: String(data.settings.lookback_days) });
      const cadence = el("input", { type: "number", min: "5", max: "1440", value: String(data.settings.interval_minutes) });
      const review = el("input", { type: "checkbox", checked: data.settings.review_all });
      const auto = el("input", { type: "checkbox", checked: data.settings.auto_scan });
      let scheduleMode = data.settings.schedule_mode || "interval";
      let scheduleTimes = [...(data.settings.schedule_times || ["09:00"])];
      const scheduleDays = new Set(data.settings.schedule_days || [0, 1, 2, 3, 4, 5, 6]);
      const dayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
      const schedule = el("div");
      const scheduleSummary = el("p", { class: "meta", "aria-live": "polite" });
      const settingsError = el("p", { class: "crm-review", role: "alert" });
      function summary() {
        const clocks = [...scheduleTimes].sort().map((t) => {
          if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(t)) return "an unset time";
          const [h, m] = t.split(":").map(Number);
          return `${h % 12 || 12}:${String(m).padStart(2, "0")} ${h < 12 ? "AM" : "PM"}`;
        });
        const days = [...scheduleDays].sort();
        const when = days.length === 7 ? "every day" : days.join() === "0,1,2,3,4" ? "on weekdays" : "on " + days.map((d) => dayNames[d]).join(", ");
        scheduleSummary.textContent = scheduleMode === "interval" ? `Scans every ${cadence.value} minutes while Kairos is running` :
          `Scans at ${clocks.join(clocks.length === 2 ? " and " : ", ")} ${when} (${timezone.value})`;
      }
      function drawSchedule() {
        schedule.replaceChildren();
        const modes = el("div", { class: "segmented-tabs", role: "group", "aria-label": "Scan schedule" });
        for (const [value, label] of [["interval", "Every N minutes"], ["times", "At set times"]]) {
          modes.append(button(label, () => { editing = true; scheduleMode = value; drawSchedule(); },
            "segmented-tab" + (scheduleMode === value ? " active" : "")));
        }
        schedule.append(modes);
        if (scheduleMode === "interval") schedule.append(field("Check every (minutes)", cadence));
        else {
          const times = el("div", { class: "crm-schedule-times" });
          scheduleTimes.forEach((value, index) => {
            const input = el("input", { type: "time", value, required: true, "aria-label": `Scan time ${index + 1}` });
            input.addEventListener("input", () => { scheduleTimes[index] = input.value; summary(); });
            const remove = button("Remove", () => { editing = true; scheduleTimes.splice(index, 1); drawSchedule(); }, "quiet");
            remove.disabled = scheduleTimes.length === 1;
            times.append(el("div", { class: "crm-actions" }, [input, remove]));
          });
          const add = button("Add time", () => { editing = true; scheduleTimes.push(""); drawSchedule(); }, "quiet");
          add.disabled = scheduleTimes.length >= 6;
          schedule.append(times, add);
          const days = el("div", { class: "tab-build-chips", role: "group", "aria-label": "Scan days" });
          dayNames.forEach((label, index) => {
            const chip = button(label, () => {
              editing = true;
              if (scheduleDays.has(index)) scheduleDays.delete(index); else scheduleDays.add(index);
              drawSchedule();
            }, "tab-build-chip" + (scheduleDays.has(index) ? " active" : ""));
            chip.setAttribute("aria-pressed", String(scheduleDays.has(index)));
            days.append(chip);
          });
          days.append(button("Every day", () => { editing = true; dayNames.forEach((_, i) => scheduleDays.add(i)); drawSchedule(); }, "quiet"));
          schedule.append(el("p", { class: "meta", text: "Days and times use the timezone above." }), days);
        }
        summary();
      }
      cadence.addEventListener("input", summary);
      timezone.addEventListener("input", summary);
      drawSchedule();
      content.append(el("div", { class: "glass card crm-settings" }, [el("h3", { text: "Scanning" }),
        el("div", { class: "crm-pair" }, [field("Extraction model", model), modelField, field("Timezone", timezone), field("Email lookback (days)", lookback)]),
        el("label", { class: "crm-check" }, [auto, "Auto scan"]), schedule, scheduleSummary,
        el("label", { class: "crm-check" }, [review, "Review every extracted task before adding it to active work"]),
        el("p", { class: "meta", text: "Scanning uses your selected model. Claude CLI and local/API models run without action tools. Codex CLI is not offered for unattended extraction." }),
        settingsError, button("Save scanning settings", async () => {
          settingsError.textContent = "";
          if (scheduleMode === "times" && (!scheduleDays.size || scheduleTimes.length < 1 || scheduleTimes.length > 6 ||
              scheduleTimes.some((t) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(t)) || new Set(scheduleTimes).size !== scheduleTimes.length)) {
            settingsError.textContent = "Choose at least one day and 1 to 6 unique scan times.";
            return;
          }
          try {
            await api(BASE + "/settings", { method: "PUT", body: JSON.stringify({ endpoint_id: model.value || null, model: specificModel.value || null,
              timezone: timezone.value, lookback_days: Number(lookback.value), interval_minutes: Number(cadence.value), auto_scan: auto.checked, review_all: review.checked,
              schedule_mode: scheduleMode, schedule_times: [...scheduleTimes].sort(), schedule_days: [...scheduleDays].sort() }) });
            await load(); toast("Scanning settings saved", "success");
          } catch (error) { settingsError.textContent = error.message; }
        }, "primary")]));
      void loadModels(data.settings.model);
      const connection = select([["", "Select a connected account or document"], ...connections.connections.map((c) => [c.kind + ":" + c.id, c.label])], "", "CRM connection");
      const folder = el("input", { value: "INBOX" });
      const scope = el("input", { placeholder: "Optional channel/chat IDs, separated by commas" });
      content.append(el("details", { class: "disclosure-panel", open: !data.sources.length }, [el("summary", { text: "Add a source" }),
        field("Connection", connection), field("Email folder", folder), field("Messaging conversations", scope),
        button("Add selected source", async () => {
          if (!connection.value) throw new Error("Select a connected account or document first");
          const [kind, connection_id] = connection.value.split(":");
          await api(BASE + "/sources", { method: "POST", body: JSON.stringify({ kind, connection_id, folder: kind === "email" ? folder.value : "INBOX",
            conversations: scope.value.split(",").map((v) => v.trim()).filter(Boolean) }) });
          await load(); toast("Source added", "success");
        }), el("p", { class: "meta", text: "Connect accounts through Email or Settings > Channels, or add a document to Library, then select it here." })]));
    } else content.append(el("p", { class: "meta", text: "An administrator manages shared connection access. You can still create and manage your own follow-ups." }));
    for (const source of data.sources) {
      content.append(el("div", { class: "glass card crm-source" }, [el("div", {}, [el("strong", { text: source.label }),
        el("div", { class: "meta", text: source.last_scan_at ? "Last checked " + new Date(source.last_scan_at * 1000).toLocaleString() : "No scan yet" }),
        source.error && el("p", { class: "crm-review", text: source.error })]),
        button(source.enabled ? "Pause source" : "Enable source", async () => {
          await api(BASE + "/sources/" + source.id, { method: "PATCH", body: JSON.stringify({ enabled: !source.enabled }) });
          await load();
        }, "quiet")]));
    }
    if (data.failed_messages && data.can_connect) content.append(button(`Retry ${data.failed_messages} failed message${data.failed_messages === 1 ? "" : "s"}`, async () => {
      await api(BASE + "/scan", { method: "POST", body: JSON.stringify({ retry: true }) }); await load();
    }));
    if (data.runs.length) content.append(el("details", { class: "disclosure-panel" }, [el("summary", { text: "Scan history" }),
      ...data.runs.slice(0, 15).map((r) => el("div", { class: "crm-run" }, [el("strong", { text: r.source }),
        el("div", { class: "meta", text: `${new Date(r.at * 1000).toLocaleString()} · ${r.scanned} extracted · ${r.created} created · ${r.remaining} remaining` }),
        r.error && el("p", { class: "crm-review", text: r.error })]))]));
  }

  try { await load(); }
  catch (error) { content.replaceChildren(el("p", { class: "crm-review", text: "Could not load CRM: " + error.message }), button("Retry", load)); }
  return () => { disposed = true; clearTimeout(pollTimer); closeSource?.(); closeChat(); };
}
