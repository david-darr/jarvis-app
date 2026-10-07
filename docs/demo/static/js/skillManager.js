import { api, el, toast, confirmDialog, iconButton, emptyState } from "./api.js";
import { ICONS } from "./icons.js";

// The Tool Store's local-skill management panel. Kept separate from catalog
// cards so create/import/edit/delete and exact-content approval share one UI.
export async function renderSkillManager(container, focusSlug = null) {
  container.replaceChildren();
  const form = el("div", { class: "glass card" });
  const nameInput = el("input", { placeholder: "Skill name...", style: "flex:1;" });
  const descInput = el("input", { placeholder: "One-line description...", style: "flex:1;" });
  const addBtn = el("button", { class: "btn", text: "Create" });

  // File input and FileReader work in both Electron and the web client.
  const fileInput = el("input", { type: "file", accept: ".md,.txt", style: "display:none;" });
  const importBtn = el("button", { class: "btn", text: "Import from File...", onclick: () => fileInput.click() });
  const importStatus = el("span", { class: "meta" });

  form.append(
    el("div", { class: "card-row" }, [nameInput, descInput, addBtn]),
    el("div", { class: "card-row", style: "margin-top:8px;" }, [importBtn, importStatus, fileInput]),
  );

  // An import the scan refused shows its report here, inline, rather than as a
  // toast: the user needs to read what was found before deciding anything.
  const importReview = el("div", { style: "margin-top:8px;" });
  form.append(importReview);

  const list = el("div", { id: "skills-list", style: "margin-top:14px;" });
  container.append(el("h3", { class: "tool-store-heading", text: "Your skills" }), form, list);

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
    await importSkillFile(file.name, content, false);
  });

  // Imported skills are untrusted and scanned server-side before anything is
  // written (services/skill_curator.py). Plain fetch rather than api(): a
  // refusal comes back as a 409 carrying the scan report, which api() would
  // flatten into a one-line toast.
  async function importSkillFile(filename, content, confirmed) {
    importReview.innerHTML = "";
    importStatus.textContent = `Importing ${filename}…`;
    let res;
    try {
      res = await fetch("/api/skills/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename, content, confirmed }),
      });
    } catch (e) {
      importStatus.textContent = "";
      toast(`Import failed: ${e.message}`, "error");
      return;
    }
    importStatus.textContent = "";
    const payload = await res.json().catch(() => ({}));
    if (res.ok) {
      await refresh(list);
      toast(`Imported ${filename}`, "success");
      return;
    }
    const detail = payload.detail;
    if (res.status !== 409 || typeof detail !== "object" || detail === null) {
      toast(`Import failed: ${typeof detail === "string" ? detail : res.statusText}`, "error");
      return;
    }
    const actions = [el("button", { class: "btn", text: "Cancel", onclick: () => { importReview.innerHTML = ""; } })];
    if (detail.needs_confirmation) {
      actions.unshift(el("button", {
        class: "btn danger",
        text: "Import anyway",
        onclick: () => importSkillFile(filename, content, true),
      }));
    }
    importReview.append(el("div", { class: "glass card" }, [
      el("div", { class: "title", text: detail.needs_confirmation
        ? `${filename} needs a look before importing`
        : `${filename} was blocked` }),
      el("div", { class: "meta", style: "margin:4px 0 8px;", text: detail.needs_confirmation
        ? "The scan found things a skill can use to misdirect a model. Read the findings; import it only if you trust where it came from."
        : "The scan found something dangerous. This cannot be overridden; fix the file or don't use it." }),
      el("pre", { style: "white-space:pre-wrap;font-size:12px;max-height:240px;overflow:auto;margin:0;", text: detail.report || "" }),
      el("div", { class: "card-row", style: "gap:6px;margin-top:10px;" }, actions),
    ]));
  }

  await refresh(list);
  if (focusSlug) {
    const focused = Array.from(list.children).find((node) => node.dataset.skill === focusSlug);
    focused?.scrollIntoView({ block: "nearest" });
  }
}

async function refresh(list) {
  const skills = await api("/api/skills");
  list.innerHTML = "";
  if (skills.length === 0) {
    list.appendChild(emptyState({
      icon: ICONS.brain,
      title: "No skills yet",
      hint: "Skills are reusable SKILL.md procedures Kairos can follow. Create one above, or import an existing .md file.",
    }));
    return;
  }
  for (const skill of skills) {
    list.appendChild(await buildSkillCard(skill, list));
  }
}

// Editing an already-imported skill (David's ask 2026-09-02) — the backend
// (PUT /api/skills/{slug}, services/skills_service.py's update_skill)
// already supported this; the older skill view only ever
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
    // An unreadable skill (roadmap phase 6) has nothing to edit; the API says why.
    let full;
    try { full = await api(`/api/skills/${skill.slug}`); } catch (problem) { toast(problem.message, "error"); return; }
    card.replaceWith(buildSkillEditor(full, list));
  });
  const card = el("div", { class: "glass bracket card has-row-actions", "data-skill": skill.slug }, [
    el("div", { class: "card-row" }, [
      el("div", {}, [
        el("div", { class: "title", text: skill.slug }),
        el("div", { class: "meta", text: skill.error || skill.description || "No description" }),
        ...curationDetails(skill, list),
      ]),
      el("div", { class: "row-actions" }, [editBtn, delBtn]),
    ]),
  ]);
  return card;
}

const SOURCE_LABELS = {
  bundled: "Bundled with Kairos",
  user: "Written in Kairos",
  imported: "Imported",
  unknown: "Origin unknown (created before curation)",
};

// Where a skill came from, what its scan found, and authoring advice
// (services/skill_curator.py, skill_linter.py). All text is set via
// textContent: findings quote the skill itself, which may be hostile.
function curationDetails(skill, list) {
  const c = skill.curation;
  if (!c) return [];
  const origin = c.origin ? ` from ${c.origin}` : "";
  const scan = c.scan
    ? `Scan: ${c.scan.verdict}${c.scan.findings.length ? ` (${c.scan.findings.length} finding${c.scan.findings.length === 1 ? "" : "s"})` : ""}`
    : "Not scanned (trusted source)";
  const parts = [el("div", { class: "meta", style: "margin-top:4px;", text: `${SOURCE_LABELS[c.source] || c.source}${origin} · ${scan}` })];

  if (c.blocked_for_models) {
    const approveBtn = el("button", { class: "btn danger", text: "Approve for models", onclick: async () => {
      const ok = await confirmDialog({
        title: "Let models use this skill?",
        message: `"${skill.slug}" scanned as dangerous. Approving lets every model read and follow it as it is now; editing it later needs approving again.`,
        confirmLabel: "Approve",
      });
      if (!ok) return;
      await api(`/api/skills/${skill.slug}/approve`, { method: "POST" });
      await refresh(list);
      toast(`Approved ${skill.slug}`, "success");
    } });
    parts.push(el("div", { class: "meta", style: "color:var(--danger);margin-top:4px;", text: "Hidden from models: its content scanned as dangerous." }), approveBtn);
  }

  const notes = [
    ...(c.scan ? c.scan.findings.map((f) => `${f.severity.toUpperCase()} ${f.file}:${f.line} ${f.description} - "${f.match}"`) : []),
    ...c.lint.map((l) => `Advice: ${l.message}`),
  ];
  if (notes.length) {
    const summary = el("summary", { class: "meta", text: `${notes.length} note${notes.length === 1 ? "" : "s"}` });
    const details = el("details", { style: "margin-top:4px;" }, [summary]);
    for (const note of notes) details.appendChild(el("div", { class: "meta", style: "margin-top:2px;", text: note }));
    parts.push(details);
  }
  return parts;
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
