import { api, el, toast, confirmDialog } from "../api.js";
import { runSlashCommand } from "../slashCommands.js";
import * as chatStream from "../chatStream.js";
import { renderMessageBody, copyText, closeArtifact } from "../chatContent.js";
import { openBrowser, closeBrowser } from "../browserPane.js";
import { createRecorder, transcribeBlob, getSpeechStatus, isRecordingSupported } from "../voiceInput.js";
import { createOpenMic } from "../openMic.js";
import { createChatActivity } from '../chatActivity.js';

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
const ICON_STOP = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
const ICON_MIC = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>';
const ICON_OPENMIC = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M4 4l16 16" opacity="0"/><circle cx="12" cy="12" r="10.5" opacity=".45"/></svg>';
const ICON_SEND = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>';
const ICON_X = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg>';
const ICON_CHEVRON = '<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>';
const ICON_COPY = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const ICON_PLUG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 2v6"/><path d="M15 2v6"/><path d="M12 17v5"/><path d="M6 8h12a2 2 0 0 1 2 2v2a6 6 0 0 1-6 6h-4a6 6 0 0 1-6-6v-2a2 2 0 0 1 2-2z"/></svg>';
const ICON_GLOBE = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>';
const ICON_CHATS = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5h16v11H8l-4 4V5z"/></svg>';
// "Done" marker (David's ask 2026-09-02: a clear indicator for when a reply
// has fully finished, distinct from mid-turn pauses that can look frozen).
const ICON_DONE = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
// Projects (David's ask 2026-09-12).
const ICON_FOLDER = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h5l2 2h9a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z"/></svg>';
const ICON_GEAR = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';

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
function messageCard(role, text, ts, status = 'complete') {
  const time = new Date((ts || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const body = el("div", { class: "msg-body" });
  renderMessageBody(body, text, activeSessionId, role === 'assistant');
  const copyBtn = el("button", { type: "button", class: "msg-action-btn", title: "Copy" });
  copyBtn.insertAdjacentHTML("beforeend", ICON_COPY);
  copyBtn.addEventListener("click", async () => {
    await copyText(body._rawText || '');
  });
  // Timestamp moved off the (now removed) header into the hover row, so the
  // information is still there without putting a label on every message.
  const actions = el("div", { class: "msg-actions" }, [copyBtn, el("span", { class: "msg-time", text: time })]);

  const card = el("div", { class: `msg ${role}` }, [body, actions]);
  if (status === 'interrupted') card.append(el('div', { class: 'msg-interrupted', text: 'Response interrupted · Partial reply saved' }));
  return card;
}

let activeSessionId = null;
let activeProjectFilter = null; // David's ask 2026-09-12 — null = "All Chats"
let stagedAttachments = []; // [{id, filename}]
// chatStream subscriptions made by whatever's currently mounted, unwound by
// render()'s returned unmount function (David's ask 2026-09-12 — see
// chatStream.js's docstring) — without this, leaving Chat mid-reattachment
// would keep a listener alive pointing at DOM this view already discarded.
let activeUnsubscribers = [];

// Drives a reply card's DOM from chatStream's shared state instead of a
// local fetch loop (David's ask 2026-09-12) — used both right after
// sending a message and when reopening a chat that's still generating, so
// the two cases render identically and a listener never has to care which
// one it started as. Returns the unsubscribe function; callers push it
// onto activeUnsubscribers so render()'s unmount can clean it up.
function attachToInFlight(sessionId, messages, replyCard, replyBody, sendBtn) {
  const cursor = el("span", { class: "msg-cursor" });
  const initial = chatStream.getInFlight(sessionId);
  const activity = createChatActivity();
  replyCard.insertBefore(activity.node, replyBody);
  // Deliberately not touching sendBtn.disabled here: syncChatBusy() below
  // owns that control now and flips it into stop mode, so disabling it would
  // remove the only way to end the turn this function is rendering.
  if (sendBtn) syncChatBusy(true);

  const paint = (entry) => {
    activity.update(entry);
    const follow = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 100;
    const current = sessionId === activeSessionId && replyCard.isConnected;
    if (current) syncChatBusy(entry.status === 'processing');
    replyCard.setAttribute('aria-busy', String(entry.status === 'processing'));
    if (entry.text) {
      renderMessageBody(replyBody, entry.text, sessionId);
      if (entry.status === "processing") replyBody.appendChild(cursor);
    }
    if (entry.status === "done") {
      cursor.remove();
      if (!entry.text) replyBody.textContent = 'No text response was returned.';
      const actionRow = replyCard.querySelector(".msg-actions");
      if (!actionRow.classList.contains("has-done")) {
        const doneIcon = el("span", { class: "msg-done" });
        doneIcon.insertAdjacentHTML("beforeend", ICON_DONE);
        actionRow.appendChild(doneIcon);
        actionRow.classList.add("has-done");
      }
      unsubscribe();
    } else if (entry.status === "stopped") {
      // Deliberate stop, so it reads as an ended reply rather than a fault.
      // The backend already saved whatever had streamed, so the text on
      // screen is the text that was kept.
      cursor.remove();
      renderMessageBody(replyBody, entry.text, sessionId);
      if (!entry.text) replyBody.textContent = 'Stopped before a response started.';
      replyCard.append(el('div', { class: 'msg-stopped', role: 'status', text: 'Stopped · partial reply saved' }));
      unsubscribe();
    } else if (entry.status === "failed") {
      cursor.remove();
      renderMessageBody(replyBody, entry.text, sessionId);
      replyCard.append(el('div', { class: 'msg-interrupted', role: 'status', text: entry.error || 'Response interrupted. Try sending your message again.' }));
      replyCard.classList.add("msg-failed");
      toast(`Message failed: ${entry.error || "connection dropped"}`, "error");
      unsubscribe();
    }
    // Guarded on still-being-the-active-session: this callback keeps firing
    // for as long as the turn runs even if the user has since switched to a
    // different chat (a live replyCard for a session that's no longer on
    // screen just goes quietly stale, which is correct — the floating
    // overlay is what surfaces its progress elsewhere, not this DOM).
    // Without the guard, a background turn finishing would scroll/refresh
    // whatever chat happens to be visible right now, not its own.
    if (current) {
      if (follow) messages.scrollTop = messages.scrollHeight;
      messages.dispatchEvent(new Event('scroll'));
      if (entry.status === "done" || entry.status === "failed" || entry.status === "stopped") {
        const sessionsList = document.getElementById("sessions-list");
        if (sessionsList) refreshSessions(sessionsList, messages);
        // The turn that just finished is what produced the new reading.
        refreshContextMeter(sessionId);
      }
    }
  };

  let paintTimer = null;
  const detach = chatStream.subscribe(sessionId, entry => {
    if (entry.status === 'processing') {
      // Coalesce token bursts; do not rebuild Markdown for every byte.
      if (paintTimer === null) paintTimer = setTimeout(() => { paintTimer = null; paint(entry); }, 40);
    } else { clearTimeout(paintTimer); paintTimer = null; paint(entry); }
  });
  const unsubscribe = () => { clearTimeout(paintTimer); detach(); };
  if (initial) paint(initial); // reattach case: render whatever's already there, don't wait for the next chunk
  return unsubscribe;
}

// Keep one live composer: moving it never replaces its input or attachments.
function syncChatLayout(messages, title) {
  const main = messages.closest('#chat-main');
  if (!main) return;
  const dock = main.querySelector('.chat-composer-dock');
  const empty = !messages.childElementCount;
  if (main.classList.contains('is-empty') !== empty) {
    dock.getAnimations().forEach(animation => animation.cancel());
    const before = dock.getBoundingClientRect();
    main.classList.toggle('is-empty', empty);
    const after = dock.getBoundingClientRect();
    if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
      dock.animate([
        { transform: `translateY(${before.top - after.top}px)` },
        { transform: 'translateY(0)' },
      ], { duration: 380, easing: 'cubic-bezier(.22,1,.36,1)' });
    }
  }
  if (title !== undefined) {
    const label = main.querySelector('.chat-title');
    label.textContent = title || 'New chat';
    label.title = title || 'New chat';
  }
}

function renderWelcome(messages) {
  messages.replaceChildren();
  syncChatLayout(messages, 'New chat');
}

export async function render(container, tabId, options = {}) {
  container.innerHTML = "";
  container.classList.add("chat-layout");
  stagedAttachments = [];
  // Belt-and-suspenders reset (David's ask 2026-09-12): app.js's switchTab()
  // already calls the previous mount's returned unmount — see the bottom of
  // this function — which drains this same array before a fresh render()
  // ever runs, so this should always already be empty. Resetting explicitly
  // anyway means a future bug in that call order fails safe (no leaked
  // listeners) instead of silently accumulating one per tab visit.
  activeUnsubscribers = [];
  const mountSubscriptions = activeUnsubscribers;
  // Every fresh landing on the Chat tab starts at the welcome state, even
  // if a session was open the last time this tab was visited — matches a
  // typical chat app's "new chat by default" convention rather than
  // silently resuming wherever you left off.
  activeSessionId = null;

  const sessionsPanel = el("div", { id: "chat-sessions" });

  // Projects (David's ask 2026-09-12) — a filter above the chat list,
  // matching Claude/ChatGPT's own "you're inside a project" framing:
  // picking one narrows the list to that project's chats and any new chat
  // created while it's active is assigned to it automatically (see
  // createSession() below). Gear only shows once a specific project is
  // selected — "All Chats" has no settings of its own to edit.
  activeProjectFilter = options.projectId || null;
  const projectPickerBtn = el("button", { type: "button", class: "model-picker-btn", id: "project-picker-btn" }, [
    el("span", { id: "project-picker-label", text: "All Chats" }),
  ]);
  projectPickerBtn.insertAdjacentHTML("beforeend", ICON_CHEVRON);
  const projectPickerMenu = el("div", { class: "overflow-menu below hidden", id: "project-picker-menu" });
  projectPickerBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleMenu(projectPickerMenu);
    refreshProjectPicker(sessionsList, messages);
  });
  const projectSettingsBtn = el("button", {
    type: "button", class: "input-icon-btn", id: "project-settings-btn", title: "Project settings", style: "display:none;",
    onclick: async () => { if (activeProjectFilter) openProjectModal(await api(`/api/projects/${activeProjectFilter}`)); },
  });
  projectSettingsBtn.insertAdjacentHTML("beforeend", ICON_GEAR);
  const projectPickerWrap = el("div", { class: "project-picker-wrap" }, [projectPickerBtn, projectSettingsBtn, projectPickerMenu]);

  const newBtn = el("button", { type: "button", class: "btn chat-new-btn", text: "New chat", onclick: startNewChat });
  newBtn.insertAdjacentHTML('afterbegin', ICON_PLUS);
  const sessionsList = el("div", { id: "sessions-list" });
  sessionsPanel.append(projectPickerWrap, newBtn, sessionsList);

  // History has its own preference, independent of the global icon rail.
  const sessionsBackdrop = el("div", { class: "chat-sessions-backdrop hidden" });
  const openSessionsBtn = el("button", { type: "button", id: "chat-history-toggle", class: "input-icon-btn chat-mobile-menu-btn", "aria-controls": "chat-sessions" });
  openSessionsBtn.insertAdjacentHTML("beforeend", ICON_CHATS);
  const headerNewBtn = el('button', { type: 'button', class: 'input-icon-btn chat-header-new', title: 'New chat', 'aria-label': 'New chat', onclick: startNewChat });
  headerNewBtn.insertAdjacentHTML('beforeend', ICON_PLUS);
  const mobileHeader = el("div", { class: "chat-mobile-header" }, [openSessionsBtn, el("span", { class: 'chat-title', text: "New chat" }), headerNewBtn]);
  const desktopChat = matchMedia('(min-width: 769px)');
  let historyCollapsed = true;
  try { historyCollapsed = localStorage.getItem('jarvis:chat-history-collapsed') !== 'false'; } catch { /* Storage is optional. */ }
  function syncHistory() {
    container.classList.toggle('chat-history-collapsed', historyCollapsed);
    const visible = desktopChat.matches ? !historyCollapsed : sessionsPanel.classList.contains('open');
    sessionsPanel.inert = !visible;
    sessionsPanel.setAttribute('aria-hidden', String(!visible));
    openSessionsBtn.setAttribute('aria-expanded', String(visible));
    openSessionsBtn.setAttribute('aria-label', visible ? 'Hide chat history' : 'Show chat history');
    openSessionsBtn.title = visible ? 'Hide chat history' : 'Show chat history';
  }
  function closeSessionsDrawer(restoreFocus = false) {
    const wasOpen = sessionsPanel.classList.contains('open');
    sessionsPanel.classList.remove('open');
    sessionsBackdrop.classList.add('hidden');
    syncHistory();
    if (restoreFocus && wasOpen) openSessionsBtn.focus();
  }
  openSessionsBtn.addEventListener('click', () => {
    if (desktopChat.matches) {
      historyCollapsed = !historyCollapsed;
      try { localStorage.setItem('jarvis:chat-history-collapsed', String(historyCollapsed)); } catch { /* Storage is optional. */ }
    } else {
      const open = sessionsPanel.classList.toggle('open');
      sessionsBackdrop.classList.toggle('hidden', !open);
    }
    syncHistory();
    if (!desktopChat.matches && sessionsPanel.classList.contains('open')) newBtn.focus();
  });
  const historyCloseBtn = el('button', { type: 'button', class: 'input-icon-btn chat-history-close', text: 'Close', 'aria-label': 'Close chat history', onclick: () => closeSessionsDrawer(true) });
  sessionsPanel.prepend(historyCloseBtn);
  sessionsBackdrop.addEventListener('click', () => closeSessionsDrawer(true));
  const onChatBreakpoint = () => closeSessionsDrawer();
  desktopChat.addEventListener('change', onChatBreakpoint);
  syncHistory();

  const main = el("div", { id: "chat-main", class: 'is-empty' }, [mobileHeader]);
  const messages = el("div", { id: "chat-messages" });
  const attachStrip = el("div", { id: "attach-strip", class: "attach-strip" });

  // -- composer: top row (textarea + model picker) --------------------
  const input = el("textarea", { id: "chat-input", rows: "1", placeholder: "Where should we start?", "aria-label": "Message JARVIS" });
  const modelBtn = el("button", { type: "button", class: "model-picker-btn", id: "model-picker-btn" }, [
    el("span", { id: "model-picker-label", text: "No model — add one in Settings" }),
  ]);
  modelBtn.insertAdjacentHTML("beforeend", ICON_CHEVRON);
  const modelMenu = el("div", { class: "model-picker-menu hidden", id: "model-picker-menu" });
  const modelWrap = el("div", { class: "model-picker-wrap" }, [modelBtn, modelMenu]);
  modelBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleMenu(modelMenu); });
  const versionBtn = el('button', { type: 'button', class: 'model-picker-btn', id: 'model-version-btn', text: 'Model version', hidden: true });
  const versionMenu = el('div', { class: 'model-picker-menu hidden version-menu', id: 'model-version-menu' });
  versionBtn.addEventListener('click', e => { e.stopPropagation(); toggleMenu(versionMenu); });
  versionMenu.addEventListener('click', e => e.stopPropagation());
  const versionWrap = el('div', { class: 'model-picker-wrap' }, [versionBtn, versionMenu]);

  const inputTop = el("div", { class: "chat-input-top" }, [input]);

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
  // Side browser (David's ask 2026-09-15) — an explicit user action, which
  // is the only way the pane ever opens. The other entry point is activating
  // a link in a reply (see app.js's onOpenRequest). Nothing opens it
  // automatically, and no agent tool reads from it.
  const browseItem = menuItem(ICON_GLOBE, "Browse the web", () => { closeMenu(overflowMenu); openBrowser(''); });
  overflowMenu.append(attachItem, docItem, workspaceItem, browseItem, integrationsItem, promptItem);
  const overflowWrap = el("div", { class: "overflow-wrapper" }, [overflowBtn, overflowMenu, fileInput]);

  const workspacePill = el("div", { id: "workspace-pill-slot" });
  // Context meter (David's ask 2026-09-15) — how full THIS chat's context
  // currently is, which is a different question from the Home tab's
  // cumulative token spend. Hidden until a turn actually reports usable
  // usage, so it never occupies the composer with a placeholder.
  const contextPill = el("div", { id: "context-pill", class: "context-pill", hidden: true, role: "status" });

  const sendBtn = el("button", { class: "btn", id: "chat-send", title: "Send" });
  sendBtn.insertAdjacentHTML("beforeend", ICON_SEND);

  const inputLeft = el("div", { class: "chat-input-left" }, [overflowWrap, workspacePill, contextPill]);
  // Dictation (David's ask 2026-09-15). Hidden outright when the browser
  // cannot record, rather than offered and then failing on click.
  const micBtn = el("button", { type: "button", class: "input-icon-btn chat-mic-btn", id: "chat-mic", title: "Dictate", "aria-label": "Dictate a message" });
  micBtn.insertAdjacentHTML("beforeend", ICON_MIC);
  micBtn.hidden = !isRecordingSupported();
  wireMic(micBtn, input);
  const openMicBtn = el("button", { type: "button", class: "input-icon-btn chat-openmic-btn", id: "chat-openmic", title: "Open Mic", "aria-label": "Start an Open Mic conversation" });
  openMicBtn.insertAdjacentHTML("beforeend", ICON_OPENMIC);
  openMicBtn.hidden = !isRecordingSupported();
  openMicBtn.addEventListener("click", () => setOpenMic(!isOpenMicActive(), openMicBtn));
  const inputRight = el("div", { class: "chat-input-right" }, [modelWrap, versionWrap, micBtn, openMicBtn, sendBtn]);
  const inputBottom = el("div", { class: "chat-input-bottom" }, [inputLeft, inputRight]);

  const composer = el("div", { class: "glass chat-input-bar border-beam" }, [inputTop, inputBottom]);
  const dock = el('div', { class: 'chat-composer-dock' }, [attachStrip, composer, el("div", { class: "composer-hint", text: "Enter to send · Shift + Enter for a new line" })]);
  main.append(messages, dock);
  const dockObserver = new ResizeObserver(() => main.style.setProperty('--composer-height', `${dock.offsetHeight}px`));
  dockObserver.observe(dock);
  const jump = el('button', { type: 'button', class: 'chat-jump btn', text: '↓ Latest', hidden: true, onclick: () => messages.scrollTo({ top: messages.scrollHeight, behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' }) });
  main.append(jump);
  messages.addEventListener('scroll', () => { jump.hidden = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 160; });
  sendBtn.setAttribute("aria-label", "Send message");
  const syncBeam = () => { composer.dataset.active = String(!document.hidden); };
  document.addEventListener("visibilitychange", syncBeam);
  syncBeam();

  container.append(sessionsPanel, sessionsBackdrop, main);
  // Selecting a session or starting a new one closes the mobile drawer —
  // no-op above the breakpoint since the classes it touches are inert there.
  sessionsList.addEventListener("click", () => closeSessionsDrawer(true));

  function startNewChat() {
    closeArtifact();
    activeUnsubscribers.forEach(unsub => unsub());
    activeUnsubscribers.length = 0;
    activeSessionId = null;
    stagedAttachments = [];
    renderAttachStrip(attachStrip);
    input.value = '';
    input.style.height = 'auto';
    jump.hidden = true;
    renderWelcome(messages);
    closeSessionsDrawer();
    refreshSessions(sessionsList, messages).catch(error => toast(error.message, 'error'));
    input.focus();
  }

  sendBtn.addEventListener("click", () => {
    if (sendBtn.dataset.mode === 'stop') { chatStream.stopTurn(activeSessionId); return; }
    sendMessage(messages, input, sendBtn, attachStrip);
  });
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

  const dismissMenus = () => { closeMenu(overflowMenu); closeMenu(modelMenu); closeMenu(versionMenu); closeMenu(projectPickerMenu); };
  const escapeMenus = (event) => {
    if (event.key === "Escape") { dismissMenus(); closeSessionsDrawer(true); }
    if (event.key === 'Tab' && !desktopChat.matches && sessionsPanel.classList.contains('open')) {
      const controls = [...sessionsPanel.querySelectorAll('button, input, [tabindex="0"]')].filter(node => node.getClientRects().length && !node.disabled);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  };
  document.addEventListener("click", dismissMenus);
  document.addEventListener("keydown", escapeMenus);

  let disposed = false;
  const cleanup = () => {
    if (disposed) return;
    disposed = true;
    document.removeEventListener("click", dismissMenus);
    document.removeEventListener("keydown", escapeMenus);
    document.removeEventListener("visibilitychange", syncBeam);
    desktopChat.removeEventListener('change', onChatBreakpoint);
    dockObserver.disconnect();
    dock.getAnimations().forEach(animation => animation.cancel());
    closeSessionMenu();
    closeArtifact();
    closeBrowser();
    // A live microphone must never outlive the view that owns it.
    if (openMic) { openMic.stop(); openMic = null; }
    mountSubscriptions.forEach((unsub) => unsub());
    if (activeUnsubscribers === mountSubscriptions) activeUnsubscribers = [];
  };
  options.registerCleanup?.(cleanup);

  await refreshSessions(sessionsList, messages);
  if (disposed || !container.isConnected) return cleanup;
  await refreshProjectPicker(sessionsList, messages);
  if (disposed || !container.isConnected) return cleanup;
  if (options.sessionId) await openSession(options.sessionId, sessionsList, messages);
  if (disposed || !container.isConnected) return cleanup;

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

  // Unmount (David's ask 2026-09-12): app.js's switchTab() calls this
  // before tearing down the DOM for whichever tab comes next, same
  // convention home.js already uses for its WebGL scene. Without it, a
  // reattached chatStream listener from this mount would keep firing
  // against DOM this view no longer owns.
  return cleanup;
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
  if (!wasHidden) return;
  // Centered composers need menus to choose the side with room, especially
  // on a short phone viewport. Clamp horizontally without clipping controls.
  menu.style.transform = '';
  menu.classList.remove('hidden');
  const anchor = menu.parentElement.getBoundingClientRect();
  const above = anchor.top - 12;
  const below = innerHeight - anchor.bottom - 12;
  const opensBelow = menu.classList.contains('below') || (above < 260 && below > above);
  menu.style.top = opensBelow ? 'calc(100% + 8px)' : 'auto';
  menu.style.bottom = opensBelow ? 'auto' : 'calc(100% + 8px)';
  menu.style.maxHeight = `${Math.max(0, Math.min(320, opensBelow ? below : above))}px`;
  menu.style.maxWidth = 'calc(100vw - 24px)';
  const rect = menu.getBoundingClientRect();
  const offset = rect.left < 12 ? 12 - rect.left : Math.min(0, innerWidth - 12 - rect.right);
  menu.style.transform = `translateX(${offset}px)`;
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

// -- dictation ---------------------------------------------------------------
// Click to start, click again to stop and transcribe. Toggle rather than
// press-and-hold: hold is fiddly with a mouse, impossible to discover, and
// awkward on a phone where the same button has to work by touch.
function wireMic(micBtn, input) {
  let recorder = null;
  const reset = () => {
    recorder = null;
    micBtn.classList.remove('recording', 'busy');
    micBtn.style.removeProperty('--mic-level');
    micBtn.title = 'Dictate';
    micBtn.setAttribute('aria-label', 'Dictate a message');
    micBtn.disabled = false;
  };

  micBtn.addEventListener('click', async () => {
    if (recorder) {
      // Stop and transcribe.
      const active = recorder;
      recorder = null;
      micBtn.classList.remove('recording');
      micBtn.classList.add('busy');
      micBtn.disabled = true;
      micBtn.title = 'Transcribing…';
      try {
        const blob = await active.stop();
        if (!blob) { reset(); return; }
        const text = await transcribeBlob(blob);
        if (!text) {
          toast('No speech was picked up.', 'error');
        } else {
          // Appended rather than replacing: dictation is often used to finish
          // a sentence someone already started typing.
          const existing = input.value.trim();
          input.value = existing ? `${existing} ${text}` : text;
          input.dispatchEvent(new Event('input'));
          input.focus();
        }
      } catch (error) {
        toast(error.message || 'Transcription failed.', 'error');
      }
      reset();
      return;
    }

    // Starting: check the model is actually there before opening the mic, so
    // the failure arrives before recording rather than after.
    const status = await getSpeechStatus();
    if (!status || !status.engine_available) {
      toast('Speech recognition is not available in this build.', 'error');
      return;
    }
    if (!status.active_model) {
      toast('No speech model downloaded yet. Add one in Settings > Speech.', 'error');
      return;
    }
    const active = createRecorder({ onLevel: level => micBtn.style.setProperty('--mic-level', level.toFixed(2)) });
    try {
      await active.start();
    } catch (error) {
      // Denied permission and no device look the same to the caller, so the
      // message covers both rather than guessing.
      toast(error && error.name === 'NotAllowedError'
        ? 'Microphone access was blocked. Allow it and try again.'
        : 'No microphone is available.', 'error');
      return;
    }
    recorder = active;
    micBtn.classList.add('recording');
    micBtn.title = 'Stop and transcribe';
    micBtn.setAttribute('aria-label', 'Stop recording and transcribe');
  });
}

// -- Open Mic ----------------------------------------------------------------
// A session you can talk to continuously: it listens, replies aloud, and can
// be cut off mid-sentence. Turns are ordinary messages while it runs, so
// leaving the mode leaves a real conversation behind; the spoken stretch is
// then folded into a summary server-side so a long exchange does not occupy
// the whole context window.
let openMic = null;

export function isOpenMicActive() { return !!openMic; }

function sendVoiceTurn(text, { onChunk, onDone }) {
  // Reuses the normal turn path rather than a parallel one, so a spoken turn
  // streams, persists and renders exactly like a typed one.
  return new Promise((resolve, reject) => {
    const messages = document.getElementById('chat-messages');
    if (!messages || !activeSessionId) { reject(new Error('No chat is open.')); return; }
    messages.appendChild(messageCard('user', text));
    const replyCard = messageCard('assistant', '');
    const replyBody = replyCard.querySelector('.msg-body');
    messages.appendChild(replyCard);
    syncChatLayout(messages);
    messages.scrollTop = messages.scrollHeight;

    const sessionItem = document.querySelector(`.session-item[data-session-id="${activeSessionId}"]`);
    try {
      chatStream.startTurn(activeSessionId, sessionItem?.dataset.title || 'Chat', text, []);
    } catch (error) { reject(error); return; }
    activeUnsubscribers.push(attachToInFlight(activeSessionId, messages, replyCard, replyBody, document.getElementById('chat-send')));

    let spoken = 0;
    const detach = chatStream.subscribe(activeSessionId, entry => {
      if (entry.text.length > spoken) {
        onChunk?.(entry.text.slice(spoken));
        spoken = entry.text.length;
      }
      if (entry.status === 'processing') return;
      detach();
      // A stopped turn is a barge-in: the loop is already capturing the
      // interruption, so it must not be told to start speaking again.
      if (entry.status === 'stopped') { resolve('stopped'); return; }
      onDone?.();
      if (entry.status === 'failed') reject(new Error(entry.error || 'That turn failed.'));
      else resolve('done');
    });
  });
}

async function setOpenMic(active, button) {
  if (active) {
    const status = await getSpeechStatus();
    if (!status?.engine_available || !status?.active_model) {
      toast(status?.engine_available
        ? 'No speech model downloaded yet. Add one in Settings > Speech.'
        : 'Speech recognition is not available in this build.', 'error');
      return;
    }
    // Starting Open Mic on a brand-new chat creates the session first, which
    // is how "begin a chat as an Open Mic session" works: press it on an
    // empty chat and it opens one and starts listening, rather than needing a
    // separate control that would duplicate this one.
    if (!activeSessionId) {
      try {
        await createSession();
      } catch (error) {
        toast(`Couldn't start chat: ${error.message}`, 'error');
        return;
      }
    }
    try {
      await api(`/api/sessions/${activeSessionId}/open-mic`, { method: 'POST', body: JSON.stringify({ active: true }) });
    } catch (error) { toast(error.message || 'Open Mic could not be started.', 'error'); return; }

    openMic = createOpenMic({
      sessionId: activeSessionId,
      sendMessage: (text, handlers) => sendVoiceTurn(text, handlers),
      stopTurn: (id) => chatStream.stopTurn(id),
      onState: state => {
        button.dataset.micState = state;
        button.title = `Open Mic: ${state}`;
      },
      onError: message => toast(message, 'error'),
    });
    button.classList.add('active');
    document.getElementById('chat-main')?.classList.add('open-mic');
    await openMic.start();
    return;
  }

  const controller = openMic;
  openMic = null;
  controller?.stop();
  button.classList.remove('active');
  button.removeAttribute('data-mic-state');
  button.title = 'Open Mic';
  document.getElementById('chat-main')?.classList.remove('open-mic');
  const sessionId = activeSessionId;
  if (!sessionId) return;
  toast('Ending Open Mic and summarising…', 'success');
  try {
    const result = await api(`/api/sessions/${sessionId}/open-mic`, { method: 'POST', body: JSON.stringify({ active: false }) });
    if (result.summarised && activeSessionId === sessionId) {
      // The transcript was rewritten server-side, so the on-screen copy is
      // now stale and has to be reloaded rather than patched.
      const sessionsList = document.getElementById('sessions-list');
      const messages = document.getElementById('chat-messages');
      if (sessionsList && messages) await openSession(sessionId, sessionsList, messages);
    }
  } catch (error) {
    toast(error.message || 'The summary could not be created.', 'error');
  }
}

// -- model picker -----------------------------------------------------------
// David's ask 2026-08-31 (follow-up): no default model — the picker used to
// always list a free "JARVIS (Claude)" option (id ""). Now every option,
// Claude included, is a real endpoint the user added in Settings > Add
// Models; an empty list means truly nothing's configured yet.
const NO_MODEL_LABEL = "No model — add one in Settings";

// -- context meter ----------------------------------------------------------
// David's ask 2026-09-15: show how much of the model's context this chat is
// currently occupying — explicitly NOT the Home tab's cumulative token spend.
// The value is whatever the last completed turn actually reported, so it
// falls on its own after a compaction and is per-chat by construction (each
// session stores its own; see core/session_manager.py's set_context_state).
//
// Everything here fails quiet: no reading, no meter. A number is only ever
// rendered when the server sent a real measurement, and a percentage only
// when a capacity is genuinely known — an estimated capacity says so in the
// tooltip rather than passing itself off as measured.
function formatTokens(n) {
  if (!Number.isFinite(n)) return '';
  if (n >= 1000000) return `${(n / 1000000).toFixed(n >= 10000000 ? 0 : 1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k`;
  return String(n);
}

async function refreshContextMeter(sessionId) {
  const pill = document.getElementById('context-pill');
  if (!pill) return;
  if (!sessionId) { pill.hidden = true; return; }
  let state = null;
  try { state = await api(`/api/sessions/${sessionId}/context`); } catch { /* a missing reading is not an error worth surfacing */ }
  // The chat may have been switched while that request was in flight — a
  // stale reading must never be painted onto a different session's composer.
  if (sessionId !== activeSessionId || !pill.isConnected) return;
  if (!state || !state.available || !Number.isFinite(state.used_tokens)) { pill.hidden = true; return; }

  const used = state.used_tokens;
  const capacity = Number.isFinite(state.capacity_tokens) ? state.capacity_tokens : null;
  const percent = Number.isFinite(state.percent) ? state.percent : null;
  pill.replaceChildren();
  if (percent !== null && capacity) {
    const bar = el('div', { class: 'context-bar' }, [el('div', { class: 'context-bar-fill' })]);
    bar.firstChild.style.width = `${Math.min(percent, 100)}%`;
    // Purely visual thresholds for "getting full" — they change the colour,
    // never the reported number.
    pill.dataset.level = percent >= 90 ? 'high' : percent >= 70 ? 'warn' : 'ok';
    pill.append(bar, el('span', { class: 'context-text', text: `${percent}%` }));
    const basis = state.estimated_capacity ? 'estimated capacity' : 'reported by the provider';
    pill.title = `Context: ${used.toLocaleString()} of about ${capacity.toLocaleString()} tokens (${basis}).`
      + `\nThis is the current conversation size, not total usage. It drops when the conversation is compacted.`;
  } else {
    // Real measurement, unknown ceiling: show the honest absolute number
    // rather than inventing a denominator to make a percentage out of.
    delete pill.dataset.level;
    pill.append(el('span', { class: 'context-text', text: `${formatTokens(used)} ctx` }));
    pill.title = `Context: ${used.toLocaleString()} tokens in this conversation.`
      + `\nNo context capacity is published for this model, so no percentage is shown.`;
  }
  pill.hidden = false;
}

function syncChatBusy(busy) {
  // Model controls still lock during a turn: switching provider mid-reply
  // would reconnect the brain underneath a running stream.
  for (const id of ['model-picker-btn', 'model-version-btn']) {
    const control = document.getElementById(id);
    if (control) control.disabled = busy;
  }
  // Send doubles as stop rather than greying out. Before this there was no
  // way at all to end a running reply (see chatStream.js's note that the
  // request was never actually cancelled), so a long or wrong answer had to
  // be waited out.
  const send = document.getElementById('chat-send');
  if (send) {
    send.disabled = false;
    send.dataset.mode = busy ? 'stop' : 'send';
    send.title = busy ? 'Stop' : 'Send';
    send.setAttribute('aria-label', busy ? 'Stop response' : 'Send message');
    send.replaceChildren();
    send.insertAdjacentHTML('beforeend', busy ? ICON_STOP : ICON_SEND);
  }
  if (busy) document.querySelectorAll('.model-picker-menu').forEach(m => m.classList.add('hidden'));
}

// Fetched once per page load and shared by every chat — the catalog is
// per-provider, not per-session, and re-fetching it on each session switch
// would re-read Codex's ~220KB cache for no new information. A failure
// caches an empty list rather than retrying in a loop: the custom model-ID
// field stays available, so the picker degrades instead of breaking.
let modelCatalogPromise = null;
async function loadModelCatalog(kind) {
  if (!modelCatalogPromise) modelCatalogPromise = api('/api/models/catalog').catch(() => ({}));
  const catalog = await modelCatalogPromise;
  return (catalog && catalog[kind]) || [];
}

async function refreshModelPicker(currentEndpointId, modelOverride = null, modelEffort = null) {
  const label = document.getElementById("model-picker-label");
  const menu = document.getElementById("model-picker-menu");
  if (!label || !menu) return;
  const sessionId = activeSessionId;
  const endpoints = await api("/api/models").catch(() => []);
  if (sessionId !== activeSessionId || !menu.isConnected) return;
  menu.innerHTML = "";

  const options = endpoints.map((ep) => ({
    id: ep.id,
    name: ep.kind === "claude_cli" || ep.kind === "codex_cli" ? `${ep.name} (${ep.model || "CLI default"})` : `${ep.name} (${ep.model})`,
  }));
  if (options.length === 0) {
    menu.appendChild(el("div", { class: "model-picker-item", text: "No models added yet — see Settings" }));
  }
  for (const opt of options) {
    const item = el("button", {
      type: 'button',
      class: "model-picker-item" + (opt.id === (currentEndpointId || "") ? " active" : ""),
      text: opt.name,
      onclick: async () => {
        if (chatStream.getInFlight(activeSessionId)?.status === 'processing') return;
        closeMenu(menu);
        // Landing on Chats without opening a chat leaves activeSessionId null,
        // and this used to `return` — so the dropdown listed every model and
        // silently ignored every click (David, 2026-09-04). Create the session
        // the same way sendMessage() lazily does, then apply the choice.
        if (!activeSessionId) await createSession({ preserveAttachments: true });
        if (!activeSessionId) return; // creation genuinely failed
        const target = activeSessionId;
        try {
          await api(`/api/sessions/${target}/model`, { method: "POST", body: JSON.stringify({ model_endpoint_id: opt.id }) });
          if (activeSessionId === target) await refreshModelPicker(opt.id);
        } catch (_) { /* api() displays the mutation error */ }
      },
    });
    menu.appendChild(item);
  }
  const active = options.find((o) => o.id === currentEndpointId);
  label.textContent = active ? active.name : options.length ? 'Choose model' : NO_MODEL_LABEL;
  label.parentElement.title = label.textContent;
  const ep = endpoints.find(e => e.id === currentEndpointId);
  const versionBtn = document.getElementById('model-version-btn');
  const versionMenu = document.getElementById('model-version-menu');
  if (!versionBtn || !versionMenu) return;
  const cli = ep && ['claude_cli', 'codex_cli'].includes(ep.kind);
  versionBtn.hidden = !cli;
  versionMenu.replaceChildren();
  if (cli) {
    label.textContent = ep.name;
    // The model this chat will actually use: the session's own override
    // when set, otherwise whatever the endpoint is configured with.
    const effectiveModel = (modelOverride === null ? ep.model : modelOverride) || '';
    const catalog = await loadModelCatalog(ep.kind);
    if (sessionId !== activeSessionId || !versionMenu.isConnected) return;
    const known = catalog.find(m => m.id === effectiveModel || m.alias === effectiveModel) || null;
    versionBtn.textContent = (known ? known.display_name : effectiveModel) || 'CLI default';
    if (modelEffort) versionBtn.textContent += ` · ${modelEffort}`;
    versionBtn.title = 'Model and reasoning level for this chat';

    const save = async (value, effort = null) => {
      if (activeSessionId !== sessionId || chatStream.getInFlight(sessionId)?.status === 'processing') return;
      try {
        const result = await api(`/api/sessions/${sessionId}/model`, {
          method: 'POST',
          body: JSON.stringify({ model_endpoint_id: ep.id, model_override: value, effort }),
        });
        if (activeSessionId === sessionId) {
          closeMenu(versionMenu);
          await refreshModelPicker(ep.id, result.model_override, result.effort ?? null);
          toast('Model updated for this chat', 'success');
        }
      } catch (_) { /* keep the editor open on error — api() shows the reason */ }
    };

    // -- reasoning level. Only rendered for a model whose supported levels
    // are actually known; a custom/unlisted ID gets no effort row rather
    // than a guessed one, since the server would reject an unadvertised
    // value anyway (core/model_catalog.py's validate_effort).
    const effortRow = el('div', { class: 'model-effort-row' });
    if (known && known.supported_efforts.length) {
      effortRow.append(el('span', { class: 'model-effort-label', text: 'Reasoning' }));
      const levels = [{ effort: null, description: known.default_effort
        ? `Provider default (${known.default_effort})` : 'Provider default' }, ...known.supported_efforts];
      for (const level of levels) {
        effortRow.appendChild(el('button', {
          type: 'button',
          class: 'model-effort-btn' + ((modelEffort ?? null) === level.effort ? ' active' : ''),
          text: level.effort || 'Default',
          title: level.description || '',
          onclick: () => save(modelOverride, level.effort),
        }));
      }
    }

    // -- searchable model list, filtered live. Falls back silently to the
    // custom-ID field below when the provider offers no catalog on this
    // machine, which is the exact behaviour this picker had before.
    const listWrap = el('div', { class: 'model-catalog-list' });
    const renderList = (query) => {
      listWrap.replaceChildren();
      const q = query.trim().toLowerCase();
      const matches = catalog.filter(m => !q
        || m.display_name.toLowerCase().includes(q)
        || m.id.toLowerCase().includes(q)
        || (m.description || '').toLowerCase().includes(q));
      if (!matches.length) {
        listWrap.appendChild(el('p', { class: 'muted', text: catalog.length ? 'No models match that search.' : 'No model list available for this CLI — enter an exact ID below.' }));
        return;
      }
      for (const m of matches) {
        const row = el('button', {
          type: 'button',
          class: 'model-picker-item model-catalog-item' + (known && m.id === known.id ? ' active' : ''),
          // Switching model drops the effort: levels are per-model (verified
          // against Codex's own catalog — GPT-5.5 advertises no "ultra"
          // while Astra does), so carrying one across would send a value the
          // new model may not support.
          onclick: () => save(m.id, null),
        }, [
          el('span', { class: 'model-catalog-name', text: m.display_name }),
          el('span', { class: 'model-catalog-desc', text: m.description || m.id }),
        ]);
        listWrap.appendChild(row);
      }
    };
    const search = el('input', {
      id: 'chat-model-search', type: 'search', placeholder: 'Search models',
      autocomplete: 'off', spellcheck: 'false', 'aria-label': 'Search models',
      oninput: e => renderList(e.target.value),
    });
    renderList('');

    // -- custom ID, kept as a secondary escape hatch exactly as before: the
    // catalog is what the CLI advertises locally, which is not a promise
    // about what a given account can reach.
    const input = el('input', { id: 'chat-model-id', type: 'text', maxlength: '160', value: modelOverride ?? ep.model ?? '', placeholder: 'Exact model ID', autocomplete: 'off', spellcheck: 'false' });
    const form = el('form', { class: 'model-version-form', onsubmit: e => { e.preventDefault(); save(input.value.trim()); } }, [
      el('label', { for: 'chat-model-id', text: 'Custom model ID' }), input,
      el('p', { class: 'muted', text: 'For a model your CLI account can reach that is not listed above. Applies to this chat only.' }),
      el('button', { type: 'submit', class: 'btn', text: 'Apply model' }),
    ]);

    versionMenu.append(
      el('div', { class: 'model-version-search' }, [search]),
      listWrap,
      ...(effortRow.childElementCount ? [effortRow] : []),
      el('details', { class: 'model-custom-details' }, [el('summary', { text: 'Custom model ID' }), form]),
      el('button', { type: 'button', class: 'model-picker-item', text: 'Use CLI default', onclick: () => save('') }),
      el('button', { type: 'button', class: 'model-picker-item', text: 'Use endpoint setting', onclick: () => save(null) }));
  }
  syncChatBusy(chatStream.getInFlight(activeSessionId)?.status === 'processing');
}

// -- projects (David's ask 2026-09-12, "similar to how Claude and ChatGPT
// have projects") — a filter above the chat list rather than a per-session
// setting like the model picker above: picking one narrows sessionsList to
// that project and any chat created while it's active joins automatically
// (see createSession()). Independent of model choice entirely — a project's
// chats can each be pinned to whatever model they want, same as any other
// chat; the project only ever affects what gets appended to the landing-
// zone prompt (core/projects.py's project_addendum()).
async function refreshProjectPicker(sessionsList, messages) {
  const label = document.getElementById("project-picker-label");
  const menu = document.getElementById("project-picker-menu");
  const gearBtn = document.getElementById("project-settings-btn");
  if (!label || !menu) return;
  const allProjects = await api("/api/projects").catch(() => []);
  if (!sessionsList.isConnected) return;
  menu.innerHTML = "";

  menu.appendChild(el("div", {
    class: "overflow-menu-item" + (activeProjectFilter === null ? " active" : ""),
    text: "All Chats",
    onclick: () => {
      closeMenu(menu);
      activeProjectFilter = null;
      label.textContent = "All Chats";
      if (gearBtn) gearBtn.style.display = "none";
      refreshSessions(sessionsList, messages);
    },
  }));
  for (const project of allProjects) {
    const item = el("div", { class: "overflow-menu-item" + (activeProjectFilter === project.id ? " active" : "") });
    item.insertAdjacentHTML("afterbegin", ICON_FOLDER);
    item.appendChild(el("span", { text: project.name }));
    item.addEventListener("click", () => {
      closeMenu(menu);
      activeProjectFilter = project.id;
      label.textContent = project.name;
      if (gearBtn) gearBtn.style.display = "";
      refreshSessions(sessionsList, messages);
    });
    menu.appendChild(item);
  }
  menu.appendChild(el("div", { class: "overflow-menu-divider" }));
  menu.appendChild(menuItem(ICON_PLUS, "New Project", () => { closeMenu(menu); openProjectModal(null); }));

  const active = allProjects.find((p) => p.id === activeProjectFilter);
  label.textContent = active ? active.name : "All Chats";
  if (gearBtn) gearBtn.style.display = active ? "" : "none";
}

// -- project settings modal (create/edit/delete + Library document picker,
// matching getWorkspaceModal/getIntegrationsModal's shape above) ----------
let projectModal = null;

function getProjectModal() {
  if (projectModal) return projectModal;
  const nameInput = el("input", { class: "workspace-path-input", placeholder: "Project name" });
  const instructionsInput = el("textarea", { class: "workspace-path-input", rows: "3", placeholder: "Custom instructions every chat in this project should follow (optional)" });
  const docsBody = el("div", { class: "workspace-body" });
  const deleteBtn = el("button", { class: "btn danger", text: "Delete Project" });
  const saveBtn = el("button", { class: "btn", text: "Save" });
  const closeBtn = el("button", { class: "modal-close-btn" });
  closeBtn.insertAdjacentHTML("beforeend", ICON_X);

  const panel = el("div", { class: "glass modal-panel" }, [
    el("h4", { text: "Project" }, [closeBtn]),
    nameInput,
    instructionsInput,
    el("div", { class: "muted", style: "margin-top:8px;", text: "Knowledge — Library documents every chat in this project can read on demand:" }),
    docsBody,
    el("div", { class: "modal-footer" }, [deleteBtn, saveBtn]),
  ]);
  const backdrop = el("div", { class: "modal-backdrop hidden" }, [panel]);
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) closeProjectModal(); });
  panel.addEventListener("click", (e) => e.stopPropagation());
  document.body.appendChild(backdrop);
  closeBtn.addEventListener("click", closeProjectModal);

  projectModal = { backdrop, nameInput, instructionsInput, docsBody, deleteBtn, saveBtn };
  return projectModal;
}

function closeProjectModal() {
  if (projectModal) projectModal.backdrop.classList.add("hidden");
}

// project === null means "create" — everything else here handles both
// modes with the same panel rather than a separate creation form.
async function openProjectModal(project) {
  const modal = getProjectModal();
  modal.backdrop.classList.remove("hidden");
  modal.nameInput.value = project ? project.name : "";
  modal.instructionsInput.value = project ? project.instructions : "";
  modal.deleteBtn.style.display = project ? "" : "none";

  const [allDocs] = await Promise.all([api("/api/documents").catch(() => [])]);
  modal.docsBody.innerHTML = "";
  const checks = {};
  if (allDocs.length === 0) {
    modal.docsBody.appendChild(el("div", { class: "workspace-empty", text: "No documents yet — add some in the Library tab." }));
  }
  for (const doc of allDocs) {
    const cb = el("input", { type: "checkbox" });
    cb.checked = !!(project && project.document_ids.includes(doc.id));
    checks[doc.id] = cb;
    modal.docsBody.appendChild(el("label", { class: "workspace-row", style: "cursor:pointer;" }, [cb, el("span", { text: doc.title })]));
  }

  modal.saveBtn.onclick = async () => {
    const name = modal.nameInput.value.trim();
    if (!name) { toast("Project name is required", "error"); return; }
    const instructions = modal.instructionsInput.value.trim();
    const saved = project
      ? await api(`/api/projects/${project.id}`, { method: "PATCH", body: JSON.stringify({ name, instructions }) })
      : await api("/api/projects", { method: "POST", body: JSON.stringify({ name, instructions }) });

    // Reconcile document membership against whatever was checked — cheap
    // enough to just diff against the saved state rather than track dirty
    // checkboxes individually.
    const wantedIds = Object.entries(checks).filter(([, cb]) => cb.checked).map(([id]) => id);
    const hadIds = project ? project.document_ids : [];
    await Promise.all([
      ...wantedIds.filter((id) => !hadIds.includes(id)).map((id) => api(`/api/projects/${saved.id}/documents/${id}`, { method: "POST" })),
      ...hadIds.filter((id) => !wantedIds.includes(id)).map((id) => api(`/api/projects/${saved.id}/documents/${id}`, { method: "DELETE" })),
    ]);

    closeProjectModal();
    const sessionsList = document.getElementById("sessions-list");
    const messages = document.getElementById("chat-messages");
    if (sessionsList && messages) {
      if (!project) { activeProjectFilter = saved.id; } // land inside the project you just created, matching Claude/ChatGPT
      await refreshProjectPicker(sessionsList, messages);
      await refreshSessions(sessionsList, messages);
    }
  };

  modal.deleteBtn.onclick = async () => {
    if (!project) return;
    const ok = await confirmDialog({
      title: "Delete this project?",
      message: `"${project.name}" will be deleted. Chats currently in it are not deleted — they just become unassigned.`,
      confirmLabel: "Delete project",
    });
    if (!ok) return;
    await api(`/api/projects/${project.id}`, { method: "DELETE" });
    closeProjectModal();
    activeProjectFilter = null;
    const sessionsList = document.getElementById("sessions-list");
    const messages = document.getElementById("chat-messages");
    if (sessionsList && messages) {
      await refreshProjectPicker(sessionsList, messages);
      await refreshSessions(sessionsList, messages);
    }
  };
}

async function refreshSessions(sessionsList, messages) {
  const allSessions = await api("/api/sessions");
  if (!sessionsList.isConnected) return;
  // Projects (David's ask 2026-09-12) — narrows the list to whatever
  // project is currently selected in the picker above; null (the default,
  // "All Chats") shows everything, unchanged from before this feature.
  const sessions = activeProjectFilter === null ? allSessions : allSessions.filter((s) => s.project_id === activeProjectFilter);
  sessionsList.innerHTML = "";
  // Deliberately no early return on an empty list: the "no active session"
  // block at the bottom is what populates the model picker, and returning here
  // meant a brand-new install opened the models dropdown to nothing at all
  // (David, 2026-09-04). The loop below is simply a no-op when there are none.
  if (sessions.length === 0) {
    sessionsList.appendChild(el("div", { class: "empty-state", text: activeProjectFilter === null ? "No chats yet" : "No chats in this project yet" }));
  }
  for (const session of sessions) {
    const item = el("button", {
      type: "button",
      class: "session-item" + (session.id === activeSessionId ? " active" : ""),
      "data-session-id": session.id,
      "data-title": session.title,
      onclick: () => openSession(session.id, sessionsList, messages),
      oncontextmenu: (e) => {
        e.preventDefault();
        showSessionMenu(e.clientX, e.clientY, session, item, sessionsList, messages);
      },
    });
    if (session.starred) item.appendChild(el("span", { class: "star-mark", text: "★ " }));
    item.appendChild(document.createTextNode(session.title));
    item.title = session.title;
    sessionsList.appendChild(item);
    if (session.id === activeSessionId) syncChatLayout(messages, session.title);
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
  const moveItem = el("div", {
    class: "context-menu-item",
    text: "Move to Project",
    onclick: async (e) => {
      e.stopPropagation();
      const allProjects = await api("/api/projects").catch(() => []);
      menu.innerHTML = "";
      menu.appendChild(el("div", {
        class: "context-menu-item",
        text: "No Project",
        onclick: async () => {
          closeSessionMenu();
          await api(`/api/sessions/${session.id}/project`, { method: "POST", body: JSON.stringify({ project_id: null }) });
          await refreshSessions(sessionsList, messages);
        },
      }));
      if (allProjects.length === 0) {
        menu.appendChild(el("div", { class: "context-menu-item", text: "(no projects yet)" }));
      }
      for (const project of allProjects) {
        menu.appendChild(el("div", {
          class: "context-menu-item" + (session.project_id === project.id ? " active" : ""),
          text: project.name,
          onclick: async () => {
            closeSessionMenu();
            await api(`/api/sessions/${session.id}/project`, { method: "POST", body: JSON.stringify({ project_id: project.id }) });
            await refreshSessions(sessionsList, messages);
          },
        }));
      }
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

  menu.append(renameItem, starItem, moveItem, deleteItem);
  document.body.appendChild(menu);
  openMenu = menu;
  // Deferred so the click that opened the menu doesn't immediately close it.
  setTimeout(() => document.addEventListener("click", closeSessionMenu), 0);
}

function startRename(item, session, sessionsList, messages) {
  const input = el("input", { class: "session-rename-input", value: session.title, "aria-label": "Conversation title" });
  item.replaceWith(input);
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

async function createSession({ preserveAttachments = false } = {}) {
  const draftFiles = preserveAttachments ? stagedAttachments : [];
  const session = await api("/api/sessions", { method: "POST", body: JSON.stringify({}) });
  activeSessionId = session.id;
  // A chat created while a project is selected joins it automatically
  // (David's ask 2026-09-12, matching Claude/ChatGPT's "new chat inside
  // this project" behavior) rather than landing unassigned and needing a
  // separate move-to-project step every time.
  if (activeProjectFilter) {
    await api(`/api/sessions/${session.id}/project`, { method: "POST", body: JSON.stringify({ project_id: activeProjectFilter }) });
  }
  const sessionsList = document.getElementById("sessions-list");
  const messages = document.getElementById("chat-messages");
  await refreshSessions(sessionsList, messages);
  await openSession(session.id, sessionsList, messages);
  if (preserveAttachments && activeSessionId === session.id && messages.isConnected) {
    stagedAttachments = draftFiles;
    renderAttachStrip(document.getElementById('attach-strip'));
  }
}

async function openSession(sessionId, sessionsList, messages) {
  closeArtifact();
  // Disposed on session switch, not carried across: the pane belongs to the
  // conversation it was opened from, and in the desktop app leaving it
  // running would keep a native view (and its scripts and audio) alive over
  // a chat that never asked for it.
  closeBrowser();
  activeUnsubscribers.forEach(unsub => unsub());
  activeUnsubscribers.length = 0;
  activeSessionId = sessionId;
  stagedAttachments = [];
  const attachStrip = document.getElementById("attach-strip");
  if (attachStrip) attachStrip.innerHTML = "";
  [...sessionsList.children].forEach((c) => c.classList.remove("active"));
  const session = await api(`/api/sessions/${sessionId}`);
  if (!messages.isConnected || activeSessionId !== sessionId) return;
  messages.innerHTML = "";
  for (const msg of session.messages) {
    messages.appendChild(messageCard(msg.role, msg.content, msg.ts, msg.status));
  }
  // Reattach to a turn still generating (David's ask 2026-09-12) — its
  // reply isn't in session.messages yet (the backend only persists it once
  // the full turn completes), so it renders as one extra live card on top
  // of the real history rather than something openSession would otherwise
  // know about.
  if (chatStream.getInFlight(sessionId)?.status === 'processing') {
    const replyCard = messageCard("assistant", "");
    const replyBody = replyCard.querySelector(".msg-body");
    messages.appendChild(replyCard);
    activeUnsubscribers.push(attachToInFlight(sessionId, messages, replyCard, replyBody, null));
  }
  syncChatLayout(messages, session.title);
  messages.scrollTop = messages.scrollHeight;
  await refreshModelPicker(session.model_endpoint_id, session.model_override ?? null, session.model_effort ?? null);
  if (!messages.isConnected || activeSessionId !== sessionId) return;
  syncWorkspacePill(session.workspace_dir);
  // Read from the session record, so the meter is correct straight after a
  // page reload or a switch between chats — not only after a fresh turn.
  refreshContextMeter(sessionId);
  await refreshSessions(sessionsList, messages);
}

// Overlay's click-to-jump entry point (David's ask 2026-09-12) — app.js's
// switchTab("chat") has already run and rebuilt this view's DOM by the
// time floatingProgress.js calls this, so the ids below are guaranteed
// fresh, same assumption createSession() above already makes.
export function openSessionById(sessionId) {
  const sessionsList = document.getElementById("sessions-list");
  const messages = document.getElementById("chat-messages");
  if (sessionsList && messages) return openSession(sessionId, sessionsList, messages);
}

async function sendMessage(messages, input, sendBtn, attachStrip) {
  if (sendBtn.disabled || chatStream.getInFlight(activeSessionId)?.status === 'processing') return;
  const text = input.value.trim();
  if (!text && stagedAttachments.length === 0) return;

  if (text.startsWith("/")) {
    input.value = "";
    input.style.height = "auto";
    messages.appendChild(messageCard("user", text));
    syncChatLayout(messages);
    const sessionsList = document.getElementById("sessions-list");
    const { output } = await runSlashCommand(text, {
      sessionId: () => activeSessionId,
      createSession,
      refreshSessions: () => refreshSessions(sessionsList, messages),
      onCurrentSessionDeleted: () => { activeSessionId = null; messages.innerHTML = ""; },
      onWorkspaceCleared: () => syncWorkspacePill(null),
    });
    messages.appendChild(messageCard("assistant", output));
    syncChatLayout(messages);
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

  // Lazy session creation opens a fresh session and clears staged UI files.
  // Capture IDs first so the first message keeps its attachments.
  const attachmentIds = stagedAttachments.map((a) => a.id);
  if (!activeSessionId) {
    sendBtn.disabled = true;
    try { await createSession({ preserveAttachments: true }); }
    catch (error) { sendBtn.disabled = false; toast(`Couldn't start chat: ${error.message}`, 'error'); return; }
    sendBtn.disabled = false;
    if (!messages.isConnected) return;
  }
  stagedAttachments = [];
  renderAttachStrip(attachStrip);

  input.value = "";
  input.style.height = "auto";
  messages.appendChild(messageCard("user", text || "(attachment)"));
  const replyCard = messageCard("assistant", "");
  const replyBody = replyCard.querySelector(".msg-body");
  messages.appendChild(replyCard);
  syncChatLayout(messages);
  messages.scrollTop = messages.scrollHeight;

  // The actual request now lives in chatStream.js, one level above this
  // view (David's ask 2026-09-12 — see its module docstring): it survives
  // switching tabs away from Chat entirely, and the floating overlay
  // (floatingProgress.js) picks it up independently of whatever's on
  // screen here. attachToInFlight just wires this specific card's DOM up
  // to that shared state — same rendering whether a turn was just started
  // or is being reattached to on reopen.
  const sessionItem = document.querySelector(`.session-item[data-session-id="${activeSessionId}"]`);
  const sessionTitle = sessionItem?.dataset.title || "Chat";
  chatStream.startTurn(activeSessionId, sessionTitle, text, attachmentIds);
  activeUnsubscribers.push(attachToInFlight(activeSessionId, messages, replyCard, replyBody, sendBtn));

  input.focus();
}
