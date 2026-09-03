import { api, el, toast, confirmDialog, iconButton, emptyState } from "../api.js";
import { ICONS } from "../icons.js";
import { createVaultGraph } from "../vaultGraph.js";

// Brain tab: Skills (portable SKILL.md procedures) and Vault (David's ask
// 2026-09-01 — a browsable graph of the vault itself, "kind of similar to
// the VAULT tab in our original jarvis kiosk"), toggled via a segmented tab
// pair rather than two separate top-level nav items, since both are really
// "how JARVIS's memory works" facets of one Brain concept.
let activeSection = "skills";

export async function render(container) {
  container.innerHTML = "";
  let vaultCleanup = null;

  const header = el("div", { class: "view-header" }, [
    el("div", {}, [
      el("h2", { text: "Brain" }),
      el("div", { class: "sub", text: "Skills — portable SKILL.md procedures. Memory lives directly in the vault." }),
    ]),
  ]);

  const tabs = el("div", { class: "segmented-tabs" });
  const body = el("div", { style: "flex:1;min-height:0;display:flex;flex-direction:column;" });

  function renderTabs() {
    tabs.innerHTML = "";
    for (const [id, label] of [["skills", "Skills"], ["vault", "Vault"]]) {
      const tab = el("button", {
        type: "button",
        class: "segmented-tab" + (activeSection === id ? " active" : ""),
        text: label,
        onclick: () => switchSection(id),
      });
      tabs.appendChild(tab);
    }
  }

  async function switchSection(id) {
    if (vaultCleanup) { vaultCleanup(); vaultCleanup = null; }
    activeSection = id;
    renderTabs();
    body.innerHTML = "";
    body.classList.toggle("view-constrained", id === "skills");
    if (id === "skills") {
      await renderSkillsSection(body);
    } else {
      vaultCleanup = createVaultGraph(body);
    }
  }

  // Skills is a normal constrained list; the Vault graph is full-bleed on
  // purpose (it needs the whole canvas), so the constraint is applied per
  // section in switchSection() rather than to the whole view.
  container.append(header, tabs, body);
  await switchSection(activeSection);

  return () => { if (vaultCleanup) vaultCleanup(); };
}

async function renderSkillsSection(container) {
  const form = el("div", { class: "glass card" });
  const nameInput = el("input", { placeholder: "Skill name...", style: "flex:1;" });
  const descInput = el("input", { placeholder: "One-line description...", style: "flex:1;" });
  const addBtn = el("button", { class: "btn", text: "Create" });

  // Import from a local file (David's ask 2026-09-01) — a plain HTML file
  // input + FileReader works identically in the Electron desktop shell and
  // the plain-HTTP web-access path, so this no longer needs Electron's
  // pickSkillFile IPC bridge (window.jarvis) at all. That IPC path used to
  // hide the button entirely outside Electron; real bug found live from
  // that: the hiding logic handed a bare `null` to the native
  // Element.append() below (this `form` is a plain DOM element, not the
  // el() helper), which stringifies a lone `null` argument into a literal
  // "null" text node instead of skipping it — visible garbage text in the
  // web view. Fixed at the root by removing the conditional entirely.
  const fileInput = el("input", { type: "file", accept: ".md,.txt", style: "display:none;" });
  const importBtn = el("button", { class: "btn", text: "Import from File...", onclick: () => fileInput.click() });
  const importStatus = el("span", { class: "meta" });

  form.append(
    el("div", { class: "card-row" }, [nameInput, descInput, addBtn]),
    el("div", { class: "card-row", style: "margin-top:8px;" }, [importBtn, importStatus, fileInput]),
  );

  const list = el("div", { id: "skills-list", style: "margin-top:14px;" });
  container.append(form, list);

  addBtn.addEventListener("click", async () => {
    const name = nameInput.value.trim();
    if (!name) return;
    await api("/api/skills", { method: "POST", body: JSON.stringify({ name, description: descInput.value.trim(), body: "" }) });
    nameInput.value = ""; descInput.value = "";
    await refresh(list);
  });

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    fileInput.value = "";
    if (!file) return;
    const content = await file.text();
    importStatus.textContent = `Importing ${file.name}…`;
    try {
      await api("/api/skills/import", { method: "POST", body: JSON.stringify({ filename: file.name, content }) });
      importStatus.textContent = "";
      await refresh(list);
      toast(`Imported ${file.name}`, "success");
    } catch (e) {
      importStatus.textContent = "";
      toast(`Import failed: ${e.message}`, "error");
    }
  });

  await refresh(list);
}

async function refresh(list) {
  const skills = await api("/api/skills");
  list.innerHTML = "";
  if (skills.length === 0) {
    list.appendChild(emptyState({
      icon: ICONS.brain,
      title: "No skills yet",
      hint: "Skills are reusable SKILL.md procedures JARVIS can follow. Create one above, or import an existing .md file.",
    }));
    return;
  }
  for (const skill of skills) {
    list.appendChild(await buildSkillCard(skill, list));
  }
}

// Editing an already-imported skill (David's ask 2026-09-02) — the backend
// (PUT /api/skills/{slug}, services/skills_service.py's update_skill)
// already supported this; the gap was purely that the Brain tab only ever
// showed slug + description with a Delete button, no way to open or change
// a skill's body. list_skills() deliberately omits body (list stays light,
// same convention as every other list/get pair in this app — Notes, Tasks,
// etc.) — full content is fetched lazily here, only when Edit is clicked.
async function buildSkillCard(skill, list) {
  const delBtn = iconButton(ICONS.trash, "Delete skill", async () => {
    const ok = await confirmDialog({
      title: "Delete this skill?",
      message: `"${skill.slug}" will be permanently deleted. This can't be undone.`,
      confirmLabel: "Delete skill",
    });
    if (!ok) return;
    await api(`/api/skills/${skill.slug}`, { method: "DELETE" });
    await refresh(list);
    toast("Skill deleted", "success");
  }, { danger: true });
  const editBtn = iconButton(ICONS.edit, "Edit skill", async () => {
    const full = await api(`/api/skills/${skill.slug}`);
    card.replaceWith(buildSkillEditor(full, list));
  });
  const card = el("div", { class: "glass bracket card has-row-actions" }, [
    el("div", { class: "card-row" }, [
      el("div", {}, [
        el("div", { class: "title", text: skill.slug }),
        el("div", { class: "meta", text: skill.description || "No description" }),
      ]),
      el("div", { class: "row-actions" }, [editBtn, delBtn]),
    ]),
  ]);
  return card;
}

function buildSkillEditor(skill, list) {
  const descInput = el("input", { style: "width:100%;", value: skill.description || "" });
  const bodyText = el("textarea", { rows: "12", style: "width:100%;font-family:monospace;font-size:12.5px;" });
  bodyText.value = skill.body || "";

  const errorMsg = el("div", { class: "meta", style: "color:var(--danger);" });
  const saveBtn = el("button", { class: "btn", text: "Save" });
  const cancelBtn = el("button", { class: "btn", text: "Cancel", onclick: () => refresh(list) });
  saveBtn.addEventListener("click", async () => {
    try {
      await api(`/api/skills/${skill.slug}`, {
        method: "PUT",
        body: JSON.stringify({ description: descInput.value.trim(), body: bodyText.value }),
      });
      await refresh(list);
      toast(`Saved ${skill.slug}`, "success");
    } catch (e) {
      errorMsg.textContent = e.message;
    }
  });

  return el("div", { class: "glass bracket card" }, [
    el("div", { class: "title", style: "margin-bottom:8px;", text: skill.slug }),
    el("div", { class: "new-tab-field" }, [el("label", { text: "Description" }), descInput]),
    el("div", { class: "new-tab-field", style: "margin-top:8px;" }, [el("label", { text: "Body (SKILL.md content)" }), bodyText]),
    errorMsg,
    el("div", { class: "card-row", style: "gap:6px;margin-top:10px;" }, [saveBtn, cancelBtn]),
  ]);
}
