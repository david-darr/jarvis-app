import { api, el, toast, confirmDialog, emptyState } from "../api.js";
import { ICONS } from "../icons.js";

// Library tab (Phase 7, David's ask 2026-09-01 "let's move on to the next
// stage") — real document storage + bounded keyword search, deliberately
// scoped far lighter than Odysseus's own documents/RAG system (Chroma
// vector DB, embeddings, PDF/Office extraction, versioning). See
// services/documents_service.py's module docstring for the full reasoning.

export async function render(container) {
  await renderList(container);
}

async function renderList(container) {
  container.innerHTML = "";

  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Library" }),
      el("div", { class: "sub", text: "Documents you keep, not just chat history — plain markdown/text, with keyword search." }),
    ]),
  ]);

  const searchInput = el("input", { placeholder: "Search documents...", style: "flex:1;" });
  const newBtn = el("button", { class: "btn", text: "+ New Document" });
  // Plain file input + FileReader (David's ask 2026-09-01) — works
  // identically in the Electron shell and the plain-HTTP web-access path,
  // so import is no longer gated behind window.jarvis (see brain.js's
  // matching change and the real "null" bug it fixed there).
  const fileInput = el("input", { type: "file", accept: ".md,.txt", style: "display:none;" });
  const importBtn = el("button", { class: "btn", text: "Import from File...", onclick: () => fileInput.click() });
  const importStatus = el("span", { class: "meta" });

  const toolbar = el("div", { class: "glass card" }, [
    el("div", { class: "card-row", style: "gap:8px;" }, [searchInput, newBtn, importBtn, fileInput]),
    el("div", { style: "margin-top:6px;" }, [importStatus]),
  ]);

  const grid = el("div", { id: "library-grid", style: "margin-top:14px;display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;" });

  const wrap = el("div", { class: "view-constrained" }, [header, toolbar, grid]);
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
  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    searchDebounce = setTimeout(() => refreshGrid(grid, container, searchInput.value.trim()), 200);
  });

  await refreshGrid(grid, container, "");
}

async function refreshGrid(grid, container, query) {
  grid.innerHTML = "";
  const results = query
    ? await api(`/api/documents/search?q=${encodeURIComponent(query)}`)
    : (await api("/api/documents")).map((d) => ({ ...d, snippet: null }));

  if (results.length === 0) {
    // Grid is a CSS grid — span the empty state across all columns so it
    // centers on the page rather than sitting in the first cell.
    const empty = query
      ? emptyState({ icon: ICONS.search, title: "No matches", hint: `Nothing in your library matches "${query}".` })
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
    const card = el("div", {
      class: "glass bracket card",
      style: "cursor:pointer;",
      onclick: () => renderEditor(container, item.id),
    }, [
      el("div", { class: "title", text: item.title }),
      el("div", { class: "meta", style: "margin-top:6px;", text: meta }),
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

  container.append(header, contentArea);

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
