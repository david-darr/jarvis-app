import { api, el, toast, confirmDialog } from "./api.js";
import { renderMessageBody } from "./chatContent.js";
import { ICONS } from "./icons.js";

// A reading-first Vault browser for Library. The graph remains available as
// a separate Map mode; both use the same authenticated vault routes.
export function createVaultExplorer(container) {
  let disposed = false;
  let graph = null;
  let selected = null;
  let raw = "";
  let editor = null;
  let requestVersion = 0;
  const openFolders = new Set();
  const byId = new Map();
  const children = new Map();

  const search = el("input", { type: "search", class: "library-vault-search", placeholder: "Find a note or folder", "aria-label": "Search vault notes and folders" });
  const count = el("span", { class: "meta", "aria-live": "polite", text: "Loading vault…" });
  const tree = el("div", { class: "library-vault-tree" });
  const reader = el("div", { class: "library-vault-reader" }, [
    el("div", { class: "library-vault-empty", text: "Choose a note to read it here." }),
  ]);
  const refresh = el("button", { type: "button", class: "btn quiet", text: "Refresh" });
  container.replaceChildren(el("div", { class: "library-vault-browser" }, [
    el("aside", { class: "library-vault-sidebar" }, [
      el("div", { class: "library-vault-sidebar-head" }, [search, refresh]),
      count, tree,
    ]),
    reader,
  ]));

  search.addEventListener("input", drawTree);
  refresh.addEventListener("click", async () => {
    if (!await canLeave()) return;
    selected = null; raw = ""; editor = null;
    await load();
  });
  load();

  async function load() {
    const version = ++requestVersion;
    count.textContent = "Loading vault…";
    tree.replaceChildren();
    try {
      const data = await api("/api/vault/graph");
      if (disposed || version !== requestVersion) return;
      graph = data;
      byId.clear(); children.clear();
      for (const node of data.nodes) byId.set(node.id, node);
      for (const edge of data.edges) {
        if (edge.kind !== "contains") continue;
        if (!children.has(edge.source)) children.set(edge.source, []);
        const node = byId.get(edge.target);
        if (node) children.get(edge.source).push(node);
      }
      for (const items of children.values()) items.sort((a, b) =>
        (a.type === b.type ? 0 : a.type === "folder" ? -1 : 1) || a.name.localeCompare(b.name));
      count.textContent = `${data.nodes.filter((node) => node.type === "note").length} notes · ${data.nodes.filter((node) => node.type === "folder").length - 1} folders`;
      reader.replaceChildren(el("div", { class: "library-vault-empty" }, [
        mark(ICONS.library), el("h3", { text: "Your vault" }),
        el("p", { text: "Browse folders or search to open a note. Select Map for the visual view." }),
      ]));
      drawTree();
    } catch (error) {
      if (disposed || version !== requestVersion) return;
      count.textContent = "Vault unavailable";
      tree.replaceChildren(el("div", { class: "library-empty", text: error.message }));
    }
  }

  function drawTree() {
    tree.replaceChildren();
    if (!graph) return;
    const q = search.value.trim().toLowerCase();
    if (q) {
      const matches = graph.nodes.filter((node) => node.id &&
        `${node.name} ${node.id}`.toLowerCase().includes(q));
      if (!matches.length) tree.append(el("div", { class: "library-empty", text: "No matching notes or folders." }));
      for (const node of matches) tree.append(nodeRow(node, true));
      return;
    }
    for (const node of children.get("") || []) tree.append(node.type === "folder" ? folder(node) : nodeRow(node));
    if (!tree.children.length) tree.append(el("div", { class: "library-empty", text: "No Markdown notes in this vault." }));
  }

  function folder(node) {
    const body = el("div", { class: "library-vault-children" });
    const details = el("details", { class: "library-vault-folder", open: openFolders.has(node.id) }, [
      el("summary", { class: "library-vault-folder-summary" }, [
        mark(ICONS.library), el("span", { text: node.name }),
        el("span", { class: "library-count", text: String((children.get(node.id) || []).length) }),
      ]),
      body,
    ]);
    for (const child of children.get(node.id) || []) body.append(child.type === "folder" ? folder(child) : nodeRow(child));
    details.addEventListener("toggle", () => {
      if (details.open) openFolders.add(node.id); else openFolders.delete(node.id);
    });
    return details;
  }

  function nodeRow(node, showPath = false) {
    if (node.type === "folder") return el("button", { type: "button", class: "library-vault-note", onclick: () => {
      search.value = "";
      const parts = node.id.split("/");
      for (let i = 0; i < parts.length; i++) openFolders.add(parts.slice(0, i + 1).join("/"));
      drawTree();
    } }, [mark(ICONS.library), el("span", { text: node.id })]);
    return el("button", { type: "button", class: "library-vault-note" + (selected === node.id ? " active" : ""),
      title: node.id, onclick: () => selectNote(node) }, [
      mark(ICONS.notes), el("span", { text: showPath ? node.id : node.name }),
    ]);
  }

  async function selectNote(node) {
    if (selected === node.id && !editor) return;
    if (!await canLeave()) return;
    selected = node.id;
    editor = null;
    const version = ++requestVersion;
    const folderParts = (node.folder || "").split("/").filter(Boolean);
    for (let i = 0; i < folderParts.length; i++) openFolders.add(folderParts.slice(0, i + 1).join("/"));
    drawTree();
    reader.replaceChildren(el("div", { class: "library-vault-empty", text: "Opening note…" }));
    try {
      const note = await api(`/api/vault/note?path=${encodeURIComponent(node.id)}`);
      if (disposed || version !== requestVersion) return;
      raw = note.content;
      showNote(node);
    } catch (error) {
      if (disposed || version !== requestVersion) return;
      reader.replaceChildren(el("div", { class: "library-vault-empty", text: `Couldn't open this note: ${error.message}` }));
    }
  }

  function showNote(node) {
    editor = null;
    const edit = el("button", { type: "button", class: "btn", text: "Edit" });
    const header = noteHeader(node, [edit]);
    const body = el("div", { class: "library-vault-reading artifact-document" });
    const readingText = raw.replace(/^---\s*\r?\n[\s\S]*?\r?\n(?:---|\.\.\.)\s*(?:\r?\n|$)/, "");
    renderMessageBody(body, readingText, null);
    linkWikilinks(body);
    reader.replaceChildren(header, body);
    edit.addEventListener("click", () => editNote(node));
  }

  function noteHeader(node, actions) {
    return el("div", { class: "library-vault-reader-head" }, [
      el("div", { class: "library-vault-reader-title" }, [
        el("span", { class: "meta", text: node.folder || "Vault root" }),
        el("h3", { text: node.name }),
      ]),
      el("div", { class: "library-vault-reader-actions" }, actions),
    ]);
  }

  function editNote(node) {
    const save = el("button", { type: "button", class: "btn primary", text: "Save" });
    const cancel = el("button", { type: "button", class: "btn quiet", text: "Cancel" });
    const status = el("span", { class: "meta", role: "status" });
    editor = el("textarea", { class: "library-vault-editor", "aria-label": `Edit ${node.name}` });
    editor.value = raw;
    reader.replaceChildren(noteHeader(node, [status, cancel, save]), editor);
    cancel.addEventListener("click", () => { editor = null; showNote(node); });
    save.addEventListener("click", async () => {
      const editing = editor;
      const version = requestVersion;
      const content = editing.value;
      save.disabled = true;
      status.textContent = "Saving…";
      try {
        await api("/api/vault/note", { method: "POST", body: JSON.stringify({ path: node.id, content }) });
        if (disposed || version !== requestVersion || selected !== node.id || editor !== editing) return;
        raw = content;
        showNote(node);
        toast("Vault note saved", "success");
      } catch (error) {
        if (!disposed && version === requestVersion && editor === editing) status.textContent = error.message;
        save.disabled = false;
      }
    });
  }

  function linkWikilinks(body) {
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    const matches = [];
    while (walker.nextNode()) if (!walker.currentNode.parentElement?.closest("pre, code") && /\[\[.+?\]\]/.test(walker.currentNode.textContent)) matches.push(walker.currentNode);
    for (const textNode of matches) {
      const source = textNode.textContent;
      const fragment = document.createDocumentFragment();
      const pattern = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;
      let last = 0, match;
      while ((match = pattern.exec(source))) {
        fragment.append(document.createTextNode(source.slice(last, match.index)));
        const target = match[1].split("#")[0].replace(/\.md$/i, "").toLowerCase();
        const linked = graph.nodes.find((candidate) => candidate.type === "note" &&
          (candidate.id.replace(/\.md$/i, "").toLowerCase() === target || candidate.name.toLowerCase() === target));
        const label = match[2] || match[1].split("/").pop();
        fragment.append(linked
          ? el("button", { type: "button", class: "vault-note-link", text: label, onclick: () => selectNote(linked) })
          : document.createTextNode(label));
        last = pattern.lastIndex;
      }
      fragment.append(document.createTextNode(source.slice(last)));
      textNode.replaceWith(fragment);
    }
  }

  async function canLeave() {
    if (!editor || editor.value === raw) return true;
    return confirmDialog({ title: "Discard note edits?", message: "Your unsaved changes to this Vault note will be lost.", confirmLabel: "Discard changes" });
  }

  return { canLeave, destroy() { disposed = true; ++requestVersion; } };
}

function mark(svg) {
  const host = el("span", { class: "library-vault-mark", "aria-hidden": "true" });
  host.innerHTML = svg;
  return host;
}
