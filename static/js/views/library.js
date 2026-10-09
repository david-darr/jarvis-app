import { api, el, toast, confirmDialog, emptyState } from "../api.js";
import { ICONS } from "../icons.js";
import { createVaultExplorer } from "../vaultExplorer.js";
import { createVaultGraph } from "../vaultGraph.js";
import { renderGoogleWorkspace } from "../googleWorkspace.js";

// Library tab (Phase 7, David's ask 2026-09-01 "let's move on to the next
// stage") — real document storage + bounded keyword search, deliberately
// scoped far lighter than Odysseus's own documents/RAG system (Chroma
// vector DB, embeddings, PDF/Office extraction, versioning). See
// services/documents_service.py's module docstring for the full reasoning.

let activeSection = "documents";
let activeVaultView = null;

export async function render(container, tabId, options = {}) {
  if (["vault", "documents", "google"].includes(options.section)) activeSection = options.section;
  if (activeSection === "vault") renderVault(container);
  else if (activeSection === "google") renderGoogle(container);
  else await renderList(container);
  return () => disposeVault();
}

function disposeVault() {
  activeVaultView?.destroy();
  activeVaultView = null;
}

function libraryChrome(container, current) {
  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Library" }),
      el("div", { class: "sub", text: current === "vault"
        ? "Browse and read the notes in your connected Vault."
        : current === "google" ? "Your connected Google Drive, Sheets, and Forms."
        : "A home for your references, drafts, and ideas worth keeping." }),
    ]),
  ]);
  const tabs = el("div", { class: "segmented-tabs library-tabs", role: "group", "aria-label": "Library section" });
  for (const [id, label] of [["documents", "Documents & files"], ["vault", "Vault"], ["google", "Google Drive"]]) {
    const button = el("button", { type: "button", class: "segmented-tab" + (id === current ? " active" : ""),
      text: label, "aria-pressed": id === current ? "true" : "false" });
    button.addEventListener("click", async () => {
      if (id === current) return;
      if (activeVaultView?.canLeave && !await activeVaultView.canLeave()) return;
      if (id === "vault") renderVault(container);
      else if (id === "google") renderGoogle(container);
      else await renderList(container);
    });
    tabs.append(button);
  }
  return [header, tabs];
}

function renderGoogle(container) {
  disposeVault();
  activeSection = "google";
  const [header, tabs] = libraryChrome(container, "google");
  renderGoogleWorkspace(container, header, tabs);
}

function renderVault(container) {
  disposeVault();
  activeSection = "vault";
  container.replaceChildren();
  const [header, tabs] = libraryChrome(container, "vault");
  const modes = el("div", { class: "segmented-tabs library-vault-modes", role: "group", "aria-label": "Vault view" });
  const host = el("div", { class: "library-vault-host" });
  container.append(el("div", { class: "library-vault-page" }, [
    header, tabs,
    el("div", { class: "library-vault-intro" }, [
      el("div", {}, [el("h3", { text: "Vault" }), el("p", { text: "Your Markdown notes, organized by folder. Open Map to explore their connections." })]),
      modes,
    ]),
    host,
  ]));
  let mode = "browse";
  const drawModes = () => {
    modes.replaceChildren();
    for (const [id, label] of [["browse", "Browse"], ["map", "Map"]]) {
      const button = el("button", { type: "button", class: "segmented-tab" + (mode === id ? " active" : ""),
        text: label, "aria-pressed": mode === id ? "true" : "false" });
      button.addEventListener("click", async () => {
        if (mode === id) return;
        if (activeVaultView?.canLeave && !await activeVaultView.canLeave()) return;
        disposeVault();
        mode = id;
        drawModes();
        host.replaceChildren();
        activeVaultView = mode === "browse" ? createVaultExplorer(host) : createVaultGraph(host);
      });
      modes.append(button);
    }
  };
  drawModes();
  activeVaultView = createVaultExplorer(host);
}

function section(title, description, icon, body) {
  const glyph = el("span", { class: "library-section-icon", "aria-hidden": "true" });
  glyph.innerHTML = ICONS[icon];
  const count = el("span", { class: "library-count", text: "0" });
  const summary = el("summary", { class: "library-section-summary" }, [
    glyph,
    el("span", { class: "library-section-copy" }, [
      el("strong", { text: title }), el("small", { text: description }),
    ]),
    count,
    el("span", { class: "library-chevron", "aria-hidden": "true", text: "›" }),
  ]);
  return { node: el("details", { class: "library-section", open: true }, [summary, body]), count };
}

function fileSize(size) {
  if (size == null) return "";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

async function renderList(container) {
  disposeVault();
  activeSection = "documents";
  container.innerHTML = "";
  const [header, tabs] = libraryChrome(container, "documents");

  const searchInput = el("input", { placeholder: "Search documents and chat files...", style: "flex:1;" });
  const newBtn = el("button", { class: "btn primary", text: "+ New document" });
  // Plain file input + FileReader (David's ask 2026-09-01) — works
  // identically in the Electron shell and the plain-HTTP web-access path,
  // so import is no longer gated behind window.jarvis (see skillManager.js's
  // matching change and the real "null" bug it fixed there).
  const fileInput = el("input", { type: "file", accept: ".md,.txt", style: "display:none;" });
  const importBtn = el("button", { class: "btn", text: "Import from File...", onclick: () => fileInput.click() });
  const importStatus = el("span", { class: "meta" });

  const toolbar = el("div", { class: "glass card" }, [
    el("div", { class: "card-row", style: "gap:8px;" }, [searchInput, newBtn, importBtn, fileInput]),
    el("div", { style: "margin-top:6px;" }, [importStatus]),
  ]);

  const grid = el("div", { id: "library-grid", class: "document-grid" });
  const chatFiles = el("div", { class: "library-chat-files" });
  const documentsSection = section("Documents", "Notes and drafts saved to your Library", "notes", grid);
  const filesSection = section("Files by chat", "Attachments and files created in conversations", "chats", chatFiles);
  const openChats = new Set();

  const wrap = el("div", { class: "view-constrained library-view" }, [header, tabs, toolbar, documentsSection.node, filesSection.node]);
  container.append(wrap);

  newBtn.addEventListener("click", async () => {
    const doc = await api("/api/documents", { method: "POST", body: JSON.stringify({ title: "Untitled", content: "" }) });
    await renderEditor(container, doc.id);
  });

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    const content = await file.text();
    importStatus.textContent = `Importing ${file.name}…`;
    try {
      const doc = await api("/api/documents/import", { method: "POST", body: JSON.stringify({ filename: file.name, content }) });
      importStatus.textContent = "";
      await renderEditor(container, doc.id);
      toast(`Imported ${file.name}`, "success");
    } catch (e) {
      importStatus.textContent = "";
      toast(`Import failed: ${e.message}`, "error");
    }
  });

  let searchDebounce = null;
  let searchVersion = 0;
  const refresh = async (query) => {
    const version = ++searchVersion;
    const current = () => version === searchVersion && wrap.isConnected;
    await Promise.all([
      refreshGrid(grid, container, query, documentsSection, current),
      refreshChatFileGroups(chatFiles, query, filesSection, openChats, current),
    ]);
  };
  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => refresh(searchInput.value.trim()), 200);
  });

  await refresh("");
}

async function refreshChatFileGroups(host, query, section, openChats, current) {
  const groups = await api("/api/chat/files/library");
  if (!current()) return;
  host.replaceChildren();
  const wanted = query.toLowerCase();
  let shown = 0;
  for (const group of groups) {
    const files = group.files.filter(file => !wanted
      || group.title.toLowerCase().includes(wanted) || file.name.toLowerCase().includes(wanted));
    if (!files.length) continue;
    shown += files.length;
    const list = el("div", { class: "library-file-list" });
    for (const file of files) {
      const label = file.origin === "attachment" ? "Sent to chat"
        : file.origin === "created" ? "Created by model" : "Generated";
      const kind = file.name.includes(".") ? file.name.split(".").pop().slice(0, 5).toUpperCase() : "FILE";
      const card = el("button", { type: "button", class: "library-file-row document-card", title: file.name,
        disabled: !file.exists,
        onclick: async () => {
          const { switchTab } = await import("../app.js");
          await switchTab("chat", { sessionId: group.session_id });
          const { openArtifact } = await import("../chatContent.js");
          await openArtifact(group.session_id, file.url, file.name, null);
        } }, [
        el("span", { class: "library-file-kind", "aria-hidden": "true", text: kind }),
        el("span", { class: "library-file-copy" }, [
          el("strong", { text: file.name }),
          el("small", { text: file.exists ? [label, fileSize(file.size)].filter(Boolean).join(" · ") : `${label} · No longer available` }),
        ]),
        el("span", { class: "library-row-arrow", "aria-hidden": "true", text: "→" }),
      ]);
      list.append(card);
    }
    const chat = el("details", { class: "library-chat-group", open: !!wanted || openChats.has(group.session_id) }, [
      el("summary", { class: "library-chat-summary", title: group.title }, [
        el("span", { class: "library-chat-title library-chat-heading", text: group.title }),
        el("span", { class: "library-count", text: String(files.length) }),
        el("span", { class: "library-chevron", "aria-hidden": "true", text: "›" }),
      ]),
      list,
    ]);
    chat.addEventListener("toggle", () => {
      if (wanted) return;
      if (chat.open) openChats.add(group.session_id);
      else openChats.delete(group.session_id);
    });
    host.append(chat);
  }
  section.count.textContent = String(shown);
  if (wanted && shown) section.node.open = true;
  if (!shown) host.append(el("div", { class: "library-empty", text: query ? "No chat files match this search." : "Files sent to or created in chats will appear here." }));
}

async function refreshGrid(grid, container, query, section, current) {
  const results = query
    ? await api(`/api/documents/search?q=${encodeURIComponent(query)}`)
    : (await api("/api/documents")).map((d) => ({ ...d, snippet: null }));
  if (!current()) return;
  grid.replaceChildren();
  section.count.textContent = String(results.length);
  if (query && results.length) section.node.open = true;

  if (results.length === 0) {
    // Grid is a CSS grid — span the empty state across all columns so it
    // centers on the page rather than sitting in the first cell.
    const empty = query
      ? emptyState({ icon: ICONS.search, title: "No documents match", hint: `No documents match "${query}". Chat files are listed below.` })
      : emptyState({
          icon: ICONS.library,
          title: "No documents yet",
          hint: "Keep reference material here — notes, specs, drafts. You can pull any document straight into a chat from the composer's + menu.",
          actionLabel: "Create a document",
          onAction: async () => {
            const doc = await api("/api/documents", { method: "POST", body: JSON.stringify({ title: "Untitled", content: "" }) });
            await renderEditor(container, doc.id);
          },
        });
    empty.style.gridColumn = "1 / -1";
    grid.appendChild(empty);
    return;
  }

  for (const item of results) {
    const meta = query
      ? (item.snippet || "Title match")
      : `Updated ${new Date(item.updated_at * 1000).toLocaleDateString()}`;
    const card = el("button", {
      type: "button", class: "library-document-card document-card", title: item.title,
      onclick: () => renderEditor(container, item.id),
    }, [
      el("span", { class: "library-document-icon", "aria-hidden": "true", text: "▤" }),
      el("span", { class: "library-document-copy" }, [
        el("strong", { text: item.title }),
        el("small", { text: meta }),
      ]),
      el("span", { class: "library-row-arrow", "aria-hidden": "true", text: "→" }),
    ]);
    grid.appendChild(card);
  }
}

async function renderEditor(container, docId) {
  const doc = await api(`/api/documents/${docId}`);
  container.innerHTML = "";

  const backBtn = el("button", { class: "btn", text: "← Back to Library" });
  const titleInput = el("input", { style: "flex:1;font-size:15px;", value: doc.title });
  const saveStatus = el("span", { class: "meta" });
  const saveBtn = el("button", { class: "btn", text: "Save" });
  const delBtn = el("button", { class: "btn danger", text: "Delete" });

  const header = el("div", { class: "view-header" }, [
    el("div", { class: "card-row", style: "flex:1;gap:10px;" }, [backBtn, titleInput]),
    el("div", { class: "card-row", style: "gap:8px;" }, [saveStatus, saveBtn, delBtn]),
  ]);

  const contentArea = el("textarea", {
    style: "flex:1;width:100%;min-height:340px;resize:none;font-family:inherit;font-size:13.5px;line-height:1.6;margin-top:14px;",
    text: doc.content,
  });
  contentArea.value = doc.content; // el() sets textContent via "text", but a <textarea>'s live value needs .value too

  container.append(el("div", { class: "view-constrained library-editor" }, [header, contentArea]));

  // Unsaved-changes guard (audit 2026-09-03) — leaving the editor used to
  // silently discard everything typed since the last save.
  const isDirty = () => titleInput.value !== doc.title || contentArea.value !== doc.content;
  const markSaved = () => { doc.title = titleInput.value; doc.content = contentArea.value; };

  backBtn.addEventListener("click", async () => {
    if (isDirty()) {
      const ok = await confirmDialog({
        title: "Discard unsaved changes?",
        message: `"${titleInput.value.trim() || "Untitled"}" has changes you haven't saved yet.`,
        confirmLabel: "Discard changes",
      });
      if (!ok) return;
    }
    renderList(container);
  });

  const save = async () => {
    saveBtn.disabled = true;
    saveStatus.textContent = "Saving…";
    try {
      await api(`/api/documents/${docId}`, {
        method: "PATCH",
        body: JSON.stringify({ title: titleInput.value.trim() || "Untitled", content: contentArea.value }),
      });
      markSaved();
      // Was a permanent "Saved." string that never cleared — now a toast
      // plus a status line that fades on the next edit.
      saveStatus.textContent = "All changes saved";
      toast("Document saved", "success");
    } finally {
      saveBtn.disabled = false;
    }
  };
  saveBtn.addEventListener("click", save);

  // Ctrl+S / Cmd+S saves, matching every real editor.
  contentArea.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); save(); }
  });
  const clearStatus = () => { if (saveStatus.textContent) saveStatus.textContent = ""; };
  contentArea.addEventListener("input", clearStatus);
  titleInput.addEventListener("input", clearStatus);

  delBtn.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Delete this document?",
      message: `"${doc.title || "Untitled"}" will be permanently deleted. This can't be undone.`,
      confirmLabel: "Delete document",
    });
    if (!ok) return;
    await api(`/api/documents/${docId}`, { method: "DELETE" });
    await renderList(container);
    toast("Document deleted", "success");
  });
}
