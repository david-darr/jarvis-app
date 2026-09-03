import { api, el, toast, confirmDialog } from "../api.js";
import { runSlashCommand } from "../slashCommands.js";

// Composer rebuilt to match Odysseus's actual chat-input-bar structure
// (David's ask 2026-08-31, cross-checked against the real repo at
// ~/odysseus/static/index.html + static/js/chat.js + workspace.js — not
// guessed): two-row bar (textarea + inline model picker on top; a left icon
// strip led by a "+" overflow menu, right side the send button, on the
// bottom), attach-strip above the bar for staged files, real folder-scoped
// "Workspace" via core/workspace.py (ported from Odysseus's
// src/tool_execution.py's vet_workspace/browse), and "Prompt" backed by our
// existing Skills service rather than Odysseus's separate preset system.
// "Documents" (David's ask 2026-09-01, Phase 7) now inserts a real library
// document's content into the composer, same mechanism as "Prompt" below
// (which does the same thing for Skills) — not Odysseus's own RAG-backed
// approach (which sends a document reference the model retrieves at query
// time), since JARVIS doesn't have a RAG layer. Simpler and honest about
// the difference: the whole document goes into the message up front.

const ICON_ATTACH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';
const ICON_DOC = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>';
const ICON_WORKSPACE = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const ICON_PROMPT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m18 2 4 4"/><path d="m17 7 3-3"/><path d="M19 9 8.7 19.3c-1 1-2.5 1-3.4 0l-.6-.6c-1-1-1-2.5 0-3.4L15 5"/><path d="m9 11 4 4"/></svg>';
const ICON_PLUS = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
const ICON_SEND = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>';
const ICON_X = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';
const ICON_CHEVRON = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
const ICON_COPY = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const ICON_PLUG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6"/><path d="M15 2v6"/><path d="M12 17v5"/><path d="M6 8h12a2 2 0 0 1 2 2v2a6 6 0 0 1-6 6h-4a6 6 0 0 1-6-6v-2a2 2 0 0 1 2-2z"/></svg>';
const ICON_CHATS = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H8l-4 4V5z"/></svg>';
// "Done" marker (David's ask 2026-09-02: a clear indicator for when a reply
// has fully finished, distinct from mid-turn pauses that can look frozen).
const ICON_DONE = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';

// Message, Claude-style (David's ask 2026-09-03) — replaced the previous
// Odysseus-style bordered card that carried a status-dot + role + timestamp
// header on every single message. Claude shows no per-message header at
// all: the user's turn is a rounded bubble on the right, the assistant's is
// plain unboxed text, and chrome (copy, timestamp) only appears on hover.
// Dropping the header is what makes a long conversation read as one
// continuous document instead of a stack of forms.
//
// ts is a unix-seconds float (session_manager.append_message) or omitted
// for a card being built live during streaming (uses "now").
function messageCard(role, text, ts) {
  const time = new Date((ts || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const body = el("div", { class: "msg-body", text });
  const copyBtn = el("button", { type: "button", class: "msg-action-btn", title: "Copy" });
  copyBtn.insertAdjacentHTML("beforeend", ICON_COPY);
  copyBtn.addEventListener("click", async () => {
    await navigator.clipboard.writeText(body.textContent);
    toast("Copied to clipboard", "success");
  });
  // Timestamp moved off the (now removed) header into the hover row, so the
  // information is still there without putting a label on every message.
  const actions = el("div", { class: "msg-actions" }, [copyBtn, el("span", { class: "msg-time", text: time })]);

  return el("div", { class: `msg ${role}` }, [body, actions]);
}

// Claude's idle-thinking indicator (David's ask 2026-09-03): a pulsing dot
// plus a highlight shimmering across the word. Both are pure CSS
// animations, replacing the old JS setInterval that rotated "." -> ".." ->
// "..." — no timer to clear, and it keeps animating through the silent
// gaps in a long tool-using turn (brain.py yields once per completed
// TextBlock, so a multi-step turn genuinely goes quiet between them).
function thinkingIndicator() {
  return el("div", { class: "msg-thinking-row" }, [
    el("span", { class: "msg-thinking-dot" }),
    el("span", { class: "msg-thinking-text", text: "Thinking" }),
  ]);
}

let activeSessionId = null;
let stagedAttachments = []; // [{id, filename}]

// Chat tab's default landing state (David's ask 2026-09-01) — a large
// JARVIS wordmark + prompt, not an auto-opened conversation. Nothing here
// is persisted; sendMessage()'s existing lazy createSession() call already
// only creates a real session on the first actual message.
function renderWelcome(messages) {
  messages.innerHTML = "";
  messages.appendChild(
    el("div", { class: "chat-welcome" }, [
      el("img", { src: "/static/img/jarvis-logo.png", alt: "" }),
      el("h1", { text: "JARVIS" }),
      el("p", { text: "Begin a chat with JARVIS" }),
    ]),
  );
}

export async function render(container) {
  container.innerHTML = "";
  container.classList.add("chat-layout");
  stagedAttachments = [];
  // Every fresh landing on the Chat tab starts at the welcome state, even
  // if a session was open the last time this tab was visited — matches a
  // typical chat app's "new chat by default" convention rather than
  // silently resuming wherever you left off.
  activeSessionId = null;

  const sessionsPanel = el("div", { id: "chat-sessions" });
  const newBtn = el("button", { class: "btn", style: "width:100%;margin-bottom:12px;", text: "+ New Chat", onclick: createSession });
  const sessionsList = el("div", { id: "sessions-list" });
  sessionsPanel.append(newBtn, sessionsList);

  // Mobile-only (David's ask 2026-09-01) — sessions become a slide-out
  // drawer below the responsive breakpoint (style.css's @media block),
  // matching Claude/ChatGPT mobile's "tap to see chat history" pattern
  // instead of a fixed always-visible column. The button/backdrop are
  // display:none above the breakpoint, so this is inert on desktop.
  const sessionsBackdrop = el("div", { class: "chat-sessions-backdrop hidden" });
  const openSessionsBtn = el("button", { type: "button", class: "input-icon-btn chat-mobile-menu-btn", title: "Chats" });
  openSessionsBtn.insertAdjacentHTML("beforeend", ICON_CHATS);
  const mobileHeader = el("div", { class: "chat-mobile-header" }, [openSessionsBtn, el("span", { text: "JARVIS" })]);
  function openSessionsDrawer() { sessionsPanel.classList.add("open"); sessionsBackdrop.classList.remove("hidden"); }
  function closeSessionsDrawer() { sessionsPanel.classList.remove("open"); sessionsBackdrop.classList.add("hidden"); }
  openSessionsBtn.addEventListener("click", openSessionsDrawer);
  sessionsBackdrop.addEventListener("click", closeSessionsDrawer);

  const main = el("div", { id: "chat-main" }, [mobileHeader]);
  const messages = el("div", { id: "chat-messages" });
  const attachStrip = el("div", { id: "attach-strip", class: "attach-strip" });

  // -- composer: top row (textarea + model picker) --------------------
  const input = el("textarea", { id: "chat-input", rows: "1", placeholder: "Message JARVIS..." });
  const modelBtn = el("button", { type: "button", class: "model-picker-btn", id: "model-picker-btn" }, [
    el("span", { id: "model-picker-label", text: "No model — add one in Settings" }),
  ]);
  modelBtn.insertAdjacentHTML("beforeend", ICON_CHEVRON);
  const modelMenu = el("div", { class: "model-picker-menu hidden", id: "model-picker-menu" });
  const modelWrap = el("div", { class: "model-picker-wrap" }, [modelBtn, modelMenu]);
  modelBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(modelMenu); });

  const inputTop = el("div", { class: "chat-input-top" }, [input, modelWrap]);

  // -- composer: bottom row (overflow "+" menu, workspace pill, send) --
  const overflowBtn = el("button", { type: "button", class: "input-icon-btn", id: "overflow-plus-btn", title: "More" });
  overflowBtn.insertAdjacentHTML("beforeend", ICON_PLUS);
  const overflowMenu = el("div", { class: "overflow-menu hidden", id: "overflow-menu" });
  overflowBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(overflowMenu); });

  const fileInput = el("input", { type: "file", multiple: true, style: "display:none;" });
  fileInput.addEventListener("change", () => handleFilePicked(fileInput, attachStrip));

  const attachItem = menuItem(ICON_ATTACH, "Attach files", () => { closeMenu(overflowMenu); fileInput.click(); });
  const docItem = menuItem(ICON_DOC, "Documents", () => { closeMenu(overflowMenu); openDocumentsMenu(docItem); });
  const workspaceItem = menuItem(ICON_WORKSPACE, "Workspace", () => { closeMenu(overflowMenu); openWorkspaceModal(); });
  const promptItem = menuItem(ICON_PROMPT, "Prompt", () => { closeMenu(overflowMenu); openPromptMenu(promptItem); });
  const integrationsItem = menuItem(ICON_PLUG, "Integrations", () => { closeMenu(overflowMenu); openIntegrationsModal(); });
  overflowMenu.append(attachItem, docItem, workspaceItem, integrationsItem, promptItem);
  const overflowWrap = el("div", { class: "overflow-wrapper" }, [overflowBtn, overflowMenu, fileInput]);

  const workspacePill = el("div", { id: "workspace-pill-slot" });

  const sendBtn = el("button", { class: "btn", id: "chat-send", title: "Send" });
  sendBtn.insertAdjacentHTML("beforeend", ICON_SEND);

  const inputLeft = el("div", { class: "chat-input-left" }, [overflowWrap, workspacePill]);
  const inputRight = el("div", {}, [sendBtn]);
  const inputBottom = el("div", { class: "chat-input-bottom" }, [inputLeft, inputRight]);

  const composer = el("div", { class: "glass chat-input-bar" }, [inputTop, inputBottom]);
  main.append(messages, attachStrip, composer);

  container.append(sessionsPanel, sessionsBackdrop, main);
  // Selecting a session or starting a new one closes the mobile drawer —
  // no-op above the breakpoint since the classes it touches are inert there.
  sessionsList.addEventListener("click", closeSessionsDrawer);
  newBtn.addEventListener("click", closeSessionsDrawer);

  sendBtn.addEventListener("click", () => sendMessage(messages, input, sendBtn, attachStrip));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage(messages, input, sendBtn, attachStrip);
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  });

  document.addEventListener("click", () => { closeMenu(overflowMenu); closeMenu(modelMenu); });

  await refreshSessions(sessionsList, messages);

  // New Tab builder handoff (Developer Mode, David's ask 2026-09-01) — the
  // one deliberate exception to "Chat always lands on the welcome screen"
  // above. new-tab.js creates a real session and stashes it here rather
  // than landing the user back at a blank welcome screen right after they
  // filled out the form. Consumed once (removeItem) so a later, ordinary
  // visit to this tab still resets to welcome as normal.
  const pendingRaw = sessionStorage.getItem("jarvis:pendingChatHandoff");
  if (pendingRaw) {
    sessionStorage.removeItem("jarvis:pendingChatHandoff");
    try {
      const pending = JSON.parse(pendingRaw);
      await openSession(pending.sessionId, sessionsList, messages);
      input.value = pending.message;
      input.dispatchEvent(new Event("input"));
      await sendMessage(messages, input, sendBtn, attachStrip);
    } catch (e) {
      console.error("chat: pending handoff failed", e);
    }
  }
}

function menuItem(iconSvg, label, onclick) {
  const btn = el("button", { type: "button", class: "overflow-menu-item", onclick });
  btn.insertAdjacentHTML("afterbegin", iconSvg);
  btn.appendChild(el("span", { text: label }));
  return btn;
}

function toggleMenu(menu) {
  const wasHidden = menu.classList.contains("hidden");
  document.querySelectorAll(".overflow-menu, .model-picker-menu").forEach((m) => m.classList.add("hidden"));
  if (wasHidden) menu.classList.remove("hidden");
}
function closeMenu(menu) { menu.classList.add("hidden"); }

// -- attach files ---------------------------------------------------------
async function handleFilePicked(fileInput, attachStrip) {
  const files = [...fileInput.files];
  fileInput.value = "";
  for (const file of files) {
    const form = new FormData();
    form.append("file", file);
    try {
      const staged = await api("/api/chat/attachments", { method: "POST", headers: {}, body: form });
      stagedAttachments.push(staged);
    } catch (e) {
      // Was a raw window.alert() — unstyleable OS chrome that blocked the
      // whole UI in an otherwise glass-skinned app (audit 2026-09-03).
      toast(`Couldn't attach ${file.name}: ${e.message}`, "error");
    }
  }
  renderAttachStrip(attachStrip);
}

function renderAttachStrip(attachStrip) {
  attachStrip.innerHTML = "";
  for (const a of stagedAttachments) {
    const chip = el("div", { class: "attach-chip" }, [el("span", { text: a.filename })]);
    const removeBtn = el("button", { type: "button", title: "Remove" });
    removeBtn.insertAdjacentHTML("beforeend", ICON_X);
    removeBtn.addEventListener("click", () => {
      stagedAttachments = stagedAttachments.filter((s) => s.id !== a.id);
      renderAttachStrip(attachStrip);
    });
    chip.appendChild(removeBtn);
    attachStrip.appendChild(chip);
  }
}

// -- prompts (backed by Skills, not a separate preset system) -------------
async function openPromptMenu(anchor) {
  const skills = await api("/api/skills");
  const menu = el("div", { class: "overflow-menu", style: "position:absolute; bottom:calc(100% + 8px); left:0; z-index:60;" });
  if (skills.length === 0) {
    menu.appendChild(el("div", { class: "overflow-menu-item", text: "No skills saved yet (see Brain tab)" }));
  } else {
    for (const s of skills) {
      menu.appendChild(menuItem(ICON_PROMPT, s.slug, async () => {
        const full = await api(`/api/skills/${s.slug}`);
        const input = document.getElementById("chat-input");
        input.value = (input.value ? input.value + "\n\n" : "") + full.body;
        input.dispatchEvent(new Event("input"));
        input.focus();
        menu.remove();
      }));
    }
  }
  anchor.parentElement.style.position = "relative";
  anchor.parentElement.appendChild(menu);
  setTimeout(() => document.addEventListener("click", function close() {
    menu.remove();
    document.removeEventListener("click", close);
  }), 0);
}

// -- documents (backed by the Library tab, Phase 7) ------------------------
async function openDocumentsMenu(anchor) {
  const docs = await api("/api/documents");
  const menu = el("div", { class: "overflow-menu", style: "position:absolute; bottom:calc(100% + 8px); left:0; z-index:60;" });
  if (docs.length === 0) {
    menu.appendChild(el("div", { class: "overflow-menu-item", text: "No documents yet (see Library tab)" }));
  } else {
    for (const d of docs) {
      menu.appendChild(menuItem(ICON_DOC, d.title, async () => {
        const full = await api(`/api/documents/${d.id}`);
        const input = document.getElementById("chat-input");
        input.value = (input.value ? input.value + "\n\n" : "") + `[Document: ${full.title}]\n${full.content}`;
        input.dispatchEvent(new Event("input"));
        input.focus();
        menu.remove();
      }));
    }
  }
  anchor.parentElement.style.position = "relative";
  anchor.parentElement.appendChild(menu);
  setTimeout(() => document.addEventListener("click", function close() {
    menu.remove();
    document.removeEventListener("click", close);
  }), 0);
}

// -- workspace (real folder confinement — core/workspace.py) --------------
let workspaceModal = null;
let workspaceCurPath = "";

function getWorkspaceModal() {
  if (workspaceModal) return workspaceModal;
  const pathInput = el("input", { class: "workspace-path-input", placeholder: "Type or paste a folder path, then press Enter" });
  const body = el("div", { class: "workspace-body" });
  const useBtn = el("button", { class: "btn", text: "Use this folder" });
  const cancelBtn = el("button", { class: "btn", text: "Cancel" });
  const closeBtn = el("button", { class: "modal-close-btn" });
  closeBtn.insertAdjacentHTML("beforeend", ICON_X);

  const panel = el("div", { class: "glass modal-panel" }, [
    el("h4", { text: "Select workspace" }, [closeBtn]),
    el("div", { class: "muted", text: "File tools are confined to this folder for this chat. Shell commands start here but are not sandboxed and can reach outside it." }),
    pathInput,
    body,
    el("div", { class: "modal-footer" }, [cancelBtn, useBtn]),
  ]);
  const backdrop = el("div", { class: "modal-backdrop hidden" }, [panel]);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeWorkspaceModal(); });
  panel.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(backdrop);

  closeBtn.addEventListener("click", closeWorkspaceModal);
  cancelBtn.addEventListener("click", closeWorkspaceModal);
  pathInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); navigateWorkspace(pathInput.value.trim(), body, pathInput, useBtn); }
  });
  useBtn.addEventListener("click", async () => {
    if (!activeSessionId) return;
    const res = await api(`/api/sessions/${activeSessionId}/workspace`, { method: "POST", body: JSON.stringify({ path: workspaceCurPath }) });
    closeWorkspaceModal();
    syncWorkspacePill(res.workspace_dir);
  });

  workspaceModal = { backdrop, body, pathInput, useBtn };
  return workspaceModal;
}

async function navigateWorkspace(path, body, pathInput, useBtn) {
  const data = await api(`/api/workspace/browse?path=${encodeURIComponent(path || "")}`);
  workspaceCurPath = data.path;
  pathInput.value = data.path;
  body.innerHTML = "";
  if (data.parent) {
    body.appendChild(el("div", { class: "workspace-row", text: "↑ ..", onclick: () => navigateWorkspace(data.parent, body, pathInput, useBtn) }));
  }
  if (data.dirs.length === 0 && !data.parent) {
    body.appendChild(el("div", { class: "workspace-empty", text: "No subfolders" }));
  }
  for (const d of data.dirs) {
    const row = el("div", { class: "workspace-row", onclick: () => navigateWorkspace(d.path, body, pathInput, useBtn) });
    row.insertAdjacentHTML("afterbegin", ICON_WORKSPACE);
    row.appendChild(el("span", { text: d.name }));
    body.appendChild(row);
  }
  useBtn.disabled = data.selectable === false;
  useBtn.title = data.selectable === false ? "This folder cannot be used as a workspace" : "";
}

async function openWorkspaceModal() {
  const modal = getWorkspaceModal();
  modal.backdrop.classList.remove("hidden");
  const session = activeSessionId ? await api(`/api/sessions/${activeSessionId}`) : null;
  await navigateWorkspace((session && session.workspace_dir) || "", modal.body, modal.pathInput, modal.useBtn);
}

function closeWorkspaceModal() {
  if (workspaceModal) workspaceModal.backdrop.classList.add("hidden");
}

// -- integrations / connectors (David's ask 2026-08-31, matching Claude's
// own per-conversation connector toggle — Settings > Integrations owns
// adding/removing MCP Tool Servers; this is just which of the already-
// registered ones this specific chat can reference). ---------------------
let integrationsModal = null;

function getIntegrationsModal() {
  if (integrationsModal) return integrationsModal;
  const body = el("div", { class: "workspace-body" });
  const saveBtn = el("button", { class: "btn", text: "Save" });
  const closeBtn = el("button", { class: "modal-close-btn" });
  closeBtn.insertAdjacentHTML("beforeend", ICON_X);

  const panel = el("div", { class: "glass modal-panel" }, [
    el("h4", { text: "Integrations" }, [closeBtn]),
    el("div", { class: "muted", text: "Which connected integrations this chat can reference. Add or remove integrations themselves in Settings." }),
    body,
    el("div", { class: "modal-footer" }, [saveBtn]),
  ]);
  const backdrop = el("div", { class: "modal-backdrop hidden" }, [panel]);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeIntegrationsModal(); });
  panel.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(backdrop);
  closeBtn.addEventListener("click", closeIntegrationsModal);

  integrationsModal = { backdrop, body, saveBtn };
  return integrationsModal;
}

async function openIntegrationsModal() {
  const modal = getIntegrationsModal();
  modal.backdrop.classList.remove("hidden");
  modal.body.innerHTML = "";

  const [allIntegrations, session] = await Promise.all([
    api("/api/integrations").catch(() => []),
    activeSessionId ? api(`/api/sessions/${activeSessionId}`) : null,
  ]);
  const mcpIntegrations = allIntegrations.filter((i) => i.kind === "mcp_server");
  const enabledIds = session ? session.enabled_integration_ids : null; // null = all enabled

  if (mcpIntegrations.length === 0) {
    modal.body.appendChild(el("div", { class: "workspace-empty", text: "No MCP Tool Server integrations added yet — add one in Settings > Integrations." }));
    modal.saveBtn.style.display = "none";
    return;
  }
  modal.saveBtn.style.display = "";

  const checks = {};
  for (const integ of mcpIntegrations) {
    const cb = el("input", { type: "checkbox" });
    cb.checked = enabledIds === null || enabledIds.includes(integ.id);
    checks[integ.id] = cb;
    modal.body.appendChild(el("label", { class: "workspace-row", style: "cursor:pointer;" }, [
      cb,
      el("span", { text: integ.name }),
    ]));
  }

  modal.saveBtn.onclick = async () => {
    if (!activeSessionId) { closeIntegrationsModal(); return; }
    const checkedIds = Object.entries(checks).filter(([, cb]) => cb.checked).map(([id]) => id);
    // All checked -> store null ("every registered one," including future
    // additions) rather than a literal id list that would silently exclude
    // a newly added integration next time.
    const allChecked = checkedIds.length === mcpIntegrations.length;
    await api(`/api/sessions/${activeSessionId}/integrations`, {
      method: "POST",
      body: JSON.stringify({ enabled_integration_ids: allChecked ? null : checkedIds }),
    });
    closeIntegrationsModal();
  };
}

function closeIntegrationsModal() {
  if (integrationsModal) integrationsModal.backdrop.classList.add("hidden");
}

function syncWorkspacePill(path) {
  const slot = document.getElementById("workspace-pill-slot");
  if (!slot) return;
  slot.innerHTML = "";
  if (!path) return;
  const name = path.replace(/[\\/]+$/, "").split(/[\\/]/).pop();
  const pill = el("div", { class: "workspace-pill", title: `Workspace: ${path}` }, [el("span", { text: name })]);
  const clearBtn = el("button", { type: "button", title: "Clear workspace" });
  clearBtn.insertAdjacentHTML("beforeend", ICON_X);
  clearBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!activeSessionId) return;
    await api(`/api/sessions/${activeSessionId}/workspace`, { method: "POST", body: JSON.stringify({ path: null }) });
    syncWorkspacePill(null);
  });
  pill.appendChild(clearBtn);
  slot.appendChild(pill);
}

// -- model picker -----------------------------------------------------------
// David's ask 2026-08-31 (follow-up): no default model — the picker used to
// always list a free "JARVIS (Claude)" option (id ""). Now every option,
// Claude included, is a real endpoint the user added in Settings > Add
// Models; an empty list means truly nothing's configured yet.
const NO_MODEL_LABEL = "No model — add one in Settings";

async function refreshModelPicker(currentEndpointId) {
  const label = document.getElementById("model-picker-label");
  const menu = document.getElementById("model-picker-menu");
  if (!label || !menu) return;
  const endpoints = await api("/api/models");
  menu.innerHTML = "";

  const options = endpoints.map((ep) => ({
    id: ep.id,
    name: ep.kind === "claude_cli" ? `${ep.name} (${ep.model || "CLI default"})` : `${ep.name} (${ep.model})`,
  }));
  if (options.length === 0) {
    menu.appendChild(el("div", { class: "model-picker-item", text: "No models added yet — see Settings" }));
  }
  for (const opt of options) {
    const item = el("div", {
      class: "model-picker-item" + (opt.id === (currentEndpointId || "") ? " active" : ""),
      text: opt.name,
      onclick: async () => {
        closeMenu(menu);
        if (!activeSessionId) return;
        await api(`/api/sessions/${activeSessionId}/model`, { method: "POST", body: JSON.stringify({ model_endpoint_id: opt.id }) });
        label.textContent = opt.name;
      },
    });
    menu.appendChild(item);
  }
  const active = options.find((o) => o.id === currentEndpointId);
  label.textContent = active ? active.name : NO_MODEL_LABEL;
}

async function refreshSessions(sessionsList, messages) {
  const sessions = await api("/api/sessions");
  sessionsList.innerHTML = "";
  if (sessions.length === 0) {
    sessionsList.appendChild(el("div", { class: "empty-state", text: "No chats yet" }));
    return;
  }
  for (const session of sessions) {
    const item = el("div", {
      class: "session-item" + (session.id === activeSessionId ? " active" : ""),
      onclick: () => openSession(session.id, sessionsList, messages),
      oncontextmenu: (e) => {
        e.preventDefault();
        showSessionMenu(e.clientX, e.clientY, session, item, sessionsList, messages);
      },
    });
    if (session.starred) item.appendChild(el("span", { class: "star-mark", text: "★ " }));
    item.appendChild(document.createTextNode(session.title));
    sessionsList.appendChild(item);
  }
  if (!activeSessionId) {
    renderWelcome(messages);
    await refreshModelPicker(null);
    syncWorkspacePill(null);
  }
}

// Right-click star/delete (David's ask, 2026-08-31) — a small floating menu
// appended to <body> rather than the session item itself, so it isn't
// clipped by the scrollable sidebar panel.
let openMenu = null;

function closeSessionMenu() {
  if (openMenu) { openMenu.remove(); openMenu = null; }
  document.removeEventListener("click", closeSessionMenu);
}

function showSessionMenu(x, y, session, item, sessionsList, messages) {
  closeSessionMenu();
  const menu = el("div", { class: "glass context-menu", style: `left:${x}px;top:${y}px;` });

  const renameItem = el("div", {
    class: "context-menu-item",
    text: "Rename",
    onclick: () => {
      closeSessionMenu();
      startRename(item, session, sessionsList, messages);
    },
  });
  const starItem = el("div", {
    class: "context-menu-item",
    text: session.starred ? "☆ Unstar" : "★ Star",
    onclick: async () => {
      await api(`/api/sessions/${session.id}/star`, { method: "POST", body: JSON.stringify({ starred: !session.starred }) });
      closeSessionMenu();
      await refreshSessions(sessionsList, messages);
    },
  });
  const deleteItem = el("div", {
    class: "context-menu-item danger",
    text: "Delete",
    onclick: async () => {
      closeSessionMenu();
      // Deleting a whole conversation used to happen on one click with no
      // undo — the most destructive single action in the app (audit
      // 2026-09-03).
      const ok = await confirmDialog({
        title: "Delete this chat?",
        message: `"${session.title}" and its full message history will be permanently deleted. This can't be undone.`,
        confirmLabel: "Delete chat",
      });
      if (!ok) return;
      await api(`/api/sessions/${session.id}`, { method: "DELETE" });
      if (activeSessionId === session.id) {
        activeSessionId = null;
        messages.innerHTML = "";
      }
      await refreshSessions(sessionsList, messages);
      toast("Chat deleted", "success");
    },
  });

  menu.append(renameItem, starItem, deleteItem);
  document.body.appendChild(menu);
  openMenu = menu;
  // Deferred so the click that opened the menu doesn't immediately close it.
  setTimeout(() => document.addEventListener("click", closeSessionMenu), 0);
}

function startRename(item, session, sessionsList, messages) {
  item.innerHTML = "";
  const input = el("input", { class: "session-rename-input", value: session.title });
  item.appendChild(input);
  input.focus();
  input.select();

  let done = false;
  const commit = async () => {
    if (done) return;
    done = true;
    const newTitle = input.value.trim();
    if (newTitle && newTitle !== session.title) {
      await api(`/api/sessions/${session.id}`, { method: "PATCH", body: JSON.stringify({ title: newTitle }) });
    }
    await refreshSessions(sessionsList, messages);
  };

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); commit(); }
    if (e.key === "Escape") { e.preventDefault(); done = true; refreshSessions(sessionsList, messages); }
  });
  input.addEventListener("blur", commit);
  // Renaming shouldn't also open the chat underneath the input.
  input.addEventListener("click", (e) => e.stopPropagation());
}

async function createSession() {
  const session = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) });
  activeSessionId = session.id;
  const sessionsList = document.getElementById("sessions-list");
  const messages = document.getElementById("chat-messages");
  await refreshSessions(sessionsList, messages);
  await openSession(session.id, sessionsList, messages);
}

async function openSession(sessionId, sessionsList, messages) {
  activeSessionId = sessionId;
  stagedAttachments = [];
  const attachStrip = document.getElementById("attach-strip");
  if (attachStrip) attachStrip.innerHTML = "";
  [...sessionsList.children].forEach((c) => c.classList.remove("active"));
  const session = await api(`/api/sessions/${sessionId}`);
  messages.innerHTML = "";
  for (const msg of session.messages) {
    messages.appendChild(messageCard(msg.role, msg.content, msg.ts));
  }
  messages.scrollTop = messages.scrollHeight;
  await refreshModelPicker(session.model_endpoint_id);
  syncWorkspacePill(session.workspace_dir);
  await refreshSessions(sessionsList, messages);
}

async function sendMessage(messages, input, sendBtn, attachStrip) {
  const text = input.value.trim();
  if (!text && stagedAttachments.length === 0) return;

  if (text.startsWith("/")) {
    input.value = "";
    input.style.height = "auto";
    messages.appendChild(messageCard("user", text));
    const sessionsList = document.getElementById("sessions-list");
    const { output } = await runSlashCommand(text, {
      sessionId: () => activeSessionId,
      createSession,
      refreshSessions: () => refreshSessions(sessionsList, messages),
      onCurrentSessionDeleted: () => { activeSessionId = null; messages.innerHTML = ""; },
      onWorkspaceCleared: () => syncWorkspacePill(null),
    });
    messages.appendChild(messageCard("assistant", output));
    messages.scrollTop = messages.scrollHeight;
    // Persist so the command + its reply survive leaving and reopening this
    // chat — real bug found live: they were only ever appended to the DOM,
    // never saved, so they vanished on reopen. activeSessionId may have
    // changed by now (e.g. /new), which is correct: log into whichever
    // session the command actually landed in.
    if (activeSessionId) {
      await api(`/api/sessions/${activeSessionId}/messages`, { method: "POST", body: JSON.stringify({ role: "user", content: text }) });
      await api(`/api/sessions/${activeSessionId}/messages`, { method: "POST", body: JSON.stringify({ role: "assistant", content: output }) });
      await refreshSessions(sessionsList, messages);
    }
    input.focus();
    return;
  }

  if (!activeSessionId) await createSession();

  const attachmentIds = stagedAttachments.map((a) => a.id);
  stagedAttachments = [];
  renderAttachStrip(attachStrip);

  input.value = "";
  input.style.height = "auto";
  sendBtn.disabled = true;
  messages.appendChild(messageCard("user", text || "(attachment)"));
  const replyCard = messageCard("assistant", "");
  const replyBody = replyCard.querySelector(".msg-body");
  messages.appendChild(replyCard);
  messages.scrollTop = messages.scrollHeight;

  // Idle "thinking" state (David's ask 2026-09-02 for the indicator itself,
  // redesigned Claude-style 2026-09-03) — shown until the first real text
  // chunk arrives, so a long tool-using turn doesn't look frozen. Now a CSS
  // shimmer instead of a JS-driven dot rotation, so there's no interval to
  // leak if an exit path is ever missed.
  const thinkingEl = thinkingIndicator();
  replyBody.appendChild(thinkingEl);
  let gotFirstChunk = false;
  const clearThinking = () => { if (thinkingEl.isConnected) thinkingEl.remove(); };

  // Streaming cursor (David's ask 2026-09-02) — separate from the thinking
  // dots above: once real text starts, a mid-turn pause (e.g. a tool call
  // between two text blocks — brain.py's run_turn_stream yields once per
  // completed TextBlock, so a multi-step turn genuinely goes silent between
  // them) used to look identical to "finished." This blinks continuously
  // (CSS animation, not tied to chunk arrival) for as long as the request
  // is open, so a silent gap still visibly reads as "still working."
  // Re-appended after every textContent update below since setting
  // .textContent wipes all child nodes, cursor included.
  const cursor = el("span", { class: "msg-cursor" });

  // Real bug found by audit 2026-09-03: this whole block was unguarded.
  // sendBtn.disabled was set true above, so ANY failure here (backend
  // restart mid-turn, a 500, a dropped connection, malformed SSE) threw out
  // of sendMessage() and never re-enabled it — the composer stayed
  // permanently dead and the thinking dots animated forever on a stuck
  // card, with tab-switching the only recovery. The finally block below is
  // the actual fix; the catch turns a silent brick into a visible error.
  let failed = false;
  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: activeSessionId, message: text, attachment_ids: attachmentIds }),
    });
    if (!res.ok || !res.body) throw new Error(`stream failed (${res.status})`);

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
        let payload;
        // One malformed frame shouldn't abort an otherwise-good reply.
        try { payload = JSON.parse(line.slice(6)); } catch (_) { continue; }
        if (payload.chunk) {
          if (!gotFirstChunk) {
            gotFirstChunk = true;
            clearThinking();
            replyBody.textContent = "";
          }
          replyBody.textContent += payload.chunk;
          replyBody.appendChild(cursor);
          messages.scrollTop = messages.scrollHeight;
        }
      }
    }
  } catch (e) {
    failed = true;
    clearThinking();
    replyBody.textContent = gotFirstChunk
      ? replyBody.textContent + "\n\n[Reply interrupted — the connection dropped before it finished.]"
      : "[Couldn't reach JARVIS. The backend may be restarting — try again.]";
    replyCard.classList.add("msg-failed");
    toast(`Message failed: ${e.message}`, "error");
  } finally {
    clearThinking();
    cursor.remove();
    sendBtn.disabled = false;
  }

  // "Done" marker (David's ask 2026-09-02) — a small checkmark next to the
  // timestamp, permanent once added. Only the live-streamed reply gets
  // this, not messages replayed from history on openSession() — those are
  // never ambiguous about being finished, nothing is actively streaming
  // when they're rendered. Skipped on failure: a checkmark on an
  // interrupted reply would claim it completed successfully.
  if (!failed) {
    const doneIcon = el("span", { class: "msg-done" });
    doneIcon.insertAdjacentHTML("beforeend", ICON_DONE);
    // Lives in the hover action row now that the per-message header is
    // gone (Claude-style redesign, 2026-09-03). `.has-done` keeps that row
    // permanently visible on this one message — the checkmark is a status,
    // not a hover affordance.
    const actionRow = replyCard.querySelector(".msg-actions");
    actionRow.appendChild(doneIcon);
    actionRow.classList.add("has-done");
  }

  input.focus();
  const sessionsList = document.getElementById("sessions-list");
  await refreshSessions(sessionsList, messages);
}
