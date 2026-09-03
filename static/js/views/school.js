import { api, el } from "../api.js";

// School: Canvas assignments grouped by course. Three states in one view
// (no routing) — overview (upcoming-across-everything + course cards, a mix
// of Calendar's "Next 7 Days" panel and Cookbook's card grid), a course's
// assignment list, and an assignment workspace (text editor + a real chat
// session scoped to that course, memory built server-side — see
// services/school_service.py). Draft edits autosave (debounced) and push a
// context note into the course's chat session on save, so the assistant's
// memory of "what I'm working on" tracks the editor without spending a
// model turn on every keystroke (see sync-memory's docstring).

let state = { view: "overview", course: null, assignmentId: null };
let draftSaveTimer = null;

function formatDue(iso) {
  if (!iso) return "No due date";
  if (!iso.includes("T")) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
  }
  return new Date(iso).toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export async function render(container) {
  container.innerHTML = "";
  state = { view: "overview", course: null, assignmentId: null };

  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "School" }),
      el("div", { class: "sub", text: "Canvas assignments by course, with a text editor and course-memory chat per assignment" }),
    ]),
  ]);

  const body = el("div", { id: "school-body" });
  container.append(header, body);
  await renderBody(body);
}

async function renderBody(body) {
  body.innerHTML = "";
  if (state.view === "overview") await renderOverview(body);
  else if (state.view === "course") await renderCourseView(body);
  else await renderAssignmentWorkspace(body);
}

// -- settings ---------------------------------------------------------------
async function renderSettings(container, onSynced) {
  const settings = await api("/api/tab-school/settings");
  const card = el("div", { class: "glass bracket card", style: "padding:8px 10px;" });
  const inputStyle = "flex:1;min-width:180px;font-size:11.5px;padding:5px 8px;";
  const baseUrlInput = el("input", { placeholder: "Canvas base URL (e.g. https://school.instructure.com)", value: settings.canvas_base_url, style: inputStyle });
  const tokenInput = el("input", { type: "password", placeholder: settings.canvas_api_token_configured ? "API token saved (leave blank to keep)" : "Canvas API access token (optional)", style: inputStyle });
  const icsInput = el("input", { placeholder: "or: Canvas iCal feed URL (Calendar > Calendar Feed)", value: settings.ics_url, style: inputStyle });
  const saveBtn = el("button", { class: "btn", text: "Save", style: "font-size:11.5px;padding:5px 9px;" });
  const syncBtn = el("button", { class: "btn", text: "Sync Now", style: "font-size:11.5px;padding:5px 9px;" });
  const statusEl = el("div", { class: "meta", style: "margin-top:6px;font-size:10px;" });

  saveBtn.addEventListener("click", async () => {
    await api("/api/tab-school/settings", {
      method: "PUT",
      body: JSON.stringify({ canvas_base_url: baseUrlInput.value, canvas_api_token: tokenInput.value || null, ics_url: icsInput.value }),
    });
    tokenInput.value = "";
    statusEl.textContent = "Saved.";
  });
  syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    statusEl.textContent = "Syncing...";
    try {
      const res = await api("/api/tab-school/sync", { method: "POST" });
      statusEl.textContent = `Synced ${res.count} assignments from ${res.source === "canvas_api" ? "the Canvas API" : "the iCal feed"}.`;
      await onSynced();
    } catch (e) {
      statusEl.textContent = "Sync failed: " + e.message;
    }
    syncBtn.disabled = false;
  });

  card.append(
    el("div", { class: "title", style: "margin-bottom:4px;font-size:12px;", text: "Canvas Connection" }),
    el("div", { class: "meta", style: "margin-bottom:6px;font-size:10px;", text: "Use the API (real descriptions + links) if you have a token, or just the iCal feed URL otherwise." }),
    el("div", { class: "card-row", style: "flex-wrap:wrap;gap:6px;" }, [baseUrlInput, tokenInput]),
    el("div", { class: "card-row", style: "flex-wrap:wrap;gap:6px;margin-top:6px;" }, [icsInput, saveBtn, syncBtn]),
    statusEl,
  );
  if (settings.last_synced_at) {
    const when = new Date(settings.last_synced_at * 1000).toLocaleString();
    const sourceLabel = settings.last_sync_source === "canvas_api" ? "Canvas API" : "iCal feed";
    card.appendChild(el("div", { class: "meta", style: "margin-top:4px;font-size:10px;color:var(--text-faint);", text: `Last synced ${when} via ${sourceLabel}` }));
  }
  container.appendChild(card);
}

// -- overview: overdue + upcoming + course cards -----------------------------
async function renderOverview(body) {
  await renderSettings(body, () => renderBody(body));

  const [overdue, upcoming, courses] = await Promise.all([
    api("/api/tab-school/assignments?overdue=true"),
    api("/api/tab-school/assignments?upcoming_days=14"),
    api("/api/tab-school/courses"),
  ]);

  body.appendChild(el("div", { class: "title", style: "margin:18px 0 10px;font-size:15px;", text: "Courses" }));
  const grid = el("div", { style: "display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-bottom:22px;" });
  body.appendChild(grid);
  if (courses.length === 0) {
    grid.appendChild(el("div", { class: "empty-state", text: "No courses yet — connect Canvas above and hit Sync Now." }));
  }
  for (const c of courses) {
    const bits = [`${c.upcoming_count} upcoming`];
    if (c.overdue_count > 0) bits.push(`${c.overdue_count} overdue`);
    bits.push(`${c.assignment_count} total`);
    grid.appendChild(el("div", { class: "glass bracket card", style: "cursor:pointer;padding:20px 18px;", onclick: () => openCourse(body, c.name) }, [
      el("div", { class: "title", style: "font-size:16px;", text: c.name }),
      el("div", { class: "meta", style: "margin-top:8px;font-size:12.5px;" + (c.overdue_count > 0 ? "color:var(--danger);" : ""), text: bits.join(" · ") }),
    ]));
  }

  if (overdue.length > 0) {
    body.appendChild(el("div", { class: "title", style: "margin:18px 0 8px;color:var(--danger);", text: `Overdue (${overdue.length})` }));
    const overdueList = el("div", {});
    body.appendChild(overdueList);
    for (const a of overdue) overdueList.appendChild(assignmentCard(a, () => openAssignment(body, a.id), true));
  }

  body.appendChild(el("div", { class: "title", style: "margin:18px 0 8px;", text: "Upcoming (next 14 days)" }));
  const upcomingList = el("div", {});
  body.appendChild(upcomingList);
  if (upcoming.length === 0) {
    upcomingList.appendChild(el("div", { class: "empty-state", text: "Nothing due in the next two weeks — or nothing synced yet." }));
  } else {
    for (const a of upcoming) upcomingList.appendChild(assignmentCard(a, () => openAssignment(body, a.id)));
  }
}

function assignmentCard(a, onclick, showOverdue) {
  const links = a.attachment_links || [];
  return el("div", { class: "glass bracket card", style: "padding:7px 10px;" }, [
    el("div", { class: "card-row", style: "cursor:pointer;", onclick }, [
      el("div", {}, [
        el("div", { class: "title", style: "font-size:12px;" + (a.completed ? "text-decoration:line-through;opacity:0.6;" : ""), text: a.title }),
        el("div", { class: "meta", style: "margin-top:2px;font-size:10px;" + (showOverdue ? "color:var(--danger);" : ""), text: `${a.course} · ${formatDue(a.due)}` }),
      ]),
    ]),
    links.length > 0 ? el("div", { style: "margin-top:4px;display:flex;flex-wrap:wrap;gap:6px;" },
      links.map((link) => el("a", { href: link, target: "_blank", rel: "noopener", text: "Attachment ↗", style: "color:var(--accent);font-size:10px;", onclick: (e) => e.stopPropagation() }))) : null,
  ]);
}

// -- course view: back + assignment list -------------------------------------
async function openCourse(body, course) {
  state = { view: "course", course, assignmentId: null };
  await renderBody(body);
}

async function renderCourseView(body) {
  const backBtn = el("button", { class: "btn", text: "‹ All Courses", onclick: () => { state = { view: "overview", course: null, assignmentId: null }; renderBody(body); } });
  const showCompletedCheckbox = el("input", { type: "checkbox" });
  const showCompletedLabel = el("label", { style: "display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-faint);cursor:pointer;" }, [showCompletedCheckbox, "Show completed"]);

  body.append(
    el("div", { class: "card-row", style: "margin-bottom:12px;justify-content:space-between;" }, [backBtn, showCompletedLabel]),
    el("div", { class: "title", style: "margin-bottom:10px;", text: state.course }),
  );

  const list = el("div", {});
  body.appendChild(list);

  async function refreshList() {
    list.innerHTML = "";
    const assignments = await api(`/api/tab-school/assignments?course=${encodeURIComponent(state.course)}&include_completed=${showCompletedCheckbox.checked}`);
    if (assignments.length === 0) {
      list.appendChild(el("div", { class: "empty-state", text: "No assignments found for this course yet." }));
      return;
    }
    for (const a of assignments) list.appendChild(assignmentCard(a, () => openAssignment(body, a.id)));
  }
  showCompletedCheckbox.addEventListener("change", refreshList);
  await refreshList();
}

// -- assignment workspace: editor + course chat -------------------------------
async function openAssignment(body, assignmentId) {
  state = { view: "assignment", course: state.course, assignmentId };
  await renderBody(body);
}

async function renderAssignmentWorkspace(body) {
  const a = await api(`/api/tab-school/assignments/${state.assignmentId}`);
  state.course = a.course;

  const backBtn = el("button", { class: "btn", text: `‹ ${a.course}`, onclick: () => { state = { view: "course", course: a.course, assignmentId: null }; renderBody(body); } });
  const completeCheckbox = el("input", { type: "checkbox" });
  completeCheckbox.checked = !!a.completed;
  completeCheckbox.addEventListener("change", async () => {
    await api(`/api/tab-school/assignments/${a.id}`, { method: "PATCH", body: JSON.stringify({ completed: completeCheckbox.checked }) });
  });
  const completeLabel = el("label", { style: "display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-faint);cursor:pointer;" }, [completeCheckbox, "Mark complete"]);

  body.append(el("div", { class: "card-row", style: "margin-bottom:12px;justify-content:space-between;" }, [backBtn, completeLabel]));

  const links = a.attachment_links || [];
  const infoCard = el("div", { class: "glass bracket card" }, [
    el("div", { class: "title", text: a.title }),
    el("div", { class: "meta", style: "margin-top:4px;", text: `Due ${formatDue(a.due)}` }),
    a.url ? el("div", { style: "margin-top:6px;" }, [el("a", { href: a.url, target: "_blank", rel: "noopener", text: "Open assignment ↗", style: "color:var(--accent);font-size:12.5px;" })]) : null,
    links.length > 0 ? el("div", { style: "margin-top:6px;display:flex;flex-wrap:wrap;gap:10px;" },
      links.map((link, i) => el("a", { href: link, target: "_blank", rel: "noopener", text: `Attachment ${i + 1} ↗`, style: "color:var(--accent);font-size:12.5px;" }))) : null,
    a.description ? el("div", { class: "meta", style: "margin-top:8px;white-space:pre-wrap;", text: a.description }) : null,
  ]);
  body.appendChild(infoCard);

  const workspace = el("div", { style: "display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px;align-items:stretch;" });
  body.appendChild(workspace);

  // -- editor pane --
  const saveStatus = el("span", { class: "meta" });
  const editorCard = el("div", { class: "glass card", style: "display:flex;flex-direction:column;min-height:420px;" });
  const editor = el("textarea", {
    placeholder: "Start your work on this assignment...",
    style: "flex:1;min-height:380px;width:100%;resize:vertical;background:transparent;color:var(--text);border:1px solid var(--border);border-radius:8px;padding:10px;font-family:inherit;font-size:13px;line-height:1.5;",
  });
  const draft = await api(`/api/tab-school/assignments/${a.id}/draft`);
  editor.value = draft.content;
  editor.addEventListener("input", () => {
    saveStatus.textContent = "Unsaved...";
    clearTimeout(draftSaveTimer);
    draftSaveTimer = setTimeout(async () => {
      await api(`/api/tab-school/assignments/${a.id}/draft`, { method: "PUT", body: JSON.stringify({ content: editor.value }) });
      await api(`/api/tab-school/assignments/${a.id}/sync-memory`, { method: "POST" });
      saveStatus.textContent = "Saved";
    }, 1200);
  });
  editorCard.append(
    el("div", { class: "card-row", style: "margin-bottom:8px;" }, [el("div", { class: "title", text: "Your work" }), saveStatus]),
    editor,
  );
  workspace.appendChild(editorCard);

  // -- chat pane --
  const chatCard = el("div", { class: "glass card", style: "display:flex;flex-direction:column;min-height:420px;" });
  workspace.appendChild(chatCard);
  await api(`/api/tab-school/assignments/${a.id}/sync-memory`, { method: "POST" });
  await renderCoursePanel(chatCard, a.course, a.id);
}

// A compact chat panel bound to the course's persistent session — same
// message-card/composer shape as static/js/views/chat.js, scoped down (no
// attachments/workspace/integrations, this is meant to live in half a
// column) since this session's whole point is accumulating course memory,
// not being a general-purpose chat.
function messageCard(role, text, ts) {
  const time = new Date((ts || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  // Header row (status dot + role + timestamp) dropped to match the main
  // chat's Claude-style redesign (2026-09-03) — the two views share these
  // classes, so they'd otherwise diverge visually. Timestamp moved to the
  // hover row, same as chat.js.
  return el("div", { class: `msg ${role}` }, [
    el("div", { class: "msg-body", text }),
    el("div", { class: "msg-actions" }, [el("span", { class: "msg-time", text: time })]),
  ]);
}

async function renderCoursePanel(chatCard, course, assignmentId) {
  const { session_id: sessionId } = await api(`/api/tab-school/courses/session?course=${encodeURIComponent(course)}`);
  const session = await api(`/api/sessions/${sessionId}`);

  const modelBtn = el("button", { type: "button", class: "model-picker-btn" }, [el("span", { id: "school-model-label" })]);
  const modelMenu = el("div", { class: "model-picker-menu hidden" });
  const modelWrap = el("div", { class: "model-picker-wrap", style: "position:relative;" }, [modelBtn, modelMenu]);
  modelBtn.addEventListener("click", (e) => { e.stopPropagation(); modelMenu.classList.toggle("hidden"); });
  document.addEventListener("click", () => modelMenu.classList.add("hidden"));
  await refreshCourseModelPicker(sessionId, modelBtn.querySelector("span"), modelMenu);

  // Manual push (David's ask, follow-up) — the debounced autosave already
  // syncs the draft into memory 1.2s after typing stops, but this lets you
  // force it immediately (e.g. right before asking a question about work
  // you just finished) without waiting or making a throwaway edit.
  const syncBtn = el("button", { type: "button", class: "btn", style: "font-size:11px;padding:5px 9px;", text: "Sync memory" });
  syncBtn.addEventListener("click", async () => {
    syncBtn.disabled = true;
    const original = syncBtn.textContent;
    const res = await api(`/api/tab-school/assignments/${assignmentId}/sync-memory`, { method: "POST" });
    syncBtn.textContent = res.updated ? "Synced" : "Already current";
    setTimeout(() => { syncBtn.textContent = original; syncBtn.disabled = false; }, 1500);
  });

  const messages = el("div", { style: "flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:10px;margin:8px 0;" });
  for (const msg of session.messages) messages.appendChild(messageCard(msg.role, msg.content, msg.ts));
  messages.scrollTop = messages.scrollHeight;

  const input = el("textarea", { rows: "2", placeholder: "Ask about this course...", style: "flex:1;background:transparent;color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px;font-family:inherit;font-size:13px;resize:none;" });
  const sendBtn = el("button", { class: "btn", text: "Send" });
  const composer = el("div", { class: "card-row", style: "align-items:flex-end;gap:8px;" }, [input, sendBtn]);

  async function send() {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    sendBtn.disabled = true;
    messages.appendChild(messageCard("user", text));
    const replyCard = messageCard("assistant", "");
    const replyBody = replyCard.querySelector(".msg-body");
    messages.appendChild(replyCard);
    messages.scrollTop = messages.scrollHeight;

    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, message: text, attachment_ids: [] }),
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = JSON.parse(line.slice(6));
        if (payload.chunk) { replyBody.textContent += payload.chunk; messages.scrollTop = messages.scrollHeight; }
      }
    }
    sendBtn.disabled = false;
    input.focus();
  }
  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });

  chatCard.append(
    el("div", { class: "card-row", style: "margin-bottom:4px;" }, [
      el("div", { class: "title", text: `${course} — Course Chat` }),
      el("div", { class: "card-row", style: "gap:8px;" }, [syncBtn, modelWrap]),
    ]),
    messages,
    composer,
  );
}

async function refreshCourseModelPicker(sessionId, label, menu) {
  const endpoints = await api("/api/models");
  const session = await api(`/api/sessions/${sessionId}`);
  menu.innerHTML = "";
  if (endpoints.length === 0) {
    menu.appendChild(el("div", { class: "model-picker-item", text: "No models added yet — see Settings" }));
  }
  for (const ep of endpoints) {
    const name = ep.kind === "claude_cli" ? `${ep.name} (${ep.model || "CLI default"})` : `${ep.name} (${ep.model})`;
    menu.appendChild(el("div", {
      class: "model-picker-item" + (ep.id === session.model_endpoint_id ? " active" : ""),
      text: name,
      onclick: async (e) => {
        e.stopPropagation();
        await api(`/api/sessions/${sessionId}/model`, { method: "POST", body: JSON.stringify({ model_endpoint_id: ep.id }) });
        label.textContent = name;
        menu.classList.add("hidden");
      },
    }));
  }
  const active = endpoints.find((e) => e.id === session.model_endpoint_id);
  label.textContent = active ? (active.kind === "claude_cli" ? `${active.name} (${active.model || "CLI default"})` : `${active.name} (${active.model})`) : "No model — add one in Settings";
}
